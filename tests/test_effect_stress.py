"""Phase 6B adversarial effect stress (docs/RESOURCE_BOUNDS.md, attack matrix).

Each test starts its own `beigewall.ipc.server` subprocess (the server owns
task identity: t8 may be begun once per server) and attacks it with the
hostile client in `beigewall.ipc.stress`. Landed effects are always counted
from the trusted ledger, never from what the client observed. Tests that
need the resource-budget flags skip if the server rejects them.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from beigewall.ipc import stress as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T8 = "t8-send-reply"
BOUNDED = ["--effect-count", "3", "--task-effects", "4"]


class _Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bw-stress-")
        os.chmod(self.tmp, 0o700)
        self.sock = os.path.join(self.tmp, "s")
        self.ledger = os.path.join(self.tmp, "receipts.jsonl")
        self.proc = None
        self.sessions = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for s in self.sessions:
            s.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, *flags):
        log_path = os.path.join(self.tmp, "server.log")
        with open(log_path, "wb") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "beigewall.ipc.server", "--socket", self.sock,
                 "--ledger", self.ledger, "--condition", "control", "--tasks", T8,
                 *flags], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while not os.path.exists(self.sock):
            if self.proc.poll() is not None:
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    out = fh.read()
                if flags and self.proc.returncode == 2 and "unrecognized arguments" in out:
                    self.skipTest(f"server does not support resource flags {list(flags)} "
                                  "yet (argparse rejected them)")
                self.fail(f"server exited {self.proc.returncode}: {out[-500:]}")
            if time.monotonic() > deadline:
                self.fail("server did not create its socket")
            time.sleep(0.02)

    def session(self, **kw) -> S.StressSession:
        s = S.StressSession(self.sock, T8, **kw)
        self.sessions.append(s)
        return s

    def landed(self) -> int:
        return S.count_landed(self.ledger, T8)

    def assert_no_double_consumption(self):
        effects = S.effect_rows(self.ledger, T8)
        grants = [r["grant_id"] for r in effects]
        self.assertEqual(len(grants), len(set(grants)), "a grant landed two effects")

    def refusals(self, result) -> list[str]:
        return [o["effect"]["refusal"] for o in result["outcomes"]
                if o.get("effect") and "refusal" in o["effect"]]


class BoundedStressTest(_Server):
    def test_sequential_fresh_grants_land_exactly_cap(self):
        self.start(*BOUNDED)
        sess = self.session()
        res = S.sequential_fresh_grants(sess, 20)
        self.assertEqual(self.landed(), 3)
        self.assertEqual(S.summarize(res)["client_successes"], 3)
        self.assertTrue(all(o["grant_ok"] for o in res["outcomes"]),
                        "budgets must not deny grants: authority is unchanged")
        refusals = self.refusals(res)
        self.assertEqual(len(refusals), 17)
        self.assertTrue(all(r.startswith("effect budget exhausted") for r in refusals))
        rows = S.budget_refusals(self.ledger, T8)
        self.assertEqual(len(rows), 17)
        self.assertEqual(rows[0]["budget"], {"scope": "task-effect-resource",
                                             "consumed": 3, "cap": 3})
        self.assert_no_double_consumption()

    def test_concurrent_connections_land_at_most_cap(self):
        self.start(*BOUNDED)
        sess = self.session()
        res = S.concurrent(sess, 60, 20)
        landed = self.landed()
        self.assertLessEqual(landed, 3)
        self.assertEqual(landed, 3)          # the cap is reachable: min(3, 60)
        self.assertEqual(S.summarize(res)["client_successes"], landed)
        self.assertEqual(len(res["outcomes"]), 60)
        refusals = self.refusals(res)
        self.assertEqual(len(refusals), 57)
        self.assertTrue(all("exhausted" in r for r in refusals), refusals[:3])
        self.assertEqual(len(S.budget_refusals(self.ledger, T8)), 57)
        self.assert_no_double_consumption()

    def test_reconnect_between_attempts_does_not_reset(self):
        self.start(*BOUNDED)
        sess = self.session()
        res = S.reconnect_between(sess, 10)
        self.assertEqual(self.landed(), 3)
        self.assertTrue(all(r.startswith("effect budget exhausted")
                            for r in self.refusals(res)))
        self.assertEqual(len(self.refusals(res)), 7)

    def test_second_process_continues_task_and_lands_nothing_more(self):
        self.start(*BOUNDED)
        sess = self.session()
        S.sequential_fresh_grants(sess, 5)
        self.assertEqual(self.landed(), 3)
        res = S.second_process(sess, 5)
        self.assertEqual(res["returncode"], 0, res.get("stderr"))
        self.assertEqual(len(res["outcomes"]), 5)
        self.assertTrue(all(o["grant_ok"] for o in res["outcomes"]),
                        "the second process must reach the active task's authority")
        self.assertEqual(self.landed(), 3)
        self.assertTrue(all(r.startswith("effect budget exhausted")
                            for r in self.refusals(res)))
        self.assertEqual(len(self.refusals(res)), 5)

    def test_replayed_effect_frame_is_consumed(self):
        self.start(*BOUNDED)
        sess = self.session()
        res = S.replay_effect_frame(sess, 5)
        self.assertTrue(res["captured_from_success"])
        self.assertEqual(self.landed(), 1)
        replays = [o["effect"] for o in res["outcomes"] if o.get("replay")]
        self.assertEqual(len(replays), 5)
        self.assertTrue(all("effect grant consumed" in e.get("refusal", "")
                            for e in replays), replays)

    def test_forged_counter_is_protocol_rejection(self):
        self.start(*BOUNDED)
        sess = self.session()
        res = S.forged_counter(sess)
        effects = [o["effect"] for o in res["outcomes"]]
        self.assertEqual(len(effects), len(S.FORGED_COUNTER_ARGS))
        self.assertTrue(all(e.get("protocol") == "unknown_arg" for e in effects), effects)
        self.assertEqual(self.landed(), 0)

    def test_forged_task_and_grant_ids_are_rejected(self):
        self.start(*BOUNDED)
        sess = self.session()
        tasks = S.forged_task_id(sess)
        for o in tasks["outcomes"]:
            e = o["effect"]
            self.assertFalse(e["ok"])
            if o.get("op") == "begin_task":
                self.assertEqual(e.get("protocol"), "task_already_active")
            else:
                self.assertIn(e.get("protocol"), ("task_not_active", "task_closed"), o)
        grants = S.forged_grant_id(sess)
        for o in grants["outcomes"]:
            self.assertIn("unknown grant", o["effect"].get("refusal", ""), o)
        self.assertEqual(self.landed(), 0)

    def test_reordered_effect_before_grant_is_unknown_grant(self):
        self.start(*BOUNDED)
        sess = self.session()
        res = S.reordered(sess)
        steps = {o["step"]: o for o in res["outcomes"]}
        self.assertIn("unknown grant", steps["effect_before_grant"]["effect"]["refusal"])
        self.assertTrue(steps["grant"]["predicted_matched"])
        self.assertTrue(steps["effect_after_grant"]["effect"]["ok"])
        self.assertEqual(self.landed(), 1)

    def test_resource_policy_receipt_written_at_start(self):
        self.start(*BOUNDED)
        self.session()
        rows = S.resource_policy_rows(self.ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effect_count"], 3)
        self.assertEqual(rows[0]["task_effects"], 4)

    def test_rate_bounds_a_burst(self):
        self.start("--effect-count", "100", "--task-effects", "100", "--rate", "3/10")
        sess = self.session()
        res = S.sequential_fresh_grants(sess, 20)
        self.assertLess(res["elapsed_s"], 10, "burst must fit inside one rate window")
        self.assertEqual(self.landed(), 3)
        refusals = self.refusals(res)
        self.assertEqual(len(refusals), 17)
        self.assertTrue(all(r.startswith("effect rate exceeded") for r in refusals), refusals)


class UnboundedControlTest(_Server):
    def test_unbounded_server_lands_every_fresh_grant(self):
        """Phase 6 result (condition A/B): grant-scoped consumption only."""
        self.start()
        sess = self.session()
        res = S.sequential_fresh_grants(sess, 20)
        self.assertEqual(self.landed(), 20)
        self.assertEqual(S.summarize(res)["client_successes"], 20)
        self.assertEqual(S.resource_policy_rows(self.ledger), [])
        self.assertEqual(S.budget_refusals(self.ledger, T8), [])
        self.assert_no_double_consumption()


class CliMatrixTest(_Server):
    def test_matrix_cli_against_bounded_server(self):
        self.start(*BOUNDED)
        out = os.path.join(self.tmp, "matrix.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "run_effect_stress.py"), "--socket",
             self.sock, "--ledger", self.ledger, "--task", T8, "--attack", "matrix",
             "--n", "10", "--workers", "5", "--out", out],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("| same_grant |", proc.stdout)
        import json
        with open(out, encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["landed_total"], 3)
        self.assertEqual(summary["double_consumed_grants"], 0)
        self.assertEqual(sum(r["landed_delta"] for r in summary["attacks"]), 3)
        self.assertIn("accumulate", summary["note"])


if __name__ == "__main__":
    unittest.main()
