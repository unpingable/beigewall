import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from beigewall.agent.drivers import LLMDriver
from beigewall.agent.harness import Harness
from beigewall.authority import PolicyAuthority, PolicyRule
from beigewall.broker import CapabilityBroker
from beigewall.corpus import build_demo_corpus
from beigewall.receipts import Ledger, canonical_hash


class EffectStub(BaseHTTPRequestHandler):
    """Sequence: grant read, read, grant send, effect (schema arg names),
    read-with-missing-grant_id (malformed), final, narrative."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        n = self.server.request_count
        self.server.request_count += 1
        if n == 0:
            reply = self._tc("c1", "request_grant", {
                "resource": "messages/thread/thr-alice", "operation": "read",
                "projection": "conversation@1"})
        elif n == 1:
            reply = self._tc("c2", "read", {
                "grant_id": self._grant_id(body["messages"]),
                "resource": "messages/thread/thr-alice",
                "projection": "conversation@1"})
        elif n == 2:
            reply = self._tc("c3", "request_grant", {
                "resource": "messages/thread/thr-alice",
                "operation": "send_message"})
        elif n == 3:
            proj = self._proj_hash(body["messages"])
            reply = self._tc("c4", "effect", {
                "grant_id": self._last_grant_id(body["messages"]),
                "effect_name": "send_message",
                "params": {"thread_id": "thr-alice", "body": "thanks"},
                "derived_from": [{"type": "projection", "ref": proj},
                                 {"type": "user_input", "ref": "prompt"}]})
        elif n == 4:
            reply = self._tc("c5", "read", {"resource": "messages/thread/thr-alice"})
            # missing grant_id: malformed
        elif n == 5:
            reply = {"role": "assistant", "content": "Replied to Alice."}
        else:
            reply = {"role": "assistant", "content": json.dumps(
                {"claims": [], "declared_effects": ["send_message"],
                 "summary": "s"})}
        payload = json.dumps({"choices": [{"message": reply}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _tc(call_id, name, args):
        return {"role": "assistant", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]}

    @staticmethod
    def _scan(messages, marker):
        for msg in reversed(messages):
            blob = msg.get("content") or ""
            i = blob.find(marker)
            if i >= 0:
                return blob[i + len(marker):].split('"')[0]
        raise AssertionError(f"stub never saw {marker}")

    def _grant_id(self, messages):
        return self._scan(messages, '"grant_id": "')

    def _last_grant_id(self, messages):
        ids = []
        for msg in messages:
            blob = msg.get("content") or ""
            start = 0
            while True:
                i = blob.find('"grant_id": "', start)
                if i < 0:
                    break
                ids.append(blob[i + 13:].split('"')[0])
                start = i + 13
        self_assert = ids, None
        return ids[-1]

    def _proj_hash(self, messages):
        return self._scan(messages, '"content_hash": "')

    def log_message(self, *args):
        pass


class TestEffectArgMapping(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), EffectStub)
        self.server.request_count = 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_effect_schema_names_map_to_harness_move(self):
        now = [1000.0]
        ledger = Ledger(clock=lambda: now[0])
        authority = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="messages/*",
                              operation="read", projection="conversation@1",
                              decision="approve"),
                   PolicyRule(task_id="*", resource="messages/thread/thr-alice",
                              operation="send_message", projection="*",
                              decision="approve")],
            clock=lambda: now[0])
        corpus = build_demo_corpus()
        broker = CapabilityBroker(corpus, authority, ledger,
                                  clock=lambda: now[0])
        harness = Harness(broker, ledger)
        prompt = "Reply thanks to Alice."
        driver = LLMDriver(self.base_url, "k", "stub")
        # the stub's derived_from user_input ref is bogus ("prompt"): the
        # broker must refuse; the point of this test is the arg mapping
        result = harness.run_task("tx", driver, prompt=prompt)
        kinds = [r.kind for r in ledger.by_task("tx")]
        self.assertIn("grant_mint", kinds)
        self.assertIn("read", kinds)
        # the effect call reached the broker (refused on provenance) rather
        # than crashing the harness with KeyError
        self.assertIn("refusal", kinds)
        refusal = [r for r in ledger.by_task("tx") if r.kind == "refusal"][0]
        self.assertIn("user_input", refusal.detail["reason"])
        # the read missing grant_id became a malformed observation
        mal = [o for o in result.observations if o.get("malformed")]
        self.assertEqual(len(mal), 1)
        self.assertEqual(driver.telemetry()["malformed_requests"], 1)
        self.assertEqual(result.answer, "Replied to Alice.")


if __name__ == "__main__":
    unittest.main()
