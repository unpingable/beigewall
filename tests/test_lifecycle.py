"""Lifecycle semantics (phase 5B): deterministic demonstrations against the
broker. The duplicate-effect test documents a defect found by code reading
(an effect grant had no use bound); it is written to fail on the old
behaviour and pass once effect grants are consumed on success."""

import unittest

from blackwall.authority import PolicyAuthority
from blackwall.broker import CapabilityBroker, RefusalError
from blackwall.corpus import build_demo_corpus
from blackwall.receipts import Ledger
from blackwall.tasks import LogicalClock, experiment_policy


def fixture():
    clock = LogicalClock()
    corpus = build_demo_corpus()
    ledger = Ledger(clock=clock.now)
    authority = PolicyAuthority(experiment_policy(), clock=clock.now)
    broker = CapabilityBroker(corpus, authority, ledger, clock=clock.now)
    return clock, corpus, ledger, authority, broker


def _prepare_send(broker, task_id="t8-send-reply"):
    """Read the thread (for provenance) and mint a send grant."""
    broker.begin_task(task_id, "p-hash")
    g = broker.request_grant(task_id, "messages/thread/thr-alice", "read",
                             projection="conversation@1")["grant_id"]
    res = broker.read(task_id, g, "messages/thread/thr-alice", "conversation@1")
    gs = broker.request_grant(task_id, "messages/thread/thr-alice",
                              "send_message")["grant_id"]
    prov = [{"type": "projection", "ref": res["projection_meta"]["content_hash"]},
            {"type": "user_input", "ref": "p-hash"}]
    return gs, prov


class TestEffectGrantConsumption(unittest.TestCase):
    def test_retry_of_a_landed_effect_does_not_land_twice(self):
        clock, corpus, ledger, _, broker = fixture()
        gs, prov = _prepare_send(broker)
        params = {"thread_id": "thr-alice", "body": "thanks"}
        first = broker.effect("t8-send-reply", gs, "send_message", params, prov)
        self.assertTrue(first["ok"])
        # the agent never saw the result (transport loss) and retries
        with self.assertRaises(RefusalError) as cm:
            broker.effect("t8-send-reply", gs, "send_message", params, prov)
        self.assertIn("effect grant consumed", str(cm.exception))
        self.assertEqual(len(corpus.outbox), 1)
        kinds = [r.kind for r in ledger.by_task("t8-send-reply")]
        self.assertEqual(kinds.count("effect"), 1)
        self.assertEqual(kinds[-1], "refusal")

    def test_refused_attempt_does_not_consume_the_grant(self):
        # canonical t11 path: fabricated provenance refused, then the same
        # grant succeeds with real provenance
        clock, corpus, ledger, _, broker = fixture()
        gs, prov = _prepare_send(broker, "t11-fabricated-provenance")
        bad = [{"type": "projection", "ref": "0" * 64},
               {"type": "user_input", "ref": "p-hash"}]
        with self.assertRaises(RefusalError):
            broker.effect("t11-fabricated-provenance", gs, "send_message",
                          {"thread_id": "thr-alice", "body": "thanks"}, bad)
        ok = broker.effect("t11-fabricated-provenance", gs, "send_message",
                           {"thread_id": "thr-alice", "body": "thanks"}, prov)
        self.assertTrue(ok["ok"])
        self.assertEqual(len(corpus.outbox), 1)


class TestRevocation(unittest.TestCase):
    def test_revoked_grant_is_refused_and_receipted(self):
        clock, corpus, ledger, _, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        g = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                 "read", projection="conversation@1")["grant_id"]
        broker.revoke("t1-summarize-alice", g, reason="operator revoked")
        with self.assertRaises(RefusalError) as cm:
            broker.read("t1-summarize-alice", g, "messages/thread/thr-alice",
                        "conversation@1")
        self.assertIn("revoked", str(cm.exception))
        rows = [r.to_dict() for r in ledger.by_task("t1-summarize-alice")]
        self.assertEqual([r["kind"] for r in rows],
                         ["grant_request", "grant_mint", "grant_revoke", "refusal"])
        self.assertEqual(rows[2]["grant_id"], g)
        self.assertEqual(rows[2]["reason"], "operator revoked")
        # revoking an unknown or other-task grant is itself refused
        with self.assertRaises(RefusalError):
            broker.revoke("t2-group-thread-resolution", g, reason="x")
        with self.assertRaises(RefusalError):
            broker.revoke("t1-summarize-alice", "g-999", reason="x")
        # revocation never restores or widens anything: a fresh request
        # goes through policy as usual
        g2 = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                  "read", projection="conversation@1")["grant_id"]
        self.assertNotEqual(g2, g)
        self.assertTrue(broker.read("t1-summarize-alice", g2,
                                    "messages/thread/thr-alice", "conversation@1")["ok"])


class TestExpiryAndCrossTask(unittest.TestCase):
    def test_expired_after_time_jump_and_other_task_grant_unknown(self):
        clock, corpus, ledger, _, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        g = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                 "read", projection="conversation@1")["grant_id"]
        clock.t += 700  # past DEFAULT_TTL (600)
        with self.assertRaises(RefusalError) as cm:
            broker.read("t1-summarize-alice", g, "messages/thread/thr-alice",
                        "conversation@1")
        self.assertIn("expired", str(cm.exception))
        broker.begin_task("t7-version-pin", "h2")
        g2 = broker.request_grant("t7-version-pin", "messages/thread/thr-alice",
                                  "read", projection="conversation@1")["grant_id"]
        broker.begin_task("t3-note-followup", "h3")
        with self.assertRaises(RefusalError) as cm:
            broker.read("t3-note-followup", g2, "messages/thread/thr-alice",
                        "conversation@1")
        self.assertIn("unknown grant", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
