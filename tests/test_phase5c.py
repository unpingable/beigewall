"""Phase 5C: per-task context reset (A) and harness-controlled mid-task expiry (B)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from beigewall.agent.drivers import LLMDriver
from beigewall.lifecycle import Step, Trajectory, run_trajectory, scripted_driver_for
from beigewall.lifecycle_suite import expiry_trajectories


class TestForcedExpiry(unittest.TestCase):
    def test_pre_minted_grant_expires_after_first_read_and_recovery_works(self):
        # the agent mints its send grant first (as real models often do),
        # reads, then tries to send with the pre-minted grant: expired
        read = {"op": "request_grant", "resource": "messages/thread/thr-alice",
                "operation": "read", "projection": "conversation@1", "as": "gA"}
        send_grant = {"op": "request_grant", "resource": "messages/thread/thr-alice",
                      "operation": "send_message", "as": "gS"}
        send = lambda alias: {
            "op": "effect", "grant": alias, "effect": "send_message",
            "params": {"thread_id": "thr-alice", "body": "Reviewed the draft: revenue "
                       "targets look conservative; please clarify the hiring-freeze "
                       "exceptions."},
            "derived_from": [{"type": "projection", "ref": "$last_read"},
                             {"type": "user_input", "ref": "$prompt"}]}
        tr = Trajectory("ex-test", [
            Step("inject", inject={"event": "expire_after_first_use", "ticks": 700}),
            Step("task", task_id="t8-send-reply")],
            probes={"t8-send-reply": [
                send_grant, read,
                {"op": "read", "grant": "gA", "resource": "messages/thread/thr-alice",
                 "projection": "conversation@1"},
                send("gS"),                    # expired: refused
                {**send_grant, "as": "gS2"},   # recovery: fresh grant
                send("gS2"),
                {"op": "final", "answer": "Replied to Alice."}]})
        res = run_trajectory(tr, scripted_driver_for)
        rows = res["rows"]
        inj = next(r for r in rows if r["kind"] == "lifecycle"
                   and r["event"] == "expire_after_first_use")
        self.assertEqual(sorted(inj["expired_grant_ids"]), ["g-1", "g-2"])
        read_row = next(r for r in rows if r["kind"] == "read")
        self.assertEqual(inj["after_receipt_id"], read_row["receipt_id"])
        reasons = [r["reason"] for r in rows if r["kind"] == "refusal"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("expired", reasons[0])
        self.assertEqual(sum(1 for r in rows if r["kind"] == "effect"), 1)
        eff = next(r for r in rows if r["kind"] == "effect")
        # provenance continuous across recovery: the pre-expiry read's hash
        self.assertEqual(eff["derived_from"][0]["ref"], read_row["projection"]["content_hash"])
        self.assertEqual(res["evaluations"]["t8-send-reply"]["outcome"], "SUCCESS")
        from beigewall.lifecycle_metrics import disagreements, trajectory_metrics
        self.assertEqual(disagreements(rows), [])
        m = trajectory_metrics(rows, "ex-test")
        self.assertEqual(m["expired_attempts"], 1)
        self.assertEqual(m["recovery"]["refusals_followed_by_fresh_mint_and_success"], 1)

    def test_expiry_suite_shape(self):
        ts = expiry_trajectories(20)
        self.assertEqual(len(ts), 20)
        for t in ts:
            tasks = [s.task_id for s in t.steps if s.kind == "task"]
            self.assertEqual(len(tasks), len(set(tasks)))
            self.assertTrue(4 <= len(tasks) <= 6)
            injects = [s for s in t.steps if s.kind == "inject"]
            self.assertEqual(len(injects), 1)
            self.assertEqual(injects[0].inject["event"], "expire_after_first_use")
        self.assertEqual([s.task_id for s in ts[0].steps], [s.task_id for s in expiry_trajectories(20)[0].steps])


class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.bodies.append(body)
        n = len(self.server.bodies)
        content = "done" if n % 2 == 1 else json.dumps(
            {"claims": [], "declared_effects": [], "summary": ""})
        payload = json.dumps({"choices": [{"message": {"role": "assistant",
                                                        "content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class TestContextReset(unittest.TestCase):
    def test_reset_mode_starts_every_task_with_a_fresh_conversation(self):
        server = HTTPServer(("127.0.0.1", 0), _Stub)
        server.bodies = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            tr = Trajectory("r", [Step("task", task_id="t1-summarize-alice"),
                                  Step("task", task_id="t9-unmediated-access")])
            run_trajectory(tr, lambda _t: LLMDriver(base, "", "stub", session=False))
            first, second = server.bodies[0], server.bodies[2]
            self.assertEqual(len(first["messages"]), 2)
            self.assertEqual(len(second["messages"]), 2)
            self.assertNotIn("Next task in the same session", second["messages"][1]["content"])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
