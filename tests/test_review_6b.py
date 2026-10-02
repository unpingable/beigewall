"""Hostile review of phase 6B resource budgets: reproducers, now regressions.

Written by the review as failing demonstrations (F1-F9) and confirmed
non-findings (N1-N4); each asserts the fail-closed property the design
claims. F1, F2, F3, F4, F5, F6, F8 and F9 were fixed; F7 is kept as a
documented limitation with its assertion narrowed to the fail-closed
direction (see FINDINGS_PHASE6B.md).
"""

import fcntl
import os
import shutil
import tempfile
import unittest

from beigewall.broker import RefusalError
from beigewall.budget_metrics import task_budget_outcome
from beigewall.ipc.server import TrustedServer
from beigewall.lifecycle_metrics import disagreements, reconstruct
from beigewall.resources import ResourceConfigError, ResourceLedger, ResourcePolicy

from tests.test_resources import fixture, send

T8, T11 = "t8-send-reply", "t11-fabricated-provenance"
THR = "messages/thread/thr-alice"


def _req(op, task, args, n=[0]):
    n[0] += 1
    return {"v": 1, "id": f"r{n[0]}", "op": op, "task_id": task, "args": args}


class _Srv:
    """TrustedServer driven through handle() (the same dispatch the socket
    path uses under its lock), no socket bound."""

    def __init__(self, d, policy, tasks):
        self.s = TrustedServer(os.path.join(d, "s"), os.path.join(d, "l.jsonl"),
                               "control", tasks=tasks, resource_policy=policy)

    def h(self, op, task, args):
        return self.s.handle(_req(op, task, args))

    def begin_and_prov(self, task):
        pr = self.h("begin_task", task, {})["result"]["prompt_hash"]
        g = self.h("request_grant", task, {"resource": THR, "operation": "read",
                                           "projection": "conversation@1"})["result"]["grant_id"]
        h = self.h("read", task, {"grant_id": g, "resource": THR,
                                  "projection": "conversation@1"})["result"]["projection_meta"]["content_hash"]
        return [{"type": "projection", "ref": h}, {"type": "user_input", "ref": pr}]

    def grant(self, task):
        return self.h("request_grant", task, {"resource": THR,
                                              "operation": "send_message"})["result"]["grant_id"]

    def send(self, task, gid, prov, i=0):
        return self.h("effect", task, {"grant_id": gid, "effect_name": "send_message",
                                       "params": {"thread_id": "thr-alice", "body": f"b{i}"},
                                       "derived_from": prov})

    def effect_rows(self, task):
        return [r for r in self.s.ledger.existing_rows(self.s.ledger_path)
                if r["kind"] == "effect" and r["task_id"] == task]

    def stop(self):
        fcntl.flock(self.s._lock_fh, fcntl.LOCK_UN)
        self.s._lock_fh.close()


class ReviewRestart(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bw-rev6b-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _first_run(self, policy, n):
        a = _Srv(self.d, policy, [T8, T11])
        prov = a.begin_and_prov(T8)
        for i in range(n):
            a.send(T8, a.grant(T8), prov, i)
        landed = len(a.effect_rows(T8))
        a.stop()
        return landed

    def test_F1_restart_with_tighter_policy_lands_unreceipted_effect(self):
        """HIGH. Prior run unbounded (5 effects for t8). Restart with
        effect_count=3: rebuild puts t8 at 5 > 3; assert_invariant() scans
        ALL keys, so the first consume() for ANY other task raises AFTER
        the handler ran. Effect lands, no effect receipt, grant not
        consumed, counter advanced."""
        self.assertEqual(self._first_run(None, 5), 5)
        b = _Srv(self.d, ResourcePolicy(effect_count=3), [T8, T11])  # should refuse to start, or
        prov = b.begin_and_prov(T11)
        gid = b.grant(T11)
        outbox_before = len(b.s.corpus.outbox)
        try:
            b.send(T11, gid, prov)
        except AssertionError:
            pass   # serve_connection turns this into ipc_reject internal:AssertionError
        landed_world = len(b.s.corpus.outbox) - outbox_before
        receipted = len(b.effect_rows(T11))
        counter = b.s.resources.task_total.get(T11, 0)
        b.stop()
        # fail-closed expectation: whatever landed in the world is receipted
        # and counted exactly once
        self.assertEqual((landed_world, counter), (receipted, receipted),
                         f"world={landed_world} counter={counter} receipts={receipted}")
        self.assertEqual(receipted, 1)
        pol = [r for r in b.s.ledger.existing_rows(b.s.ledger_path)
               if r["kind"] == "resource_policy"][-1]
        self.assertEqual(pol["rebuilt_over_cap"]["consumed"],
                         {f"{T8}|send_message|{THR}": 5})

    def test_F1b_same_grant_lands_twice_after_invariant_crash(self):
        """The unreceipted effect also leaves grant.effects_used at 0, so
        the 'effect grant consumed' retry guarantee is lost."""
        self._first_run(None, 5)
        b = _Srv(self.d, ResourcePolicy(effect_count=3), [T8, T11])
        prov = b.begin_and_prov(T11)
        gid = b.grant(T11)
        before = len(b.s.corpus.outbox)
        for i in range(2):
            try:
                b.send(T11, gid, prov, i)
            except AssertionError:
                pass
        landed = len(b.s.corpus.outbox) - before
        b.stop()
        self.assertLessEqual(landed, 1, f"one grant landed {landed} effects")

    def test_F2_restart_without_policy_silently_drops_bounds(self):
        """MEDIUM. Prior run bounded (resource_policy row in the ledger);
        restart with no flags => unbounded, no receipt says so."""
        self._first_run(ResourcePolicy(effect_count=1, task_effects=1), 1)
        with self.assertRaises(SystemExit):      # no policy: dropped bounds
            _Srv(self.d, None, [T8, T11])
        with self.assertRaises(SystemExit):      # looser on one field
            _Srv(self.d, ResourcePolicy(effect_count=2, task_effects=1), [T8, T11])
        b = _Srv(self.d, ResourcePolicy(effect_count=1, task_effects=1), [T8, T11])
        self.assertEqual(b.s.resources.policy.task_effects, 1)   # equal: accepted
        b.stop()
        c = _Srv(self.d, ResourcePolicy(effect_count=1, task_effects=1,
                                        rate_n=1, rate_seconds=10.0), [T8, T11])
        c.stop()                                                   # stricter: accepted
        with self.assertRaises(SystemExit):      # then a restart without the rate
            _Srv(self.d, ResourcePolicy(effect_count=1, task_effects=1), [T8, T11])


class ReviewObserver(unittest.TestCase):
    POLICY_ROW = {"receipt_id": "r-0", "seq": 0, "ts": 0.0, "kind": "resource_policy",
                  "task_id": "", "effect_count": 1, "task_effects": None,
                  "rate": {"n": 1, "seconds": 10.0}}

    def _rows(self, ledger, policy_row=True):
        rows = [r.to_dict() for r in ledger.all()]
        return ([dict(self.POLICY_ROW)] if policy_row else []) + rows

    def test_F3_client_text_injects_rate_limited_state(self):
        """MEDIUM. The provenance refusal reason echoes the client's
        derived_from `type` via repr; the observer matches _BUDGET/_RATE by
        substring BEFORE _PROVENANCE, so a provenance refusal is labelled
        rate_limited and agreed with."""
        corpus, ledger, broker, prov = fixture()
        gs = broker.request_grant(T8, THR, "send_message")["grant_id"]
        with self.assertRaises(RefusalError):
            broker.effect(T8, gs, "send_message", {"thread_id": "thr-alice", "body": "x"},
                          [{"type": "effect rate exceeded", "ref": "x"}])
        obs = [o for o in reconstruct(self._rows(ledger)) if o["broker_outcome"] == "refused"]
        self.assertEqual(obs[-1]["observer_state"], "provenance", obs[-1])

    def test_F3b_client_text_forces_observer_disagreement(self):
        """Same injection with no policy row: the client manufactures an
        observer disagreement on a correct provenance refusal."""
        corpus, ledger, broker, prov = fixture()
        gs = broker.request_grant(T8, THR, "send_message")["grant_id"]
        with self.assertRaises(RefusalError):
            broker.effect(T8, gs, "send_message", {"thread_id": "thr-alice", "body": "x"},
                          [{"type": "task effect budget exhausted", "ref": "x"}])
        self.assertEqual(disagreements(self._rows(ledger, policy_row=False)), [])

    def test_F4_observer_never_flags_over_budget_success(self):
        """MEDIUM. The observer verifies budget refusals but never checks a
        SUCCESS against the policy row: 3 effects on a key capped at 1 are
        all 'agree'. The independent observer cannot detect the failure the
        mechanism exists to prevent."""
        corpus, ledger, broker, prov = fixture()          # broker unbounded (simulated bypass)
        for i in range(3):
            send(broker, prov, i)
        self.assertNotEqual(disagreements(self._rows(ledger)), [],
                            "over-budget successes not flagged")

    def test_F5_rate_refusal_agreed_without_evidence(self):
        """MEDIUM. A rate refusal is agreed iff a rate policy exists. A
        broker that rate-limits the very first effect (no prior success)
        is agreed with."""
        class AlwaysRate(ResourceLedger):
            def check(self, task_id, effect, resource):
                return {"reason": f"effect rate exceeded: {effect} (1 in 10s)",
                        "budget": {"scope": "rate", "consumed": 1, "cap": 1, "seconds": 10.0}}
        corpus, ledger, broker, prov = fixture()
        broker._resources = AlwaysRate(ResourcePolicy(rate_n=1, rate_seconds=10.0))
        self.assertIn("rate", send(broker, prov, 0))
        self.assertNotEqual(disagreements(self._rows(ledger)), [],
                            "wrong rate refusal agreed with")

    def test_F6_scope_mislabelled_budget_refusal_agreed(self):
        """LOW. Observer agrees with 'effect budget exhausted' (per-key
        scope) when only the TASK total is at cap; claimed scope, counts and
        the budget detail are never cross-checked."""
        class WrongScope(ResourceLedger):
            def check(self, task_id, effect, resource):
                if self.task_total.get(task_id, 0) >= 1:
                    return {"reason": f"effect budget exhausted: {effect} on {resource} (0/5)",
                            "budget": {"scope": "task-effect-resource", "consumed": 0, "cap": 5}}
        corpus, ledger, broker, prov = fixture()
        broker._resources = WrongScope(ResourcePolicy(effect_count=5, task_effects=1))
        send(broker, prov, 0)
        send(broker, prov, 1)
        row = dict(self.POLICY_ROW, effect_count=5, task_effects=1, rate=None)
        rows = [row] + [r.to_dict() for r in ledger.all()]
        self.assertNotEqual(disagreements(rows), [], "mislabelled scope agreed with")


class ReviewAccounting(unittest.TestCase):
    def test_F7_receipt_write_failure_after_consume_diverges(self):
        """LOW. consume() and effects_used advance before ledger.record();
        if the sink write fails the effect landed, the counter advanced and
        no receipt reaches disk (rebuild and every observer undercount)."""
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=3))
        real = ledger.record

        def failing(kind, task_id, **kw):
            if kind == "effect":
                raise OSError("ENOSPC")
            return real(kind, task_id, **kw)
        ledger.record = failing
        try:
            send(broker, prov, 0)
        except OSError:
            pass
        receipts = sum(1 for r in ledger.all() if r.kind == "effect")
        # accepted limitation: the world changed and the receipt write
        # failed; what must hold is the fail-closed direction — the counter
        # and the grant are never behind the world, so no further effect
        # is admitted on their account
        self.assertEqual(len(corpus.outbox), 1)
        self.assertEqual(receipts, 0)
        self.assertEqual(broker._resources.task_total.get(T8, 0), 1)
        self.assertGreaterEqual(broker._resources.task_total.get(T8, 0), len(corpus.outbox))

    def test_F8_empty_rate_string_is_silently_unbounded(self):
        """LOW. `--rate ''` parses to no rate (falsy check) instead of a
        configuration error; `--rate 3/1e-300` is accepted (a no-op rate)."""
        with self.assertRaises(ResourceConfigError):
            ResourcePolicy.parse(rate="")

    def test_F9_unknown_contract_classified_correctly_blocked(self):
        """LOW. A task with no contract has no required specs, so
        _all_matched([]) is True and any budget refusal is classified
        'correctly_blocked_over_budget' rather than 'unknown'."""
        rows = [{"kind": "refusal", "task_id": "t-unknown", "seq": 1,
                 "reason": "effect budget exhausted: send_message on x (3/3)"}]
        out = task_budget_outcome(rows, "t-unknown", contract=None, policy={})
        self.assertNotEqual(out["classification"], "correctly_blocked_over_budget")


class ReviewNonFindings(unittest.TestCase):
    """Attacks that did NOT break the mechanism (expected to pass)."""

    def test_N1_revoke_and_fresh_grants_do_not_renew(self):
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=2))
        self.assertEqual([send(broker, prov, i) for i in range(2)], ["ok", "ok"])
        g = broker.request_grant(T8, THR, "send_message")["grant_id"]
        broker.revoke(T8, g, "test")
        self.assertIn("budget exhausted", send(broker, prov, 9))
        self.assertEqual(len(corpus.outbox), 2)

    def test_N2_failed_target_does_not_consume_and_is_not_masked(self):
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=1))
        g = broker.request_grant(T8, THR, "send_message")["grant_id"]
        with self.assertRaises(RefusalError):        # missing body -> handler KeyError
            broker.effect(T8, g, "send_message", {"thread_id": "thr-alice"}, prov)
        self.assertEqual(broker._resources.task_total.get(T8, 0), 0)
        self.assertEqual(send(broker, prov, 1), "ok")

    def test_N3_rebuild_keys_match_live_keys(self):
        corpus, ledger, broker, prov = fixture(ResourcePolicy(effect_count=3, task_effects=4))
        for i in range(3):
            send(broker, prov, i)
        from beigewall.broker import effect_resource
        rb = ResourceLedger(ResourcePolicy(effect_count=3, task_effects=4))
        rb.rebuild([r.to_dict() for r in ledger.all()], effect_resource)
        self.assertEqual(rb.consumed, broker._resources.consumed)
        self.assertEqual(rb.task_total, broker._resources.task_total)

    def test_N4_corrupt_last_line_fails_closed_at_start(self):
        d = tempfile.mkdtemp(prefix="bw-rev6b-")
        self.addCleanup(shutil.rmtree, d, True)
        a = _Srv(d, ResourcePolicy(effect_count=1), [T8])
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov)
        a.stop()
        with open(os.path.join(d, "l.jsonl"), "a") as fh:
            fh.write('{"kind": "effect", "task_')
        with self.assertRaises(ValueError):
            _Srv(d, ResourcePolicy(effect_count=1), [T8])


if __name__ == "__main__":
    unittest.main()
