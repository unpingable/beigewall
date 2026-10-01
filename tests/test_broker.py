import unittest

from blackwall.authority import PolicyAuthority, PolicyRule
from blackwall.broker import CapabilityBroker, RefusalError
from blackwall.corpus import build_demo_corpus
from blackwall.grants import Budget
from blackwall.receipts import Ledger, canonical_hash


class BrokerFixture(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.corpus = build_demo_corpus()
        self.ledger = Ledger(clock=lambda: self.now[0])
        self.authority = PolicyAuthority(
            rules=[
                PolicyRule(task_id="*", resource="messages/*", operation="read",
                           projection="conversation@1", decision="approve"),
                PolicyRule(task_id="*", resource="messages/*", operation="read",
                           projection="attachment@1", decision="approve"),
                PolicyRule(task_id="*", resource="notes/*", operation="read",
                           projection="note@1", decision="approve"),
                PolicyRule(task_id="t8", resource="messages/thread/thr-alice",
                           operation="send_message", projection="*",
                           decision="approve"),
                PolicyRule(task_id="t8", resource="notes/*",
                           operation="update_note", projection="*",
                           decision="approve"),
                PolicyRule(task_id="tdeny", resource="contacts/*", operation="read",
                           projection="*", decision="deny", reason="not needed"),
            ],
            clock=lambda: self.now[0])
        self.broker = CapabilityBroker(self.corpus, self.authority, self.ledger,
                                       clock=lambda: self.now[0])
        self.prompt_hash = canonical_hash({"prompt": "do the thing"})


class TestGrantRequests(BrokerFixture):
    def test_grant_request_records_receipt_chain(self):
        self.broker.begin_task("t1", self.prompt_hash)
        res = self.broker.request_grant("t1", "messages/thread/thr-alice", "read",
                                        projection="conversation@1")
        self.assertTrue(res["ok"])
        kinds = [r.kind for r in self.ledger.by_task("t1")]
        self.assertEqual(kinds, ["grant_request", "grant_mint"])
        mint = self.ledger.by_task("t1")[1]
        self.assertEqual(mint.detail["grant_id"], res["grant_id"])

    def test_denied_request_is_receipted_telemetry(self):
        self.broker.begin_task("tdeny", self.prompt_hash)
        res = self.broker.request_grant("tdeny", "contacts/ct-alice", "read",
                                        projection="contact@1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "not needed")
        kinds = [r.kind for r in self.ledger.by_task("tdeny")]
        self.assertEqual(kinds, ["grant_request", "grant_deny"])


class TestReads(BrokerFixture):
    def test_read_happy_path_discloses_with_provenance(self):
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/thr-alice", "read",
                                      projection="conversation@1")
        out = self.broker.read("t1", g["grant_id"], "messages/thread/thr-alice",
                               projection="conversation@1")
        self.assertTrue(out["ok"])
        self.assertIn("messages", out["content"])
        read_rcpt = self.ledger.by_kind("read")[0]
        prov = read_rcpt.detail["projection"]
        self.assertEqual(prov["name"], "conversation@1")
        self.assertIn("msg-1", prov["source_object_ids"])
        self.assertIn("message.body", prov["disclosed_fields"])
        self.assertEqual(len(prov["content_hash"]), 64)

    def test_unknown_grant_refused(self):
        self.broker.begin_task("t1", self.prompt_hash)
        with self.assertRaises(RefusalError):
            self.broker.read("t1", "g-999", "messages/thread/thr-alice",
                             projection="conversation@1")
        self.assertEqual(len(self.ledger.by_kind("refusal")), 1)

    def test_expired_grant_refused(self):
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/thr-alice", "read",
                                      projection="conversation@1", ttl=10)
        self.now[0] += 11
        with self.assertRaises(RefusalError):
            self.broker.read("t1", g["grant_id"], "messages/thread/thr-alice",
                             projection="conversation@1")

    def test_projection_version_mismatch_refused(self):
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/thr-alice", "read",
                                      projection="conversation@1")
        with self.assertRaises(RefusalError):
            self.broker.read("t1", g["grant_id"], "messages/thread/thr-alice",
                             projection="conversation@2")

    def test_object_budget_refusal_discloses_nothing(self):
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/thr-group", "read",
                                      projection="conversation@1",
                                      budget=Budget(reads=8, objects=5))
        with self.assertRaises(RefusalError) as ctx:
            self.broker.read("t1", g["grant_id"], "messages/thread/thr-group",
                             projection="conversation@1")
        self.assertIn("object budget", str(ctx.exception))
        self.assertEqual(len(self.ledger.by_kind("read")), 0)

    def test_read_budget_exhaustion(self):
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/thr-alice", "read",
                                      projection="conversation@1",
                                      budget=Budget(reads=1, objects=100))
        self.broker.read("t1", g["grant_id"], "messages/thread/thr-alice",
                         projection="conversation@1")
        with self.assertRaises(RefusalError):
            self.broker.read("t1", g["grant_id"], "messages/thread/thr-alice",
                             projection="conversation@1")

    def test_resource_outside_grant_scope_refused(self):
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/thr-alice", "read",
                                      projection="conversation@1")
        with self.assertRaises(RefusalError):
            self.broker.read("t1", g["grant_id"], "messages/thread/thr-group",
                             projection="conversation@1")

    def test_read_of_nonexistent_resource_refused_not_crash(self):
        # discovered via real-model probing: a model guesses resource names;
        # a grant may legitimately cover a name that does not exist. The
        # broker must refuse fail-closed, not raise KeyError.
        self.broker.begin_task("t1", self.prompt_hash)
        g = self.broker.request_grant("t1", "messages/thread/alice", "read",
                                      projection="conversation@1")
        self.assertTrue(g["ok"])  # policy approves the messages/* scope
        with self.assertRaises(RefusalError) as ctx:
            self.broker.read("t1", g["grant_id"], "messages/thread/alice",
                             projection="conversation@1")
        self.assertIn("unknown resource", str(ctx.exception))

    def test_effect_on_nonexistent_target_refused_not_crash(self):
        self.broker.begin_task("t8", self.prompt_hash)
        g = self.broker.request_grant("t8", "messages/thread/thr-alice", "read",
                                      projection="conversation@1")
        out = self.broker.read("t8", g["grant_id"], "messages/thread/thr-alice",
                               projection="conversation@1")
        eg = self.broker.request_grant("t8", "notes/*", "update_note")
        self.assertTrue(eg["ok"])
        with self.assertRaises(RefusalError) as ctx:
            self.broker.effect(
                "t8", eg["grant_id"], "update_note",
                {"note_id": "note-does-not-exist", "body": "x"},
                derived_from=[
                    {"type": "projection",
                     "ref": out["projection_meta"]["content_hash"]},
                    {"type": "user_input", "ref": self.prompt_hash}])
        self.assertIn("unknown resource", str(ctx.exception))


class TestEffects(BrokerFixture):
    def _read_then_send(self):
        self.broker.begin_task("t8", self.prompt_hash)
        g = self.broker.request_grant("t8", "messages/thread/thr-alice", "read",
                                      projection="conversation@1")
        out = self.broker.read("t8", g["grant_id"], "messages/thread/thr-alice",
                               projection="conversation@1")
        eg = self.broker.request_grant("t8", "messages/thread/thr-alice",
                                       "send_message")
        return out, eg

    def test_effect_requires_own_grant(self):
        self.broker.begin_task("t8", self.prompt_hash)
        g = self.broker.request_grant("t8", "messages/thread/thr-alice", "read",
                                      projection="conversation@1")
        with self.assertRaises(RefusalError):
            self.broker.effect("t8", g["grant_id"], "send_message",
                               {"thread_id": "thr-alice", "body": "hi"},
                               derived_from=[{"type": "user_input",
                                              "ref": self.prompt_hash}])

    def test_effect_requires_provenance(self):
        _, eg = self._read_then_send()
        with self.assertRaises(RefusalError):
            self.broker.effect("t8", eg["grant_id"], "send_message",
                               {"thread_id": "thr-alice", "body": "hi"},
                               derived_from=[])

    def test_effect_rejects_unknown_projection_ref(self):
        _, eg = self._read_then_send()
        with self.assertRaises(RefusalError):
            self.broker.effect("t8", eg["grant_id"], "send_message",
                               {"thread_id": "thr-alice", "body": "hi"},
                               derived_from=[{"type": "projection", "ref": "0" * 64}])

    def test_effect_happy_path_records_provenance(self):
        out, eg = self._read_then_send()
        proj_hash = out["projection_meta"]["content_hash"]
        res = self.broker.effect(
            "t8", eg["grant_id"], "send_message",
            {"thread_id": "thr-alice", "body": "Looks good, two comments inline."},
            derived_from=[{"type": "projection", "ref": proj_hash},
                          {"type": "user_input", "ref": self.prompt_hash}])
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.corpus.outbox), 1)
        self.assertEqual(self.corpus.outbox[0]["thread_id"], "thr-alice")
        eff = self.ledger.by_kind("effect")[0]
        refs = {d["type"]: d["ref"] for d in eff.detail["derived_from"]}
        self.assertEqual(refs["projection"], proj_hash)
        self.assertEqual(refs["user_input"], self.prompt_hash)

    def test_effect_rejects_wrong_prompt_ref(self):
        out, eg = self._read_then_send()
        proj_hash = out["projection_meta"]["content_hash"]
        with self.assertRaises(RefusalError):
            self.broker.effect("t8", eg["grant_id"], "send_message",
                               {"thread_id": "thr-alice", "body": "hi"},
                               derived_from=[{"type": "projection", "ref": proj_hash},
                                             {"type": "user_input", "ref": "f" * 64}])


if __name__ == "__main__":
    unittest.main()
