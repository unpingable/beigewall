"""Phase 7B — evidence custody against a compromised ledger writer.

Primary experiment: deterministic and adversarial. A trusted producer mints a
canonical receipt stream; an independent witness records its custody; a
controlled compromised writer mutates the primary ledger every way the threat
model allows; the observer must detect every mutation without trusting the
writer, classify it, and fail closed where safety depends on the evidence.
"""

import json
import os
import unittest

from beigewall import witness as W
from beigewall import writer_corruption as C
from beigewall.lifecycle_metrics import _effect_target

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.join(ROOT, "results", "canonical", "receipts.jsonl")


def canonical_rows():
    with open(CANON, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def fresh_witness(rows):
    w = W.Witness()
    for r in rows:
        w.observe(r)
    return w


class TestBaseline(unittest.TestCase):
    def test_honest_writer_matches(self):
        rows = canonical_rows()
        w = fresh_witness(rows)
        rep = W.classify(C.identity(rows), w.records)
        self.assertEqual(rep["health"], W.HEALTHY)
        self.assertFalse(rep["detected"])
        self.assertTrue(all(o["verdict"] == W.MATCH for o in rep["occurrences"]))
        dec = W.recovery_decision(rep)
        self.assertFalse(dec["fail_closed"])
        self.assertTrue(dec["safe_to_continue"])

    def test_witness_chain_is_order_sensitive(self):
        rows = canonical_rows()
        w1 = fresh_witness(rows)
        # same payloads, swapped order -> different chain head (occurrence id)
        swapped = rows[:3] + [rows[4], rows[3]] + rows[5:]
        w2 = W.Witness()
        # witness is fed in producer order; feeding swapped seqs is a producer
        # bug and must be rejected, proving monotonic custody
        with self.assertRaises(ValueError):
            for r in swapped:
                w2.observe(r)


class TestAttackMatrix(unittest.TestCase):
    """Every mutation must be DETECTED; most carry a specific classification."""

    EXPECTED = {
        "omitted_event": {W.OMITTED},
        "altered_field": {W.ALTERED},
        "altered_source_object": {W.ALTERED},
        "altered_provenance": {W.ALTERED},
        "altered_task_identity": {W.ALTERED},
        "altered_grant_identity": {W.ALTERED},
        "altered_effect_identity": {W.ALTERED},
        "duplicated_event": {W.DUPLICATED},
        "replayed_event": {W.REPLAYED, W.DUPLICATED},
        "reordered_events": {W.REORDERED},
        "truncated_ledger": {W.TRUNCATED},
        "fabricated_event": {W.FABRICATED},
        "selective_omission": {W.OMITTED},
        "corrupted_budget_record": {W.OMITTED},
        "corrupted_lifecycle_record": {W.ALTERED},
        "writer_crash_midway": {W.TRUNCATED},
    }

    def test_every_single_snapshot_attack_detected(self):
        rows = canonical_rows()
        for name, fn in C.SINGLE_SNAPSHOT.items():
            if name == "rollback":
                continue  # needs restart context; covered separately
            with self.subTest(attack=name):
                w = fresh_witness(rows)
                primary = fn(rows)
                rep = W.classify(primary, w.records)
                self.assertTrue(rep["detected"], f"{name} not detected")
                self.assertIn(rep["health"], self.EXPECTED[name],
                              f"{name}: got {rep['health']}")
                dec = W.recovery_decision(rep)
                self.assertTrue(dec["fail_closed"], f"{name} did not fail closed")

    def test_rollback_detected_at_restart(self):
        rows = canonical_rows()
        w = fresh_witness(rows)
        primary = C.rollback(rows)
        rep = W.classify(primary, w.records, at_restart=True,
                         prior_head=w.head_seq)
        self.assertEqual(rep["health"], W.ROLLED_BACK)
        self.assertTrue(W.recovery_decision(rep)["fail_closed"])

    def test_fork_equivocation_detected(self):
        rows = canonical_rows()
        w = fresh_witness(rows)
        a, b = C.forked_history(rows)
        res = W.classify_equivocation(a, b, w.records)
        self.assertTrue(res["forked"])
        self.assertEqual(res["health"], W.FORKED)

    def test_omission_of_first_and_last_row(self):
        rows = canonical_rows()
        w = fresh_witness(rows)
        for i in (0, len(rows) - 1):
            rep = W.classify(C.omit_event(rows, index=i), w.records)
            self.assertTrue(rep["detected"])


class TestWitnessAvailability(unittest.TestCase):
    def test_no_witness_fails_closed(self):
        rows = canonical_rows()
        rep = W.classify(C.identity(rows), [])
        self.assertEqual(rep["health"], W.WITNESS_UNAVAILABLE)
        dec = W.recovery_decision(rep)
        self.assertTrue(dec["fail_closed"])
        self.assertFalse(dec["safe_to_continue"])

    def test_absence_of_witness_is_not_evidence_of_nothing(self):
        # a writer that deletes everything AND a missing witness must not look
        # "clean": no witness => fail closed, never HEALTHY
        rep = W.classify([], [])
        self.assertNotEqual(rep["health"], W.HEALTHY)


class TestBudgetSafety(unittest.TestCase):
    """A compromised writer must never make effect-budget consumption look
    smaller (or larger) than it was: any tampering fails closed."""

    def _budget_stream(self):
        # minimal stream: a policy row + two committed effects on one resource
        rows = [
            {"receipt_id": "r-1", "seq": 1, "ts": 1.0, "kind": "resource_policy",
             "task_id": "", "task_effects": 3},
            {"receipt_id": "r-2", "seq": 2, "ts": 2.0, "kind": "grant_mint",
             "task_id": "t1", "grant_id": "g-1", "operation": "effect"},
            {"receipt_id": "r-3", "seq": 3, "ts": 3.0, "kind": "effect",
             "task_id": "t1", "grant_id": "g-1",
             "effect": {"name": "create_event", "params": {}}, "derived_from": []},
            {"receipt_id": "r-4", "seq": 4, "ts": 4.0, "kind": "effect",
             "task_id": "t1", "grant_id": "g-2",
             "effect": {"name": "create_event", "params": {}}, "derived_from": []},
        ]
        return rows

    def test_honest_budget_is_trusted(self):
        rows = self._budget_stream()
        w = fresh_witness(rows)
        out = W.budget_safe_consumption(C.identity(rows), w.records, _effect_target)
        self.assertFalse(out["fail_closed"])
        self.assertEqual(out["consumption"].get("calendar/events"), 2)

    def test_removed_consumption_fails_closed_not_smaller(self):
        rows = self._budget_stream()
        w = fresh_witness(rows)
        # writer deletes a consumed effect to look less-consumed
        primary = C.corrupted_budget_record(rows)
        out = W.budget_safe_consumption(primary, w.records, _effect_target)
        self.assertTrue(out["fail_closed"],
                        "removing a consumed effect must fail closed, never "
                        "yield a smaller consumption")
        self.assertIsNone(out["consumption"])

    def test_inserted_consumption_also_fails_closed(self):
        rows = self._budget_stream()
        w = fresh_witness(rows)
        out = W.budget_safe_consumption(C.fabricated_event(rows), w.records,
                                        _effect_target)
        self.assertTrue(out["fail_closed"])


class TestEffectAuthorityDiscipline(unittest.TestCase):
    def test_fabricated_receipt_never_authorises(self):
        # a forged receipt must never be promoted to authority: recovery on a
        # fabricated ledger fails closed, and there is no API that turns a
        # witness/ledger row into a grant or effect authorization
        rows = canonical_rows()
        w = fresh_witness(rows)
        rep = W.classify(C.fabricated_event(rows), w.records)
        self.assertEqual(rep["health"], W.FABRICATED)
        self.assertTrue(W.recovery_decision(rep, safety_dependent=True)["fail_closed"])


class TestWitnessPersistence(unittest.TestCase):
    def test_witness_reloads_from_its_own_sink(self):
        import tempfile
        rows = canonical_rows()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "witness.jsonl")
            w = W.Witness(sink_path=path)
            for r in rows:
                w.observe(r)
            head = w.head_seq
            w2 = W.Witness.load(path)
            self.assertEqual(w2.head_seq, head)
            self.assertEqual(len(w2.records), len(rows))
            # a reloaded witness still detects tampering
            rep = W.classify(C.altered_field(rows), w2.records)
            self.assertEqual(rep["health"], W.ALTERED)
            # and a new epoch after reload (restart boundary)
            self.assertGreater(w2.epoch, w.epoch)


if __name__ == "__main__":
    unittest.main()
