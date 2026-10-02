"""Lifecycle suite generator, describe(), and run_lifecycle CLI plumbing."""

import collections
import json
import os
import tempfile
import unittest

import run_lifecycle
from beigewall.lifecycle_suite import (EFFECT_TASKS, TASK_CLASSES, describe,
                                       llm_trajectories,
                                       scripted_probe_trajectories)

INVARIANTS = ("forbidden_disclosure", "forbidden_effect",
              "accepted_forged_provenance", "stale_revoked_wrong_scope_success",
              "cross_task_leakage", "unmediated_access",
              "duplicate_effect_retry")


def _sig(trajs):
    return [(t.trajectory_id, [(s.kind, s.task_id, s.inject)
                               for s in t.steps]) for t in trajs]


class TestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trajs = llm_trajectories()

    def test_deterministic(self):
        self.assertEqual(_sig(self.trajs), _sig(llm_trajectories()))
        self.assertNotEqual(_sig(self.trajs),
                            _sig(llm_trajectories(seed=1)))

    def test_shape(self):
        self.assertEqual([t.trajectory_id for t in self.trajs],
                         [f"lt-{i:02d}" for i in range(1, 21)])
        for t in self.trajs:
            tasks = [s.task_id for s in t.steps if s.kind == "task"]
            self.assertTrue(4 <= len(tasks) <= 8, t.trajectory_id)
            self.assertTrue(all(a != b for a, b in zip(tasks, tasks[1:])))
            self.assertTrue(set(tasks) & set(EFFECT_TASKS))
            classes = set().union(*(TASK_CLASSES[x] for x in tasks))
            self.assertGreaterEqual(len(classes), 2)
            self.assertEqual(t.steps[-1].kind, "task")

    def test_injection_coverage_and_placement(self):
        c = collections.Counter()
        for t in self.trajs:
            ev = [s.inject["event"] for s in t.steps if s.kind == "inject"]
            c.update(ev)
            for k in ("ttl_cap", "revoke", "transport_loss",
                      "session_restart", "duplicate_delivery"):
                self.assertLessEqual(ev.count(k), 1)
            for i, s in enumerate(t.steps):
                if s.kind != "inject":
                    continue
                e = s.inject["event"]
                if e == "time_jump":
                    self.assertEqual(s.inject["ticks"], 700)
                if e == "transport_loss":
                    nxt = next(x for x in t.steps[i:] if x.kind == "task")
                    self.assertIn(nxt.task_id, EFFECT_TASKS)
        for k in ("ttl_cap", "time_jump", "revoke", "transport_loss",
                  "session_restart", "duplicate_delivery"):
            self.assertGreaterEqual(c[k], 3, k)

    def test_describe_one_row_per_trajectory(self):
        lines = describe(self.trajs).strip().splitlines()
        self.assertEqual(len(lines), 2 + len(self.trajs))
        self.assertTrue(lines[2].startswith("| lt-01 |"))

    def test_scripted_probes_empty(self):
        self.assertEqual(scripted_probe_trajectories(), [])


class TestCLI(unittest.TestCase):
    def test_refuses_canonical(self):
        out = os.path.join(os.path.dirname(run_lifecycle.__file__),
                           "results", "canonical", "x")
        self.assertEqual(run_lifecycle.main(["--out", out]), 2)

    def test_report_mentions_every_invariant(self):
        with tempfile.TemporaryDirectory() as d:
            trajs = llm_trajectories()
            for tid, useful in (("lt-01", "SUCCESS"), ("lt-02", "FAIL")):
                os.makedirs(os.path.join(d, tid))
                rec = {"trajectory_id": tid,
                       "evaluations": {"t1-summarize-alice":
                                       {"outcome": useful}},
                       "metrics": {"stale": 1, "replay": 0,
                                   "duplicate_effects": 0, "restarts": 1,
                                   "invariants": {k: 0 for k in INVARIANTS}},
                       "disagreements": ([{"receipt_id": "r-7"}]
                                         if tid == "lt-02" else [])}
                with open(os.path.join(d, tid, "result.json"), "w") as fh:
                    json.dump(rec, fh)
            rc = run_lifecycle.main(["--out", d, "--report"])
            self.assertEqual(rc, 0)
            text = open(os.path.join(d, "report.md")).read()
            for k in INVARIANTS:
                self.assertIn(f"**{k}: 0**", text)
            self.assertIn("lt-02 r-7", text)
            self.assertIn("Missing/unsealed:", text)
            self.assertIn("lt-03", text)
            self.assertIn("t1-summarize-alice: 1/2", text)


if __name__ == "__main__":
    unittest.main()
