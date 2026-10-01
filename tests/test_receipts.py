import json
import unittest

from blackwall.receipts import Ledger, Receipt, canonical_hash


class TestCanonicalHash(unittest.TestCase):
    def test_stable_across_key_order(self):
        a = {"x": 1, "y": [1, 2], "z": {"b": 2, "a": 1}}
        b = {"z": {"a": 1, "b": 2}, "y": [1, 2], "x": 1}
        self.assertEqual(canonical_hash(a), canonical_hash(b))

    def test_changes_with_content(self):
        self.assertNotEqual(canonical_hash({"a": 1}), canonical_hash({"a": 2}))

    def test_hex_sha256_shape(self):
        h = canonical_hash({"a": 1})
        self.assertEqual(len(h), 64)
        int(h, 16)


class TestLedger(unittest.TestCase):
    def test_records_sequential_ids_and_kinds(self):
        led = Ledger(clock=lambda: 10.0)
        r1 = led.record("grant_request", "t1", resource="messages/thread/thr-alice")
        r2 = led.record("read", "t1", grant_id="g-1")
        self.assertEqual((r1.receipt_id, r1.seq), ("r-1", 1))
        self.assertEqual((r2.receipt_id, r2.seq), ("r-2", 2))
        self.assertEqual(r1.kind, "grant_request")
        self.assertEqual(r1.ts, 10.0)

    def test_filter_by_task(self):
        led = Ledger(clock=lambda: 1.0)
        led.record("read", "t1")
        led.record("read", "t2")
        led.record("effect", "t1")
        self.assertEqual([r.kind for r in led.by_task("t1")], ["read", "effect"])

    def test_filter_by_kind(self):
        led = Ledger(clock=lambda: 1.0)
        led.record("refusal", "t1", reason="denied")
        led.record("read", "t1")
        self.assertEqual(len(led.by_kind("refusal")), 1)

    def test_jsonl_sink_roundtrip(self, ):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "receipts.jsonl")
            led = Ledger(clock=lambda: 7.0, sink_path=path)
            led.record("read", "t1", grant_id="g-1", extra={"a": 1})
            led.record("effect", "t1", grant_id="g-2")
            lines = open(path).read().strip().splitlines()
            self.assertEqual(len(lines), 2)
            rec = json.loads(lines[0])
            self.assertEqual(rec["receipt_id"], "r-1")
            self.assertEqual(rec["extra"], {"a": 1})

    def test_receipt_to_dict_extras_merged(self):
        r = Receipt(receipt_id="r-9", seq=9, ts=3.0, kind="read", task_id="t1",
                    detail={"grant_id": "g-1"})
        d = r.to_dict()
        self.assertEqual(d["grant_id"], "g-1")
        self.assertEqual(d["kind"], "read")


if __name__ == "__main__":
    unittest.main()
