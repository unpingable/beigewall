"""Phase 4 comparison plumbing, exercised with scripted-driver conditions."""

import os
import tempfile
import unittest

from blackwall.agent.drivers import ScriptedDriver
from blackwall.llm_experiment import run_llm_experiment
from blackwall.phase4 import classify, compare, conversion, render


class TestPhase4Comparison(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.control = os.path.join(cls.tmp.name, "control")
        cls.treatment = os.path.join(cls.tmp.name, "treatment")
        run_llm_experiment(lambda t: ScriptedDriver(t.script, t.narrative),
                           cls.control, label="c", model="scripted",
                           base_url="n/a", runs=2, condition="control")
        run_llm_experiment(lambda t: ScriptedDriver(
            [{"op": "discover"}] + list(t.script), t.narrative),
            cls.treatment, label="t", model="scripted", base_url="n/a",
            runs=2, condition="treatment")
        cls.result = compare(cls.control, cls.treatment, cls.control)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_classes_and_conversion(self):
        c, t = self.result["control"], self.result["treatment"]
        self.assertEqual(c["executions"], 24)
        self.assertEqual(c["classes"],
                         {"useful": 18, "useless": 0, "inadmissible": 4,
                          "capability_fail": 2, "unknown": 0})
        self.assertEqual(t["classes"], c["classes"])
        cv = self.result["conversion_vs_control"]
        self.assertEqual(cv["converted"], 0)
        self.assertEqual(cv["regressed"], 0)
        self.assertIsNone(cv["conversion_rate"])

    def test_discovery_telemetry_and_invariants(self):
        c, t = self.result["control"], self.result["treatment"]
        self.assertEqual(c["discovery"]["calls"], 0)
        self.assertEqual(t["discovery"]["calls"], 24)
        self.assertEqual(t["discovery"]["before_first_request"], 24)
        for cond in (c, t):
            inv = cond["invariants"]
            self.assertEqual(inv["forbidden_facts_disclosed"], 0)
            self.assertEqual(inv["forbidden_effects_landed"], 0)
            self.assertEqual(inv["effects_with_undisclosed_projection_ref"], 0)
            self.assertEqual(inv["discovery_receipts_with_projection_or_content"], 0)
            self.assertEqual(inv["unmediated_attempts"], 4)  # t9 fixture ×2
        # discovery adds no disclosure
        self.assertEqual(c["disclosure"]["bytes"]["mean"],
                         t["disclosure"]["bytes"]["mean"])
        self.assertEqual(c["friction"]["grants_requested"]["mean"],
                         t["friction"]["grants_requested"]["mean"])

    def test_render_mentions_every_section(self):
        text = render(self.result)
        for section in ("Outcome classes", "Failure-class conversion vs fresh control",
                        "Failure-class conversion vs sealed phase 3 baseline",
                        "Grant friction", "Request selection", "Discovery use",
                        "Disclosure surface", "divergence", "Safety invariants"):
            self.assertIn(section, text)

    def test_classify(self):
        def ev(cap, adm, use):
            return {"capability": {"verdict": cap},
                    "admissibility": {"verdict": adm},
                    "usefulness": {"verdict": use}}
        self.assertEqual(classify(ev("PASS", "ADMISSIBLE", "USEFUL")), "useful")
        self.assertEqual(classify(ev("PASS", "ADMISSIBLE", "NOT_USEFUL")), "useless")
        self.assertEqual(classify(ev("PASS", "INADMISSIBLE", "USEFUL")), "inadmissible")
        self.assertEqual(classify(ev("FAIL", "ADMISSIBLE", "USEFUL")), "capability_fail")
        self.assertEqual(classify(ev("PASS", "UNKNOWN", "USEFUL")), "unknown")

    def test_conversion_arithmetic(self):
        c = {"per_task": {"x": {"useless": 3, "useful": 2, "inadmissible": 0},
                          "y": {"useless": 1, "useful": 4, "inadmissible": 1}}}
        t = {"per_task": {"x": {"useless": 1, "useful": 4, "inadmissible": 0},
                          "y": {"useless": 2, "useful": 3, "inadmissible": 0}}}
        cv = conversion(c, t)
        self.assertEqual(cv["per_task"]["x"]["converted"], 2)
        self.assertEqual(cv["per_task"]["y"]["regressed"], 1)
        self.assertEqual(cv["control_useless"], 4)
        self.assertEqual(cv["treatment_useless"], 3)
        self.assertEqual(cv["converted"], 1)
        self.assertAlmostEqual(cv["conversion_rate"], 0.25)


if __name__ == "__main__":
    unittest.main()
