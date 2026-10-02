"""Driver records only safe metadata about non-answer fields (presence, size,
reasoning-only completion); the content never reaches telemetry or receipts."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from blackwall.agent.drivers import LLMDriver
from blackwall.agent.harness import Harness
from blackwall.authority import PolicyAuthority
from blackwall.broker import CapabilityBroker
from blackwall.corpus import build_demo_corpus
from blackwall.receipts import Ledger
from blackwall.tasks import LogicalClock, experiment_policy

SECRET = "PRIVATE-CHAIN-OF-THOUGHT-TOKEN"


class ReasoningStub(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        n = self.server.request_count
        self.server.request_count += 1
        if n == 0:   # reasoning-only completion: empty content
            msg = {"role": "assistant", "content": "", "reasoning": SECRET * 3}
        else:        # narrative
            msg = {"role": "assistant", "content": json.dumps(
                {"claims": [], "declared_effects": [], "summary": "none"})}
        payload = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class TestReasoningTelemetry(unittest.TestCase):
    def test_reasoning_only_completion_is_metadata_only(self):
        server = HTTPServer(("127.0.0.1", 0), ReasoningStub)
        server.request_count = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            clock = LogicalClock()
            ledger = Ledger(clock=clock.now)
            broker = CapabilityBroker(build_demo_corpus(),
                                      PolicyAuthority(experiment_policy(),
                                                      clock=clock.now),
                                      ledger, clock=clock.now)
            driver = LLMDriver(f"http://127.0.0.1:{server.server_port}/v1",
                               "", "stub")
            result = Harness(broker, ledger).run_task(
                "t1-summarize-alice", driver, prompt="p")
            tele = driver.telemetry()
            self.assertEqual(result.answer, "")
            self.assertTrue(tele["final_empty"])
            self.assertTrue(tele["final_reasoning_only"])
            self.assertEqual(tele["reasoning_turns"], 1)
            self.assertEqual(tele["reasoning_chars"], len(SECRET) * 3)
            self.assertNotIn(SECRET, json.dumps(tele))
            self.assertNotIn(SECRET, json.dumps(
                [r.to_dict() for r in ledger.all()]))
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
