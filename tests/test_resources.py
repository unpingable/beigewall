"""Phase 6B: resource budgets are trusted state; authority is unchanged."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from beigewall.authority import PolicyAuthority
from beigewall.broker import CapabilityBroker, RefusalError, effect_resource
from beigewall.corpus import build_demo_corpus
from beigewall.receipts import Ledger
from beigewall.resources import ResourceConfigError, ResourceLedger, ResourcePolicy
from beigewall.tasks import LogicalClock, experiment_policy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPolicyValidation(unittest.TestCase):
    def test_bounds_must_be_positive_finite_ints(self):
        for bad in ({"effect_count": 0}, {"effect_count": -1}, {"effect_count": True},
                    {"task_effects": 1.5}, {"rate_n": 3}, {"rate_n": 3, "rate_seconds": 0},
                    {"rate_n": 3, "rate_seconds": float("nan")},
                    {"rate_n": 3, "rate_seconds": float("inf")}):
            with self.assertRaises(ResourceConfigError, msg=bad):
                ResourcePolicy(**bad)
        with self.assertRaises(ResourceConfigError):
            ResourcePolicy.parse(rate="3/abc")
        with self.assertRaises(ResourceConfigError):
            ResourcePolicy.parse(rate="3/-1")
        self.assertFalse(ResourcePolicy().bounded)
        p = ResourcePolicy.parse(3, 4, "3/10")
        self.assertTrue(p.bounded)
        self.assertEqual(p.to_dict(), {"effect_count": 3, "task_effects": 4,
                                       "rate": {"n": 3, "seconds": 10.0}})

    def test_ledger_counts_and_invariant(self):
        t = [100.0]
        led = ResourceLedger(ResourcePolicy(effect_count=2, task_effects=3, rate_n=2,
                                            rate_seconds=5.0), clock=lambda: t[0])
        key = ("t8", "send_message", "messages/thread/thr-alice")
        self.assertIsNone(led.check(*key))
        led.consume(*key); led.consume(*key)
        self.assertEqual(led.check(*key)["budget"], {"scope": "task-effect-resource",
                                                      "consumed": 2, "cap": 2})
        other = ("t8", "send_message", "messages/thread/thr-group")   # same effect, other target
        self.assertEqual(led.check(*other)["budget"]["scope"], "rate")  # 2 sends in window
        t[0] += 6.0
        self.assertIsNone(led.check(*other))
        led.consume(*other)
        self.assertEqual(led.check(*other)["budget"]["scope"], "task")     # 3 total
        led.consumed[key] = 99
        with self.assertRaises(AssertionError):
            led.assert_invariant()


def fixture(policy=None):
    clock = LogicalClock()
    corpus = build_demo_corpus()
    ledger = Ledger(clock=clock.now)
    authority = PolicyAuthority(experiment_policy(), clock=clock.now)
    res = ResourceLedger(policy) if policy else None
    broker = CapabilityBroker(corpus, authority, ledger, clock=clock.now, resources=res)
    broker.begin_task("t8-send-reply", "p-hash")
    g = broker.request_grant("t8-send-reply", "messages/thread/thr-alice", "read",
                             projection="conversation@1")["grant_id"]
    h = broker.read("t8-send-reply", g, "messages/thread/thr-alice",
                    "conversation@1")["projection_meta"]["content_hash"]
    prov = [{"type": "projection", "ref": h}, {"type": "user_input", "ref": "p-hash"}]
    return corpus, ledger, broker, prov


def send(broker, prov, i):
    gs = broker.request_grant("t8-send-reply", "messages/thread/thr-alice",
                              "send_message")["grant_id"]
    try:
        broker.effect("t8-send-reply", gs, "send_message",
                      {"thread_id": "thr-alice", "body": f"s{i}"}, prov)
        return "ok"
    except RefusalError as exc:
        return str(exc)


class TestBrokerBudget(unittest.TestCase):
    def test_fresh_grants_do_not_bypass_task_scoped_budget(self):
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=3))
        outcomes = [send(broker, prov, i) for i in range(10)]
        self.assertEqual(outcomes[:3], ["ok"] * 3)
        self.assertTrue(all("effect budget exhausted" in o for o in outcomes[3:]))
        self.assertEqual(len(corpus.outbox), 3)
        rows = [r.to_dict() for r in ledger.all()]
        self.assertEqual(sum(1 for r in rows if r["kind"] == "effect"), 3)
        ref = [r for r in rows if r["kind"] == "refusal" and "budget exhausted" in r["reason"]]
        self.assertEqual(len(ref), 7)
        self.assertEqual(ref[0]["budget"], {"scope": "task-effect-resource", "consumed": 3,
                                            "cap": 3})
        # every effect kept its provenance and its own grant
        self.assertTrue(all(r["derived_from"] == prov for r in rows if r["kind"] == "effect"))
        # the unbounded broker lands all ten (the phase 6 control)
        corpus2, _, broker2, prov2 = fixture()
        self.assertEqual([send(broker2, prov2, i) for i in range(10)], ["ok"] * 10)
        self.assertEqual(len(corpus2.outbox), 10)

    def test_budget_refusal_does_not_consume_grant_or_widen_authority(self):
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=1))
        self.assertEqual(send(broker, prov, 0), "ok")
        gs = broker.request_grant("t8-send-reply", "messages/thread/thr-alice",
                                  "send_message")["grant_id"]
        with self.assertRaises(RefusalError):
            broker.effect("t8-send-reply", gs, "send_message",
                          {"thread_id": "thr-alice", "body": "x"}, prov)
        self.assertEqual(broker._grants[gs].effects_used, 0)   # not consumed by the refusal
        # an effect outside the grant's scope is still an authority refusal first
        with self.assertRaises(RefusalError) as cm:
            broker.effect("t8-send-reply", gs, "send_message",
                          {"thread_id": "thr-group", "body": "x"}, prov)
        self.assertIn("outside scope", str(cm.exception))
        # and fabricated provenance is still refused before any budget check
        with self.assertRaises(RefusalError) as cm:
            broker.effect("t8-send-reply", gs, "send_message",
                          {"thread_id": "thr-alice", "body": "x"},
                          [{"type": "projection", "ref": "0" * 64}])
        self.assertIn("never disclosed", str(cm.exception))

    def test_task_total_and_rebuild_from_receipts(self):
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=5, task_effects=2))
        self.assertEqual([send(broker, prov, i) for i in range(3)][2:],
                         ["task effect budget exhausted (2/2)"])
        rows = [r.to_dict() for r in ledger.all()]
        rebuilt = ResourceLedger(ResourcePolicy(effect_count=5, task_effects=2))
        self.assertEqual(rebuilt.rebuild(rows, effect_resource), 2)
        self.assertEqual(rebuilt.task_total["t8-send-reply"], 2)
        self.assertIsNotNone(rebuilt.check("t8-send-reply", "send_message",
                                           "messages/thread/thr-alice"))


class TestServerRestartKeepsConsumption(unittest.TestCase):
    def test_restart_rebuilds_counts_and_policy_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sock"), 0o700)
            sock, ledger = os.path.join(d, "sock", "bw.sock"), os.path.join(d, "receipts.jsonl")
            cmd = [sys.executable, "-m", "beigewall.ipc.server", "--socket", sock, "--ledger",
                   ledger, "--condition", "control", "--tasks", "t8-send-reply,t11-fabricated-provenance",
                   "--effect-count", "2", "--task-effects", "3"]
            srv = subprocess.Popen(cmd, cwd=ROOT)
            try:
                for _ in range(100):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.05)
                from beigewall.ipc.client import RemoteBroker, RefusalError as RRefusal
                b = RemoteBroker(sock)
                pr = b.begin_task("t8-send-reply")
                g = b.request_grant("t8-send-reply", "messages/thread/thr-alice", "read",
                                    projection="conversation@1")["grant_id"]
                h = b.read("t8-send-reply", g, "messages/thread/thr-alice",
                           "conversation@1")["projection_meta"]["content_hash"]
                prov = [{"type": "projection", "ref": h}, {"type": "user_input", "ref": pr["prompt_hash"]}]
                landed = 0
                for i in range(5):
                    gs = b.request_grant("t8-send-reply", "messages/thread/thr-alice",
                                         "send_message")["grant_id"]
                    try:
                        b.effect("t8-send-reply", gs, "send_message",
                                 {"thread_id": "thr-alice", "body": f"s{i}"}, prov)
                        landed += 1
                    except RRefusal:
                        pass
                self.assertEqual(landed, 2)
                b.close()
            finally:
                srv.terminate(); srv.wait(timeout=10)
            rows = [json.loads(l) for l in open(ledger)]
            self.assertEqual(sum(1 for r in rows if r["kind"] == "effect"), 2)
            pol = [r for r in rows if r["kind"] == "resource_policy"]
            self.assertEqual(pol[0]["effect_count"], 2)
            srv = subprocess.Popen(cmd, cwd=ROOT)
            try:
                for _ in range(100):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.05)
                rows = [json.loads(l) for l in open(ledger)]
                pol2 = [r for r in rows if r["kind"] == "resource_policy"][-1]
                self.assertEqual(pol2["rebuilt_consumed"]["task_total"], {"t8-send-reply": 2})
                from beigewall.lifecycle_metrics import disagreements
                self.assertEqual(disagreements(rows), [])
            finally:
                srv.terminate(); srv.wait(timeout=10)

    def test_malformed_budget_configuration_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            for flags in (["--effect-count", "0"], ["--effect-count", "-3"],
                          ["--rate", "3/0"], ["--rate", "nan/10"], ["--rate", "3/inf"]):
                out = subprocess.run([sys.executable, "-m", "beigewall.ipc.server", "--socket",
                                      os.path.join(d, "s"), "--ledger", os.path.join(d, "l"),
                                      "--condition", "control"] + flags, cwd=ROOT,
                                     capture_output=True, text=True, timeout=10)
                self.assertNotEqual(out.returncode, 0, flags)
                self.assertIn("invalid resource policy", out.stderr + out.stdout, flags)


if __name__ == "__main__":
    unittest.main()
