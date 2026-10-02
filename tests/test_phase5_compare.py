"""Phase 5 comparison plumbing on scripted conditions."""

import os
import tempfile
import unittest

from blackwall.agent.drivers import ScriptedDriver
from blackwall.llm_experiment import run_llm_experiment
from blackwall.phase5 import DEFERENCE_CLASSES, compare, render


class TestPhase5Compare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dirs = {}
        for cond in ("control", "denial", "task"):
            d = os.path.join(cls.tmp.name, cond)
            run_llm_experiment(lambda t: ScriptedDriver(t.script, t.narrative),
                               d, label=cond, model="scripted", base_url="n/a",
                               runs=1, condition=cond)
            cls.dirs[cond] = d
        cls.result = compare(cls.dirs, {"anchor": cls.dirs["control"]})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scripted_outcomes_identical_across_conditions(self):
        for name, s in self.result["conditions"].items():
            self.assertEqual(s["classes"], self.result["conditions"]["control"]["classes"], name)
            self.assertEqual(s["disclosure"]["bytes"]["mean"],
                             self.result["conditions"]["control"]["disclosure"]["bytes"]["mean"])
        self.assertEqual(self.result["conversion"]["denial"]["converted"], 0)
        self.assertEqual(self.result["conversion"]["task"]["regressed"], 0)

    def test_timing_and_discovery_receipts_per_condition(self):
        c = self.result["conditions"]
        self.assertEqual(c["control"]["timing"]["none"], c["control"]["timing"]["none"])
        self.assertEqual(c["task"]["timing"]["initial"] + c["task"]["timing"]["none"],
                         c["control"]["timing"]["none"])
        self.assertEqual(c["task"]["discovery"]["calls"], 12)   # one initial view per task
        self.assertGreater(c["denial"]["discovery"]["calls"], 0)  # hints on scripted denials
        self.assertEqual(c["control"]["discovery"]["calls"], 0)

    def test_deference_and_render(self):
        c = self.result["conditions"]["control"]
        self.assertEqual(sum(c["deference"].values()), 12)
        self.assertEqual(c["deference"]["blocked_correctly"], 3)   # t6, t7, t12
        self.assertEqual(c["reasoning_only_completions"], 0)       # scripted: no reasoning
        text = render(self.result)
        for section in ("Primary", "Over-deference", "Acquisition timing",
                        "Grant friction", "Request selection", "probing",
                        "Safety invariants"):
            self.assertIn(section, text)
        for k in DEFERENCE_CLASSES:
            self.assertIn(k, text)


if __name__ == "__main__":
    unittest.main()
