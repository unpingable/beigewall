import json
import unittest
from pathlib import Path

from blackwall.budget_metrics import (budget_refusal_kind, compare_conditions,
                                      render, run_budget_summary,
                                      task_budget_outcome)
from blackwall.contracts import AnswerContract

ROOT = Path(__file__).resolve().parent.parent
CANON = ROOT / "results" / "canonical" / "receipts.jsonl"
SCRIPTED = ROOT / "results" / "exploratory" / "phase6-ipc-scripted"
T8 = "t8-send-reply"


def _canon() -> list[dict]:
    with open(CANON) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _t8() -> dict:
    """t8 canonical rows by role: read mint, send mint, effect."""
    rows = [r for r in _canon() if r["task_id"] == T8]
    return {
        "rows": rows,
        "read_req": next(r for r in rows if r["kind"] == "grant_request" and r["operation"] == "read"),
        "read_mint": next(r for r in rows if r["kind"] == "grant_mint" and r["operation"] == "read"),
        "send_req": next(r for r in rows if r["kind"] == "grant_request" and r["operation"] == "send_message"),
        "send_mint": next(r for r in rows if r["kind"] == "grant_mint" and r["operation"] == "send_message"),
        "effect": next(r for r in rows if r["kind"] == "effect"),
    }


def _refusal(reason, scope="effects", consumed=1, cap=1, task_id=T8):
    return {"kind": "refusal", "task_id": task_id, "reason": reason,
            "operation": "send_message", "resource": "messages/thread/thr-alice",
            "budget": {"scope": scope, "consumed": consumed, "cap": cap}}


def _policy(**kw):
    return {"kind": "resource_policy", "task_id": "", "effect_count": 1,
            "task_effects": None, "rate": None, **kw}


class BudgetOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.t = _t8()

    def test_no_budget_pressure(self):
        t = self.t
        o = task_budget_outcome(t["rows"], T8)
        self.assertEqual(o["classification"], "no_budget_pressure")
        self.assertIsNone(o["required_effect_satisfied_before_first_budget_refusal"])
        self.assertEqual(o["effects"], 1)
        self.assertEqual(o["budget_refusals"]["total"], 0)
        self.assertEqual(o["unauthorized_effects"], 0)
        self.assertEqual(o["missing_expected_effect"], 0)
        self.assertEqual(o["grants_requested"], 2)
        self.assertEqual(o["grants_minted"], 2)
        self.assertEqual(o["grant_churn"], 0)
        self.assertIsNone(o["budget_utilization"])
        self.assertFalse(o["budget_exhausted"])

    def test_correctly_blocked_after_required_effect(self):
        t = self.t
        rows = [_policy(), t["read_req"], t["read_mint"], t["send_req"],
                t["send_mint"], t["effect"],
                _refusal("effect budget exhausted: 1 of 1")]
        o = task_budget_outcome(rows, T8)
        self.assertEqual(o["classification"], "correctly_blocked_over_budget")
        self.assertTrue(o["required_effect_satisfied_before_first_budget_refusal"])
        self.assertEqual(o["budget_refusals"],
                         {"total": 1, "count": 1, "task": 0, "rate": 0})
        self.assertEqual(o["budget_utilization"], 1.0)
        self.assertTrue(o["budget_exhausted"])

    def test_correctly_blocked_when_no_effect_required(self):
        t = self.t
        contract = AnswerContract(T8)
        rows = [t["send_req"], t["send_mint"],
                _refusal("task effect budget exhausted: 0 of 0")]
        o = task_budget_outcome(rows, T8, contract)
        self.assertEqual(o["classification"], "correctly_blocked_over_budget")
        self.assertEqual(o["budget_refusals"]["task"], 1)

    def test_incorrectly_blocked_legitimate_effect(self):
        t = self.t
        rows = [t["read_req"], t["read_mint"], t["send_req"], t["send_mint"],
                _refusal("effect budget exhausted: 0 of 0")]
        o = task_budget_outcome(rows, T8)
        self.assertEqual(o["classification"],
                         "incorrectly_blocked_legitimate_effect")
        self.assertFalse(o["required_effect_satisfied_before_first_budget_refusal"])
        self.assertEqual(o["effects"], 0)
        self.assertEqual(o["missing_expected_effect"], 1)

    def test_blocked_then_satisfied(self):
        t = self.t
        rows = [t["send_req"], t["send_mint"],
                _refusal("effect rate exceeded: 1/60s"), t["effect"]]
        o = task_budget_outcome(rows, T8)
        self.assertEqual(o["classification"], "blocked_then_satisfied")
        self.assertEqual(o["budget_refusals"]["rate"], 1)
        self.assertFalse(o["budget_exhausted"])
        self.assertEqual(o["missing_expected_effect"], 0)

    def test_non_budget_refusal_is_not_pressure(self):
        t = self.t
        self.assertIsNone(budget_refusal_kind(
            {"kind": "refusal", "reason": "grant g-1 object budget would be exceeded: 13 > 5"}))
        o = task_budget_outcome(t["rows"] + [{"kind": "refusal", "task_id": T8,
                                              "reason": "grant g-12 object budget would be exceeded"}], T8)
        self.assertEqual(o["classification"], "no_budget_pressure")

    def test_duplicate_unrequested_unauthorized_and_churn(self):
        t = self.t
        dup = dict(t["effect"], receipt_id="r-x")
        stray = dict(t["effect"], grant_id="g-nope", receipt_id="r-y",
                     effect={"name": "send_message",
                             "params": {"thread_id": "thr-alice", "body": "other"}})
        again = dict(t["send_req"], receipt_id="r-z")
        rows = t["rows"] + [dup, stray, again]
        o = task_budget_outcome(rows, T8)
        self.assertEqual(o["effects"], 3)
        self.assertEqual(o["duplicate_effects"], 1)
        self.assertEqual(o["unauthorized_effects"], 1)
        self.assertEqual(o["grant_churn"], 1)
        self.assertEqual(o["unrequested_effects"], 0)
        o2 = task_budget_outcome(rows, T8, AnswerContract(T8))
        self.assertEqual(o2["unrequested_effects"], 3)


class CanonicalLedgerTests(unittest.TestCase):
    def test_canonical_has_no_budget_pressure(self):
        s = run_budget_summary(_canon())
        self.assertEqual(s["tasks"], 12)
        self.assertIsNone(s["resource_policy"])
        self.assertEqual(s["classification"]["no_budget_pressure"], 12)
        self.assertEqual(sum(s["classification"].values()), 12)
        self.assertEqual(s["totals"]["unrequested_effects"], 0)
        self.assertEqual(s["totals"]["duplicate_effects"], 0)
        self.assertEqual(s["totals"]["unauthorized_effects"], 0)
        self.assertEqual(s["totals"]["effects"], 3)
        self.assertEqual(s["totals"]["budget_refusals"]["total"], 0)
        for o in s["per_task"].values():
            self.assertEqual(o["classification"], "no_budget_pressure")

    def test_policy_row_is_found(self):
        s = run_budget_summary([_policy(effect_count=3, rate={"n": 2, "seconds": 60})]
                               + _canon())
        self.assertEqual(s["resource_policy"]["effect_count"], 3)
        self.assertEqual(s["per_task"][T8]["budget_utilization"], 1 / 3)


class CompareConditionsTests(unittest.TestCase):
    @unittest.skipUnless(SCRIPTED.is_dir(), "scripted results dir absent")
    def test_compare_and_render(self):
        res = compare_conditions({"a": SCRIPTED, "b": SCRIPTED})
        a = res["conditions"]["a"]
        self.assertGreater(a["executions"], 0)
        self.assertEqual(a["classification"]["no_budget_pressure"], a["executions"])
        self.assertEqual(a["budget_refusals_total"], 0)
        self.assertEqual(a["per_run"]["effects"]["n"], len(a["runs"]))
        self.assertEqual(res["conditions"]["a"]["effects_total"],
                         res["conditions"]["b"]["effects_total"])
        text = render(res)
        for section in ("Outcome classes", "Budget classification",
                        "Effects and budget refusals", "Grants",
                        "Per-run distributions"):
            self.assertIn(f"## {section}", text)
        for label in ("no_budget_pressure", "grants per successful task",
                      "grant churn", "unrequested effects"):
            self.assertIn(label, text)


if __name__ == "__main__":
    unittest.main()
