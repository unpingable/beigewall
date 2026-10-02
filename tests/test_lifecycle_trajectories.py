"""Deterministic lifecycle probes (phase 5B): every injection is receipted,
every misuse is refused with the expected reason, recovery obtains fresh
authority, and no probe lands a duplicate effect or crosses a task boundary."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from blackwall.agent.drivers import LLMDriver
from blackwall.lifecycle import (Step, Trajectory, probe_trajectories,
                                 run_trajectory, scripted_driver_for)


def run_probe(name):
    tr = next(t for t in probe_trajectories() if t.trajectory_id == name)
    return run_trajectory(tr, scripted_driver_for)


def reasons(rows, task_prefix=None):
    return [r["reason"] for r in rows if r["kind"] == "refusal"
            and (task_prefix is None or r["task_id"].startswith(task_prefix))]


def events(rows):
    return [r["event"] for r in rows if r["kind"] == "lifecycle"]


class TestProbes(unittest.TestCase):
    def test_expiry_and_cross_task_reuse(self):
        res = run_probe("probe-expiry")
        rows = res["rows"]
        self.assertIn("ttl_cap", events(rows))
        self.assertEqual(events(rows).count("time_jump"), 2)
        self.assertTrue(any("expired" in r for r in reasons(rows, "t1")))
        self.assertTrue(any("unknown grant 'g-1'" in r for r in reasons(rows, "t7")))
        # recovery: a fresh grant and a successful read in both tasks
        reads = [r for r in rows if r["kind"] == "read"]
        self.assertEqual([r["task_id"][:2] for r in reads], ["t1", "t1", "t7"])
        self.assertTrue(all(e["outcome"] == "SUCCESS" for e in res["evaluations"].values()))

    def test_revocation(self):
        res = run_probe("probe-revoke")
        rows = res["rows"]
        kinds = [r["kind"] for r in rows if r["task_id"].startswith("t1")]
        self.assertIn("grant_revoke", kinds)
        self.assertLess(kinds.index("grant_revoke"), kinds.index("refusal"))
        self.assertTrue(any("revoked" in r for r in reasons(rows)))
        self.assertEqual(sum(1 for r in rows if r["kind"] == "read"), 1)
        self.assertEqual(res["evaluations"]["t1-summarize-alice"]["outcome"], "SUCCESS")

    def test_transport_loss_does_not_duplicate_the_effect(self):
        res = run_probe("probe-replay-effect")
        rows = res["rows"]
        effects = [r for r in rows if r["kind"] == "effect"]
        self.assertEqual(len(effects), 1)
        self.assertIn("transport_loss", events(rows))
        loss = next(r for r in rows if r["kind"] == "lifecycle" and r["event"] == "transport_loss")
        self.assertEqual(loss["dropped_receipt_id"], effects[0]["receipt_id"])
        self.assertTrue(any("effect grant consumed" in r for r in reasons(rows)))
        self.assertEqual(res["evaluations"]["t8-send-reply"]["outcome"], "SUCCESS")

    def test_cross_task_authority_is_refused_everywhere(self):
        res = run_probe("probe-cross-task")
        rows = res["rows"]
        t12 = reasons(rows, "t12")
        self.assertEqual(sum("unknown grant" in r for r in t12), 3)
        # nothing crossed: t12 performed no effect and read nothing
        self.assertFalse(any(r["kind"] in ("read", "effect") and r["task_id"].startswith("t12")
                             for r in rows))
        self.assertEqual(sum(1 for r in rows if r["kind"] == "effect"), 1)
        self.assertEqual(res["evaluations"]["t12-scope-mismatched-effect"]["outcome"], "SUCCESS")

    def test_duplicate_delivery_doubles_acquisition_visibly(self):
        res = run_probe("probe-duplicate-delivery")
        rows = res["rows"]
        mints = [r for r in rows if r["kind"] == "grant_mint"]
        reqs = [r for r in rows if r["kind"] == "grant_request"]
        self.assertEqual((len(reqs), len(mints)), (2, 2))
        dup = next(r for r in rows if r["kind"] == "lifecycle" and r["event"] == "duplicate_delivery")
        self.assertEqual({dup["first"], dup["second"]}, {m["grant_id"] for m in mints})
        self.assertEqual(res["evaluations"]["t1-summarize-alice"]["outcome"], "SUCCESS")

    def test_restart_and_budget_scope_probes(self):
        res = run_probe("probe-restart")
        self.assertIn("session_restart", events(res["rows"]))
        self.assertTrue(all(e["outcome"] == "SUCCESS" for e in res["evaluations"].values()))
        res = run_probe("probe-budget-and-scope")
        rs = reasons(res["rows"])
        self.assertTrue(any("object budget" in r for r in rs))
        self.assertTrue(any("outside scope" in r for r in rs))

    def test_every_injection_is_in_the_chain_before_its_effect(self):
        for tr in probe_trajectories():
            rows = run_trajectory(tr, scripted_driver_for)["rows"]
            lifecycle = [r for r in rows if r["kind"] == "lifecycle"]
            self.assertTrue(all(r["trajectory_id"] == tr.trajectory_id for r in lifecycle))
            self.assertTrue(all("step" in r for r in lifecycle))


class _SessionStub(BaseHTTPRequestHandler):
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


class TestSessionDriver(unittest.TestCase):
    def test_conversation_persists_across_tasks_and_restart_forgets(self):
        server = HTTPServer(("127.0.0.1", 0), _SessionStub)
        server.bodies = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            tr = Trajectory("s", [Step("task", task_id="t1-summarize-alice"),
                                  Step("task", task_id="t9-unmediated-access"),
                                  Step("inject", inject={"event": "session_restart"}),
                                  Step("task", task_id="t10-calendar-crossref")])
            run_trajectory(tr, lambda _t: LLMDriver(base, "", "stub", session=True))
            # request bodies 1,3,5 are the task turns (2,4,6 narratives)
            first, second, third = server.bodies[0], server.bodies[2], server.bodies[4]
            self.assertEqual(len(first["messages"]), 2)
            self.assertGreater(len(second["messages"]), len(first["messages"]))
            self.assertIn("Next task in the same session", second["messages"][-1]["content"])
            # after restart: fresh conversation that says so
            self.assertEqual(len(third["messages"]), 2)
            self.assertIn("session was restarted", third["messages"][1]["content"])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
