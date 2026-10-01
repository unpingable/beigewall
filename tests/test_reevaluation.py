import tempfile
import unittest
from pathlib import Path

from blackwall.reevaluation import (attribute_gap, comparison_metrics,
                                    discover_runs, reevaluate_results_dir)

ROOT = Path(__file__).resolve().parent.parent
EXPLORATORY = ROOT / "results/exploratory/gpt-oss-20b-20261001T185916Z"


def ev(outcome="SUCCESS", cap="PASS", adm="ADMISSIBLE", use="USEFUL",
       claims=(), answer="a", degraded=False):
    return {
        "outcome": outcome, "answer": answer, "claims": list(claims),
        "capability": {"verdict": cap},
        "admissibility": {"verdict": adm, "unsupported": [], "unknown": []},
        "usefulness": {"verdict": use, "degraded": degraded},
    }


class TestAttributeGap(unittest.TestCase):
    def test_agreement_success(self):
        self.assertEqual(attribute_gap(True, ev()), "agreement_success")

    def test_string_artifact(self):
        self.assertEqual(attribute_gap(False, ev()), "string_artifact")

    def test_unsupported_claim(self):
        e = ev("FAIL", adm="INADMISSIBLE")
        e["admissibility"]["unsupported"] = [{"kind": "fact"}]
        self.assertEqual(attribute_gap(True, e), "unsupported_claim")
        self.assertEqual(attribute_gap(False, e), "unsupported_claim")

    def test_capability_failure(self):
        self.assertEqual(attribute_gap(True, ev("FAIL", cap="FAIL")),
                         "capability_failure")

    def test_genuine_failure(self):
        e = ev("FAIL", use="NOT_USEFUL")
        self.assertEqual(attribute_gap(False, e), "genuine_failure")
        self.assertEqual(attribute_gap(True, e), "genuine_failure")

    def test_evaluation_uncertainty(self):
        e = ev("UNKNOWN", adm="UNKNOWN")
        self.assertEqual(attribute_gap(True, e), "evaluation_uncertainty")
        self.assertEqual(attribute_gap(False, e), "evaluation_uncertainty")


class TestDiscoverRuns(unittest.TestCase):
    def test_canonical(self):
        runs = discover_runs(ROOT / "results/canonical")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["receipts_path"].endswith("receipts.jsonl"))
        self.assertTrue(runs[0]["metrics_path"].endswith("metrics.json"))

    def test_exploratory_sorted(self):
        runs = discover_runs(EXPLORATORY)
        self.assertEqual([r["name"] for r in runs],
                         [f"run-{i}" for i in range(1, 6)])


def run_result(name, specs):
    return {"name": name, "per_task": {
        tid: {"legacy_strict": legacy, "evaluation": e,
              "attribution": attribute_gap(legacy, e)}
        for tid, (legacy, e) in specs.items()}}


class TestComparisonMetrics(unittest.TestCase):
    def test_two_runs(self):
        bad = ev("FAIL", adm="INADMISSIBLE", use="NOT_USEFUL", answer="x")
        bad["admissibility"]["unsupported"] = [{"kind": "fact"}]
        r1 = run_result("run-1", {
            "a": (True, ev(claims=[{"kind": "fact"}], degraded=True)),
            "b": (True, bad)})
        r2 = run_result("run-2", {
            "a": (False, ev(answer="x")),
            "b": (False, ev("UNKNOWN", adm="UNKNOWN"))})
        m = comparison_metrics([r1, r2])
        self.assertEqual(m["executions"], 4)
        c = m["counts"]
        self.assertEqual(c["old_strict_success"], 2)
        self.assertEqual(c["receipt_grounded_success"], 2)
        self.assertEqual(c["unknown_outcomes"], 1)
        self.assertEqual(c["unsupported_claim_failures"], 1)
        self.assertEqual(c["usefulness_failures"], 1)
        self.assertEqual(c["no_claims_extracted"], 3)
        self.assertEqual(c["degraded_successes"], 1)
        self.assertEqual(m["rates"]["old_strict_success"], 0.5)
        self.assertEqual(m["gap_decomposition"], {
            "string_artifact": 1, "unsupported_claim": 1, "genuine_failure": 0,
            "capability_failure": 0, "evaluation_uncertainty": 1})
        self.assertEqual(m["per_task"]["a"]["runs"], 2)
        self.assertEqual(m["per_task"]["a"]["old_strict"], 1)
        self.assertEqual(m["per_task"]["a"]["receipt_grounded"], 2)
        self.assertEqual(m["per_task"]["b"]["unknown"], 1)
        self.assertEqual(m["per_task"]["b"]["inadmissible"], 1)
        self.assertEqual(m["attribution"]["agreement_success"], 1)


class TestRefusal(unittest.TestCase):
    def test_refuses_canonical_and_source(self):
        with self.assertRaises(ValueError):
            reevaluate_results_dir(ROOT / "results/canonical",
                                   ROOT / "results/canonical/out")
        with self.assertRaises(ValueError):
            reevaluate_results_dir(EXPLORATORY, EXPLORATORY / "run-1")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                reevaluate_results_dir(ROOT / "results/canonical",
                                       ROOT / "results/canonical")


if __name__ == "__main__":
    unittest.main()
