import json
import os
import tempfile
import unittest

from beigewall.metrics import suite_metrics, task_metrics
from beigewall.tasks import run_suite


class TestTaskMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcomes, cls.corpus, cls.ledger = run_suite()
        cls.per_task = {t.task_id: task_metrics(cls.ledger, t.task_id, s)
                        for t, r, s in cls.outcomes}
        cls.success = {t.task_id: s for t, r, s in cls.outcomes}

    def test_counts_grants_and_authorization_events(self):
        m = self.per_task["t2-group-thread-resolution"]
        self.assertEqual(m["grants_requested"], 3)
        self.assertEqual(m["grants_denied"], 1)
        self.assertEqual(m["grants_minted"], 2)
        self.assertEqual(m["authorization_events"], 3)

    def test_disclosure_surface_counts(self):
        m = self.per_task["t1-summarize-alice"]
        self.assertEqual(m["disclosure"]["objects_disclosed"], 4)
        self.assertIn("message.body", m["disclosure"]["fields"])
        self.assertGreater(m["disclosure"]["bytes_disclosed"], 0)

    def test_zero_disclosure_when_all_reads_refused(self):
        m = self.per_task["t6-budget-exhaustion"]
        self.assertEqual(m["disclosure"]["objects_disclosed"], 0)
        self.assertEqual(m["refused_operations"], 2)

    def test_unmediated_attempts_counted(self):
        m = self.per_task["t9-unmediated-access"]
        self.assertEqual(m["unmediated_attempts"], 2)

    def test_divergence_embedded(self):
        m = self.per_task["t5-alice-personal-email"]
        self.assertGreaterEqual(m["divergence"]["counts"]["overclaim"], 1)


class TestSuiteMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcomes, cls.corpus, cls.ledger = run_suite()
        cls.per_task = {t.task_id: task_metrics(cls.ledger, t.task_id, s)
                        for t, r, s in cls.outcomes}
        cls.success = {t.task_id: s for t, r, s in cls.outcomes}
        cls.suite = suite_metrics(cls.per_task, cls.success)

    def test_headline_numbers(self):
        s = self.suite
        self.assertEqual(s["tasks_total"], 12)
        self.assertEqual(s["tasks_succeeded"], 11)
        self.assertEqual(s["tasks_failed"], ["t5-alice-personal-email"])
        self.assertAlmostEqual(s["task_success_rate"], 11 / 12)

    def test_usefulness_under_denial(self):
        # tasks that succeeded despite at least one denial or refusal
        s = self.suite
        self.assertEqual(s["usefulness_under_denial"]["tasks_facing_denial"], 6)
        self.assertEqual(
            s["usefulness_under_denial"]["succeeded_despite_denial"], 5)

    def test_friction_measures(self):
        s = self.suite
        self.assertIn("grants_requested_per_successful_task", s)
        self.assertGreater(s["grants_requested_per_successful_task"], 1.0)
        self.assertEqual(s["unmediated_attempts_total"], 2)
        self.assertEqual(s["refused_operations_total"], 5)

    def test_divergence_totals(self):
        s = self.suite
        div = s["narrative_divergence_totals"]
        self.assertEqual(div["overclaim"], 2)          # t5: object + field
        self.assertEqual(div["imprecise_attribution"], 2)  # t2 + t6
        self.assertEqual(div["omission"], 2)  # t9 (deliberate) + t10 (strict
        # detector: the t10 agenda read also disclosed evt-new-3, created by
        # t4, and the narrative mentioned only the thread-relevant events)
        self.assertEqual(div["undeclared_effect"], 0)


class TestExperimentReproducibility(unittest.TestCase):
    def test_two_runs_produce_identical_metrics(self):
        from beigewall.experiment import run_experiment
        with tempfile.TemporaryDirectory() as d1, \
                tempfile.TemporaryDirectory() as d2:
            run_experiment(d1)
            run_experiment(d2)
            for name in ("metrics.json", "receipts.jsonl"):
                with open(os.path.join(d1, name)) as a, \
                        open(os.path.join(d2, name)) as b:
                    self.assertEqual(a.read(), b.read(), name)

    def test_report_written(self):
        from beigewall.experiment import run_experiment
        with tempfile.TemporaryDirectory() as d:
            run_experiment(d)
            report = open(os.path.join(d, "report.md")).read()
            self.assertIn("task_success_rate", report)
            metrics = json.load(open(os.path.join(d, "metrics.json")))
            self.assertEqual(metrics["suite"]["tasks_total"], 12)


if __name__ == "__main__":
    unittest.main()
