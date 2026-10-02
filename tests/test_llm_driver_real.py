"""Tests for real-endpoint plumbing in the LLM driver.

All tests use a local stub server; no real API key is required. These cover
behaviors the scripted driver never exercises: unknown tool names (mapped to
the facade trap as unmediated attempts), malformed tool arguments (telemetry),
proper tool-role message flow, transport retries, and env gating.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from beigewall.agent.drivers import LLMDriver
from beigewall.agent.harness import Harness
from beigewall.authority import PolicyAuthority, PolicyRule
from beigewall.broker import CapabilityBroker
from beigewall.corpus import build_demo_corpus
from beigewall.receipts import Ledger


class BehaviorStub(BaseHTTPRequestHandler):
    """Scripted endpoint: unknown tool, malformed args, valid flow, final."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        n = self.server.request_count
        self.server.request_count += 1
        if n == 0:
            reply = self._tool_call("c1", "shell", {"cmd": "ls /"})
        elif n == 1:
            # malformed JSON arguments on purpose
            reply = {"role": "assistant", "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "request_grant",
                              "arguments": '{"resource": BROKEN'}}]}
        elif n == 2:
            reply = self._tool_call("c3", "request_grant", {
                "resource": "messages/thread/thr-alice", "operation": "read",
                "projection": "conversation@1"})
        elif n == 3:
            reply = self._tool_call("c4", "read", {
                "grant_id": self._extract_grant_id(body["messages"]),
                "resource": "messages/thread/thr-alice",
                "projection": "conversation@1"})
        elif n == 4:
            reply = self._content("The thread is about the quarterly plan.")
        else:
            reply = self._content(json.dumps({
                "claims": [{"resource": "messages/thread/thr-alice",
                            "objects": ["thr-alice", "msg-1", "msg-2", "msg-3"],
                            "fields": ["message.body"]}],
                "declared_effects": [], "summary": "read the thread"}))
        self._send(reply)

    def _send(self, message):
        payload = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _tool_call(call_id, name, args):
        return {"role": "assistant", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]}

    @staticmethod
    def _content(text):
        return {"role": "assistant", "content": text}

    @staticmethod
    def _extract_grant_id(messages):
        for msg in reversed(messages):
            if msg.get("role") == "tool" and '"grant_id"' in msg.get("content", ""):
                blob = msg["content"]
                marker = '"grant_id": "'
                i = blob.find(marker)
                if i >= 0:
                    return blob[i + len(marker):].split('"')[0]
        raise AssertionError("stub never saw a tool-role grant result")

    def log_message(self, *args):
        pass


class FlakyStub(BaseHTTPRequestHandler):
    """Fails the first request with a 500, then answers a final."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.request_count += 1
        if self.server.request_count == 1:
            payload = b'{"error": "transient"}'
            self.send_response(500)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        reply = {"choices": [{"message": {"role": "assistant",
                                          "content": "recovered answer"}}]}
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    server.requests = []
    server.request_count = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def make_harness():
    now = [1000.0]
    ledger = Ledger(clock=lambda: now[0])
    authority = PolicyAuthority(
        rules=[PolicyRule(task_id="*", resource="messages/*", operation="read",
                          projection="conversation@1", decision="approve")],
        clock=lambda: now[0])
    broker = CapabilityBroker(build_demo_corpus(), authority, ledger,
                              clock=lambda: now[0])
    return Harness(broker, ledger), ledger


class TestRealEndpointPlumbing(unittest.TestCase):
    def setUp(self):
        self.server = serve(BehaviorStub)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_full_flow_trap_malformed_and_telemetry(self):
        harness, ledger = make_harness()
        driver = LLMDriver(self.base_url, "k", "stub-model")
        result = harness.run_task("t1", driver, prompt="Summarize the thread.")
        self.assertIn("quarterly plan", result.answer)

        # unknown tool name was routed through the facade trap
        attempts = ledger.by_kind("unmediated_attempt")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].detail["attempted"], "shell")

        # telemetry recorded turns and the malformed request
        tele = driver.telemetry()
        self.assertEqual(tele["malformed_requests"], 1)
        self.assertEqual(tele["turns"], 5)  # shell, broken, grant, read, final
        self.assertEqual(tele["transport_retries"], 0)
        self.assertEqual(len(tele["latencies"]), 5)
        self.assertEqual(tele["tool_calls_by_name"].get("shell"), 1)

        # tool results flowed back as proper tool-role messages
        saw_tool_role = False
        for req in self.server.requests:
            for msg in req["messages"]:
                if msg.get("role") == "tool":
                    saw_tool_role = True
                    self.assertTrue(msg.get("tool_call_id"))
        self.assertTrue(saw_tool_role)

        # receipt chain intact despite the noise
        kinds = [r.kind for r in ledger.by_task("t1")]
        self.assertEqual(kinds, ["unmediated_attempt", "grant_request",
                                 "grant_mint", "read", "narrative", "task_end"])

    def test_transport_retry_telemetry(self):
        server = serve(FlakyStub)
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            harness, ledger = make_harness()
            driver = LLMDriver(url, "k", "stub-model")
            result = harness.run_task("tF", driver, prompt="p")
            self.assertEqual(result.answer, "recovered answer")
            self.assertEqual(driver.telemetry()["transport_retries"], 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_from_env_allows_keyless_loopback_only(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("BEIGEWALL_LLM_BASE_URL", "BEIGEWALL_LLM_API_KEY",
                           "BEIGEWALL_LLM_MODEL")}
        try:
            os.environ["BEIGEWALL_LLM_BASE_URL"] = "http://127.0.0.1:11434/v1"
            self.assertIsNotNone(LLMDriver.from_env())
            os.environ["BEIGEWALL_LLM_BASE_URL"] = "https://api.remote.example/v1"
            self.assertIsNone(LLMDriver.from_env())  # remote requires a key
            os.environ["BEIGEWALL_LLM_API_KEY"] = "k"
            self.assertIsNotNone(LLMDriver.from_env())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
