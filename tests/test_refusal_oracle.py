"""Phase 5C refusal-reason oracle: adversarial enumeration and probing.

The agent may learn about the interface and its own authority (scope
patterns, projection names, grant state, budget limits) and the content a
projection discloses under budget. It may not learn protected resource
state (existence, type, object count) through a refusal."""

import unittest

from beigewall.authority import PolicyAuthority
from beigewall.broker import CapabilityBroker, RefusalError
from beigewall.corpus import build_demo_corpus
from beigewall.receipts import Ledger
from beigewall.tasks import LogicalClock, experiment_policy


def fixture():
    clock = LogicalClock()
    corpus = build_demo_corpus()
    ledger = Ledger(clock=clock.now)
    authority = PolicyAuthority(experiment_policy(), clock=clock.now)
    broker = CapabilityBroker(corpus, authority, ledger, clock=clock.now)
    broker.begin_task("t1-summarize-alice", "h")
    return clock, corpus, ledger, authority, broker


def reason(broker, gid, resource, projection="conversation@1"):
    try:
        broker.read("t1-summarize-alice", gid, resource, projection)
        return "OK"
    except RefusalError as exc:
        return str(exc)


class TestEnumeration(unittest.TestCase):
    def test_unknown_and_wrong_typed_ids_are_indistinguishable(self):
        _, _, _, _, broker = fixture()
        g = broker.request_grant("t1-summarize-alice", "messages/*", "read",
                                 projection="conversation@1")["grant_id"]
        probes = ["thr-nobody", "ct-bob", "att-1", "evt-1", "note-1", "msg-1"]
        # six probes: within the eight-read budget, so recovery is testable
        reasons = {p: reason(broker, g, f"messages/thread/{p}") for p in probes}
        # every non-thread id, existing or not, gets the same reason shape
        shapes = {r.replace(p, "<id>") for p, r in reasons.items()}
        self.assertEqual(shapes, {"conversation@1 is not available for messages/thread/<id>"})
        for p, r in reasons.items():
            self.assertNotIn("unknown", r)
            self.assertNotIn("is not a thread", r)
        # a real thread still reads (recovery preserved on the same grant)
        self.assertEqual(reason(broker, g, "messages/thread/thr-alice"), "OK")

    def test_probing_is_charged_against_the_read_budget(self):
        _, _, _, _, broker = fixture()
        g = broker.request_grant("t1-summarize-alice", "messages/*", "read",
                                 projection="conversation@1")["grant_id"]
        for i in range(8):
            r = reason(broker, g, f"messages/thread/probe-{i}")
            self.assertIn("not available", r)
        self.assertEqual(broker._grants[g].reads_used, 8)
        self.assertIn("read budget exhausted", reason(broker, g, "messages/thread/thr-alice"))
        # interface refusals are charged too (projection mismatch)
        _, _, _, _, broker2 = fixture()
        g2 = broker2.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                   "read", projection="conversation@1")["grant_id"]
        self.assertIn("projection mismatch",
                      reason(broker2, g2, "messages/thread/thr-alice", "conversation@2"))
        self.assertEqual(broker2._grants[g2].reads_used, 1)
        self.assertEqual(reason(broker2, g2, "messages/thread/thr-alice"), "OK")

    def test_interface_refusals_keep_recovery_information(self):
        _, _, _, _, broker = fixture()
        g = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                 "read", projection="conversation@1")["grant_id"]
        self.assertIn("outside scope", reason(broker, g, "contacts/ct-alice"))
        self.assertIn("outside scope", reason(broker, g, "messages/attachment/att-1"))
        self.assertIn("projection mismatch",
                      reason(broker, g, "messages/thread/thr-alice", "conversation@2"))
        # under a class-wide grant, a wrong resource *shape* collapses into
        # the same non-oracle reason (projection X is not available for Y),
        # which still tells the agent what to change
        _, _, _, _, broker2 = fixture()
        g2 = broker2.request_grant("t1-summarize-alice", "messages/*", "read",
                                   projection="conversation@1")["grant_id"]
        self.assertEqual(reason(broker2, g2, "messages/attachment/att-1"),
                         "conversation@1 is not available for messages/attachment/att-1")

    def test_effect_targets_unknown_and_wrong_type_are_indistinguishable(self):
        # a wide send scope so the scope check does not fire first
        from beigewall.authority import PolicyRule
        clock = LogicalClock(); corpus = build_demo_corpus(); ledger = Ledger(clock=clock.now)
        rules = [PolicyRule(task_id="*", resource="messages/*", operation="send_message",
                            projection="*", decision="approve")] + experiment_policy()
        broker = CapabilityBroker(corpus, PolicyAuthority(rules, clock=clock.now),
                                  ledger, clock=clock.now)
        broker.begin_task("t1-summarize-alice", "h")
        g = broker.request_grant("t1-summarize-alice", "messages/*", "send_message")["grant_id"]
        out = {}
        for tid in ("thr-nobody", "ct-bob", "att-1", "evt-1"):
            try:
                broker.effect("t1-summarize-alice", g, "send_message",
                              {"thread_id": tid, "body": "x"},
                              [{"type": "user_input", "ref": "h"}])
                out[tid] = "OK"
            except RefusalError as exc:
                out[tid] = str(exc)
        self.assertEqual(set(out.values()), {"effect target not available"})

    def test_grant_requests_and_discovery_do_not_confirm_existence(self):
        _, _, _, _, broker = fixture()
        a = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice", "read",
                                 projection="conversation@1")
        b = broker.request_grant("t1-summarize-alice", "messages/thread/thr-nobody", "read",
                                 projection="conversation@1")
        self.assertTrue(a["ok"] and b["ok"])
        da = broker.discover("t1-summarize-alice", resource="messages/thread/thr-alice")["catalog"]
        db = broker.discover("t1-summarize-alice", resource="messages/thread/thr-nobody")["catalog"]
        reads = lambda c: [r for r in c["rules"] if r["operation"] == "read"]
        self.assertEqual(reads(da), reads(db))

    def test_object_budget_reason_is_budget_bounded(self):
        # the sealed wording (with the count) is kept for canonical identity;
        # the channel is now bounded: each refused read is charged
        clock = LogicalClock(); corpus = build_demo_corpus(); ledger = Ledger(clock=clock.now)
        broker = CapabilityBroker(corpus, PolicyAuthority(experiment_policy(), clock=clock.now),
                                  ledger, clock=clock.now)
        broker.begin_task("t6-budget-exhaustion", "h")
        g = broker.request_grant("t6-budget-exhaustion", "messages/thread/thr-group", "read",
                                 projection="conversation@1")["grant_id"]
        for _ in range(2):
            try:
                broker.read("t6-budget-exhaustion", g, "messages/thread/thr-group", "conversation@1")
            except RefusalError as exc:
                last = str(exc)
        self.assertIn("13 > 5", last)      # sealed wording unchanged
        try:
            broker.read("t6-budget-exhaustion", g, "messages/thread/thr-group", "conversation@1")
        except RefusalError as exc:
            self.assertIn("read budget exhausted", str(exc))


if __name__ == "__main__":
    unittest.main()
