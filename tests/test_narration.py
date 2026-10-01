import unittest

from blackwall.narration import compare_narrative
from blackwall.receipts import Ledger


def make_ledger(reads, effects, narrative):
    led = Ledger(clock=lambda: 1.0)
    for resource, objects, fields in reads:
        led.record("read", "t1", grant_id="g-1", resource=resource,
                   projection={"name": "conversation@1", "content_hash": "h-" + resource,
                               "source_object_ids": objects,
                               "disclosed_fields": fields,
                               "accounting": {"objects_disclosed": len(objects)}})
    for name in effects:
        led.record("effect", "t1", grant_id="g-2",
                   effect={"name": name, "params": {}}, derived_from=[],
                   result_summary={})
    led.record("narrative", "t1", claims=narrative.get("claims", []),
               declared_effects=narrative.get("declared_effects", []),
               summary=narrative.get("summary", ""), narrative_hash="x")
    return led


class TestAccurateNarrative(unittest.TestCase):
    def test_no_divergence_when_claims_match_receipts(self):
        led = make_ledger(
            reads=[("messages/thread/thr-alice", ["thr-alice", "msg-1"],
                    ["message.body"])],
            effects=["send_message"],
            narrative={
                "claims": [{"resource": "messages/thread/thr-alice",
                            "objects": ["thr-alice", "msg-1"],
                            "fields": ["message.body"]}],
                "declared_effects": ["send_message"],
                "summary": "read the thread, sent a reply"})
        report = compare_narrative(led, "t1")
        self.assertEqual(report["counts"], {"overclaim": 0, "omission": 0,
                                            "imprecise_attribution": 0,
                                            "undeclared_effect": 0})
        self.assertEqual(report["divergences"], [])


class TestDivergenceClasses(unittest.TestCase):
    def test_omission_when_disclosed_object_not_narrated(self):
        led = make_ledger(
            reads=[("messages/thread/thr-alice", ["thr-alice", "msg-1", "msg-2"],
                    ["message.body"])],
            effects=[],
            narrative={"claims": [{"resource": "messages/thread/thr-alice",
                                   "objects": ["thr-alice", "msg-1"],
                                   "fields": ["message.body"]}],
                       "declared_effects": []})
        report = compare_narrative(led, "t1")
        self.assertEqual(report["counts"]["omission"], 1)
        self.assertEqual(report["divergences"][0]["detail"]["object"], "msg-2")

    def test_overclaim_when_narrated_object_never_disclosed(self):
        led = make_ledger(
            reads=[("messages/thread/thr-alice", ["thr-alice"], ["message.body"])],
            effects=[],
            narrative={"claims": [{"resource": "messages/thread/thr-alice",
                                   "objects": ["thr-alice", "msg-99"],
                                   "fields": ["message.body"]}],
                       "declared_effects": []})
        report = compare_narrative(led, "t1")
        self.assertEqual(report["counts"]["overclaim"], 1)
        self.assertEqual(report["divergences"][0]["detail"]["object"], "msg-99")

    def test_overclaim_when_narrated_field_never_disclosed(self):
        led = make_ledger(
            reads=[("messages/thread/thr-alice", ["thr-alice"], ["message.body"])],
            effects=[],
            narrative={"claims": [{"resource": "messages/thread/thr-alice",
                                   "objects": ["thr-alice"],
                                   "fields": ["message.body", "contact.emails"]}],
                       "declared_effects": []})
        report = compare_narrative(led, "t1")
        self.assertEqual(report["counts"]["overclaim"], 1)
        self.assertEqual(report["divergences"][0]["detail"]["field"],
                         "contact.emails")

    def test_imprecise_attribution_for_vague_claims(self):
        led = make_ledger(
            reads=[("messages/thread/thr-alice", ["thr-alice"], ["message.body"])],
            effects=[],
            narrative={"claims": [{"resource": "messages/thread/thr-alice"}],
                       "declared_effects": []})
        report = compare_narrative(led, "t1")
        self.assertEqual(report["counts"]["imprecise_attribution"], 1)
        # vague claims cover nothing, so the disclosed object is also an omission
        self.assertEqual(report["counts"]["omission"], 1)

    def test_undeclared_effect(self):
        led = make_ledger(
            reads=[], effects=["send_message"],
            narrative={"claims": [], "declared_effects": []})
        report = compare_narrative(led, "t1")
        self.assertEqual(report["counts"]["undeclared_effect"], 1)
        self.assertEqual(report["divergences"][0]["detail"]["effect"],
                         "send_message")

    def test_claimed_resource_never_read_is_overclaim(self):
        led = make_ledger(
            reads=[], effects=[],
            narrative={"claims": [{"resource": "contacts/ct-alice",
                                   "objects": ["ct-alice"],
                                   "fields": ["contact.emails"]}],
                       "declared_effects": []})
        report = compare_narrative(led, "t1")
        kinds = {(d["class"], d["detail"].get("object") or d["detail"].get("resource"))
                 for d in report["divergences"]}
        self.assertIn(("overclaim", "ct-alice"), kinds)


if __name__ == "__main__":
    unittest.main()
