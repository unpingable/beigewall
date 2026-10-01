import json
import os
import tempfile
import unittest

from blackwall.agent.drivers import ScriptedDriver
from blackwall.llm_experiment import run_llm_experiment
from blackwall.tasks import task_suite


class TestLLMExperimentRunner(unittest.TestCase):
    """Driving the LLM runner with the scripted drivers must reproduce the
    canonical receipts byte-for-byte per run (same tasks, policy, corpus)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = cls.tmp.name
        factory = lambda task: ScriptedDriver(task.script, task.narrative)
        cls.result = run_llm_experiment(
            factory, cls.out, label="selfcheck", model="scripted-driver",
            base_url="n/a", runs=2)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_per_run_artifacts_exist(self):
        for i in (1, 2):
            run_dir = os.path.join(self.out, f"run-{i}")
            self.assertTrue(os.path.exists(os.path.join(run_dir, "receipts.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(run_dir, "metrics.json")))
        for name in ("aggregate.json", "meta.json", "report.md"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)

    def test_run_receipts_match_canonical_byte_for_byte(self):
        canonical = open("results/canonical/receipts.jsonl").read()
        for i in (1, 2):
            with open(os.path.join(self.out, f"run-{i}", "receipts.jsonl")) as fh:
                self.assertEqual(fh.read(), canonical, f"run-{i}")

    def test_aggregate_frequencies_match_scripted_baseline(self):
        agg = self.result["aggregate"]
        self.assertEqual(agg["suite"]["runs"], 2)
        self.assertEqual(agg["per_task"]["t5-alice-personal-email"]["success_frequency"], 0.0)
        self.assertEqual(agg["per_task"]["t1-summarize-alice"]["success_frequency"], 1.0)
        self.assertEqual(agg["suite"]["success_rate"]["per_run"], [11 / 12, 11 / 12])

    def test_meta_records_identity_without_secrets(self):
        meta = json.load(open(os.path.join(self.out, "meta.json")))
        self.assertEqual(meta["label"], "selfcheck")
        self.assertEqual(meta["model"], "scripted-driver")
        self.assertEqual(meta["runs"], 2)
        self.assertNotIn("api_key", json.dumps(meta).lower())
        self.assertNotIn("authorization", json.dumps(meta).lower())

    def test_per_task_records_carry_telemetry_and_recovery(self):
        run1 = json.load(open(os.path.join(self.out, "run-1", "metrics.json")))
        t2 = run1["per_task"]["t2-group-thread-resolution"]
        self.assertIn("telemetry", t2)
        self.assertEqual(t2["denial_recovery"]["summary"]["adjusted_minted"], 1)
        t6 = run1["per_task"]["t6-budget-exhaustion"]
        self.assertEqual(t6["denial_recovery"]["summary"]["repeated"], 1)
        self.assertEqual(t6["denial_recovery"]["summary"]["no_retry"], 1)

    def test_canonical_results_untouched(self):
        # the canonical directory must be byte-identical after LLM runs
        canonical_metrics = json.load(open("results/canonical/metrics.json"))
        self.assertEqual(canonical_metrics["suite"]["tasks_total"], 12)
        self.assertNotIn("selfcheck", json.dumps(canonical_metrics))


if __name__ == "__main__":
    unittest.main()
