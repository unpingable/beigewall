import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from blackwall.agent.drivers import LLMDriver
from blackwall.agent.harness import Harness
from blackwall.authority import PolicyAuthority, PolicyRule
from blackwall.broker import CapabilityBroker
from blackwall.corpus import build_demo_corpus
from blackwall.receipts import Ledger


class StubLLMHandler(BaseHTTPRequestHandler):
    """Canned OpenAI-compatible responses: grant request, read, final, narrative."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        n = self.server.request_count
        self.server.request_count += 1
        messages = body["messages"]
        if n == 0:
            reply = self._tool_call("request_grant", {
                "resource": "messages/thread/thr-alice", "operation": "read",
                "projection": "conversation@1"})
        elif n == 1:
            grant_id = self._extract_grant_id(messages)
            reply = self._tool_call("read", {
                "grant_id": grant_id,
                "resource": "messages/thread/thr-alice",
                "projection": "conversation@1"})
        elif n == 2:
            reply = self._content("The thread is about the quarterly plan.")
        else:
            reply = self._content(json.dumps({
                "claims": [{"resource": "messages/thread/thr-alice",
                            "objects": ["msg-1", "msg-2", "msg-3"],
                            "fields": ["message.body"]}],
                "declared_effects": [],
                "summary": "I read the Alice thread via conversation@1."}))
        payload = json.dumps({"choices": [{"message": reply}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _tool_call(name, args):
        return {"role": "assistant", "tool_calls": [
            {"id": "call-1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]}

    @staticmethod
    def _content(text):
        return {"role": "assistant", "content": text}

    @staticmethod
    def _extract_grant_id(messages):
        for msg in reversed(messages):
            if msg.get("role") == "user" and "Tool results" in msg.get("content", ""):
                blob = msg["content"]
                marker = '"grant_id": "'
                i = blob.find(marker)
                if i >= 0:
                    return blob[i + len(marker):].split('"')[0]
        raise AssertionError("stub server never saw a minted grant id")

    def log_message(self, *args):
        pass


class TestLLMDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubLLMHandler)
        cls.server.request_count = 0
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_from_env_gating(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("BLACKWALL_LLM_BASE_URL", "BLACKWALL_LLM_API_KEY")}
        try:
            self.assertIsNone(LLMDriver.from_env())
            os.environ["BLACKWALL_LLM_BASE_URL"] = self.base_url
            os.environ["BLACKWALL_LLM_API_KEY"] = "test-key"
            self.assertIsNotNone(LLMDriver.from_env())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_llm_driver_runs_same_broker_boundary(self):
        now = [1000.0]
        ledger = Ledger(clock=lambda: now[0])
        authority = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="messages/*", operation="read",
                              projection="conversation@1", decision="approve")],
            clock=lambda: now[0])
        broker = CapabilityBroker(build_demo_corpus(), authority, ledger,
                                  clock=lambda: now[0])
        harness = Harness(broker, ledger, prompt="Summarize the Alice thread.")
        driver = LLMDriver(self.base_url, "test-key", "stub-model")
        result = harness.run_task("tl1", driver)
        self.assertIn("quarterly plan", result.answer)
        kinds = [r.kind for r in ledger.by_task("tl1")]
        self.assertEqual(kinds, ["grant_request", "grant_mint", "read",
                                 "narrative", "task_end"])
        narrative = ledger.by_kind("narrative")[0]
        self.assertEqual(narrative.detail["claims"][0]["resource"],
                         "messages/thread/thr-alice")


if __name__ == "__main__":
    unittest.main()
