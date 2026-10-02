"""Phase 7C — recoverable evidence custody.

With the witness retaining canonical envelopes, the observer can RECONSTRUCT
canonical entries a compromised writer merely omitted (omission / truncation /
rollback), each tagged by provenance and re-verified against the attested
chain, while actively rewritten entries remain detect-and-fail-closed and
hash-only custody remains detectable-but-not-reconstructable. Recovery yields
evidence only: it never mints authority, restores budget, or authorizes an
effect.
"""

import json
import os
import unittest

from blackwall import witness as W
from blackwall import writer_corruption as C
from blackwall.lifecycle_metrics import _effect_target

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.join(ROOT, "results", "canonical", "receipts.jsonl")


def canonical_rows():
    with open(CANON, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def retain_witness(rows):
    w = W.Witness(retain=True)
    for r in rows:
        w.observe(r)
    return w


class TestReconstructOmissionClasses(unittest.TestCase):
    """Missing canonical material is reconstructable from witness custody."""

    def _check_recovered(self, rows, primary, expect_health):
        w = retain_witness(rows)
        rec = W.reconstruct(primary, w.records)
        self.assertTrue(rec["recoverable"], f"{expect_health}: {rec['reason']}")
        self.assertEqual(rec["health"], expect_health)
        # the reconstruction is the full canonical history, in order
        seqs = [e["seq"] for e in rec["reconstructed"]]
        self.assertEqual(seqs, [r["seq"] for r in w.records])
        # recovered entries are tagged distinctly from survivors
        self.assertGreater(rec["recovered_count"], 0)
        provs = {e["provenance"] for e in rec["reconstructed"]}
        self.assertTrue(provs <= {"original", "recovered"})
        # each reconstructed row hashes to the witness attestation
        for e in rec["reconstructed"]:
            wrec = next(x for x in w.records if x["seq"] == e["seq"])
            self.assertEqual(W.content_hash(e["row"]), wrec["content_hash"])
        return rec

    def test_omitted_receipt_reconstructs(self):
        rows = canonical_rows()
        self._check_recovered(rows, C.omit_event(rows), W.OMITTED)

    def test_selective_deletion_reconstructs(self):
        rows = canonical_rows()
        self._check_recovered(rows, C.selective_omission(rows, "read"), W.OMITTED)

    def test_truncation_reconstructs(self):
        rows = canonical_rows()
        self._check_recovered(rows, C.truncated_ledger(rows), W.TRUNCATED)

    def test_writer_crash_midway_reconstructs(self):
        rows = canonical_rows()
        self._check_recovered(rows, C.writer_crash_midway(rows), W.TRUNCATED)

    def test_rollback_reconstructs(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        primary = C.rollback(rows)
        # rollback needs restart context to classify; reconstruct treats a
        # clean prefix the same (missing tail) and rebuilds it
        rec = W.reconstruct(primary, w.records)
        self.assertTrue(rec["recoverable"])
        self.assertIn(rec["health"], (W.TRUNCATED, W.ROLLED_BACK))
        self.assertEqual([e["seq"] for e in rec["reconstructed"]],
                         [r["seq"] for r in w.records])

    def test_recovered_rows_equal_the_originals(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.omit_event(rows, index=5), w.records)
        recovered = [e for e in rec["reconstructed"] if e["provenance"] == "recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["row"], rows[5])


class TestNonRecoverable(unittest.TestCase):
    """Active rewriting is detected and failed closed, never 'repaired'."""

    def test_altered_not_recoverable(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.altered_field(rows), w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.ALTERED)
        self.assertIsNone(rec["reconstructed"])

    def test_fabricated_not_recoverable(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.fabricated_event(rows), w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.FABRICATED)

    def test_duplicate_not_recoverable(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.duplicated_event(rows), w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.DUPLICATED)

    def test_reordered_not_recoverable(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.reordered_events(rows), w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.REORDERED)

    def test_hash_only_witness_is_detectable_not_recoverable(self):
        rows = canonical_rows()
        w = W.Witness(retain=False)          # 7B hash-only mode
        for r in rows:
            w.observe(r)
        rec = W.reconstruct(C.omit_event(rows), w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.INDETERMINATE)
        self.assertIn("hashes only", rec["reason"])

    def test_no_witness_indeterminate(self):
        rows = canonical_rows()
        rec = W.reconstruct(C.omit_event(rows), [])
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.WITNESS_UNAVAILABLE)


class TestHostileAgainstRecovery(unittest.TestCase):
    def test_tampered_witness_envelope_self_detects(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        # an attacker who DID reach the witness swaps an envelope's content;
        # its hash no longer matches, so verify_chain/reconstruct refuse
        w.records[4]["envelope"] = dict(w.records[4]["envelope"], _evil=1)
        self.assertFalse(W.verify_chain(w.records)["ok"])
        rec = W.reconstruct(C.omit_event(rows), w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.INDETERMINATE)

    def test_tampered_witness_chain_self_detects(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        w.records[3]["chain_hash"] = "deadbeef"
        self.assertFalse(W.verify_chain(w.records)["ok"])

    def test_recovery_does_not_restore_budget(self):
        # a writer deletes a consumed effect; recovery rebuilds the EVIDENCE,
        # but the budget path still fails closed on the (divergent) primary and
        # never derives a smaller/relaxed consumption from recovered rows
        rows = canonical_rows()
        w = retain_witness(rows)
        primary = C.corrupted_budget_record(rows)
        budget = W.budget_safe_consumption(primary, w.records, _effect_target)
        self.assertTrue(budget["fail_closed"])
        self.assertIsNone(budget["consumption"])
        # reconstruction rebuilds the EVIDENCE (the deleted effect is restored,
        # so true consumption is never understated) but carries no authority
        rec = W.reconstruct(primary, w.records)
        self.assertTrue(rec["recoverable"])
        self.assertGreater(rec["recovered_count"], 0)

    def test_recovery_is_evidence_not_authority(self):
        # the reconstruct result carries only evidence rows + provenance,
        # never a grant, effect authorization, or "persisted by writer" claim
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.truncated_ledger(rows), w.records)
        for e in rec["reconstructed"]:
            self.assertEqual(set(e), {"seq", "provenance", "row"})
            self.assertIn(e["provenance"], ("original", "recovered"))

    def test_witness_rollback_does_not_enable_false_recovery(self):
        # the witness itself is rolled back (lost its tail); a primary that
        # still has those rows must not be "recovered" against a short witness —
        # the extra rows are unattested (FABRICATED) and fail closed
        rows = canonical_rows()
        w = retain_witness(rows)
        short = W.Witness(retain=True)
        short.records = w.records[:len(w.records) - 4]
        short._last_seq = short.records[-1]["seq"]
        rec = W.reconstruct(rows, short.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.FABRICATED)

    def test_identity_confusion_is_altered_not_recovered(self):
        # same seq, different occurrence_id (writer/witness identity confusion)
        rows = canonical_rows()
        w = retain_witness(rows)
        primary = [dict(r) for r in rows]
        primary[6]["receipt_id"] = "r-impostor"
        rec = W.reconstruct(primary, w.records)
        self.assertFalse(rec["recoverable"])
        self.assertEqual(rec["health"], W.ALTERED)

    def test_duplicate_recovery_yields_each_seq_once(self):
        rows = canonical_rows()
        w = retain_witness(rows)
        rec = W.reconstruct(C.selective_omission(rows, "read"), w.records)
        seqs = [e["seq"] for e in rec["reconstructed"]]
        self.assertEqual(len(seqs), len(set(seqs)))


class TestRetainPersistence(unittest.TestCase):
    def test_retain_witness_reloads_and_reconstructs(self):
        import tempfile
        rows = canonical_rows()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "witness.jsonl")
            w = W.Witness(sink_path=path, retain=True)
            for r in rows:
                w.observe(r)
            w2 = W.Witness.load(path)
            self.assertTrue(all("envelope" in r for r in w2.records))
            rec = W.reconstruct(C.truncated_ledger(rows), w2.records)
            self.assertTrue(rec["recoverable"])

    def test_hash_only_mode_unchanged_record_shape(self):
        # 7B default must be byte-stable: no envelope field in hash-only mode
        rows = canonical_rows()[:3]
        w = W.Witness(retain=False)
        for r in rows:
            w.observe(r)
        self.assertTrue(all("envelope" not in r for r in w.records))


if __name__ == "__main__":
    unittest.main()
