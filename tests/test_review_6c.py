"""Hostile review of phase 6C (evidence durability): reproducers, now regressions.

Each test is named after a finding (F1..F12). Written by the review as
failing demonstrations against d85d200; F1-F9, F11 and F12 were fixed and
the tests now assert the fail-closed behaviour. F10 remains a demonstration
of the same-UID limitation (an honest ledger file is assumed; phase 7).
F6 is kept with the conservative outcome documented in FINDINGS_PHASE6C.md.
"""

import fcntl
import json
import os
import shutil
import tempfile
import unittest

from blackwall.ipc import protocol as P
from blackwall.ipc.server import TrustedServer, unresolved_intents
from blackwall.receipts import Ledger, LedgerError
from blackwall.resources import ResourceLedger, ResourcePolicy
from blackwall.broker import effect_resource

from tests.test_evidence import _Srv, _kinds, _req, _dispatch, T8, T11, THR, POLICY


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bw-rv6c-")
        self.addCleanup(shutil.rmtree, self.d, True)


class F1RecoverRewriteWindow(_Base):
    """DEFECT (expected FAIL). _load_prior rewrites the ledger with
    open(path, "wb"): the file is truncated to zero before the complete rows
    are written back. A crash in that window destroys every prior row; the
    next start sees an empty ledger and resets seq, grant ids, effect ids,
    task closure and resource consumption."""

    def test_crash_during_recover_rewrite_keeps_complete_rows(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        self.assertTrue(a.send(T8, a.grant(T8), prov)["ok"])
        a.stop()
        with open(a.path, "a", encoding="utf-8") as fh:
            fh.write('{"kind": "ipc_conn')                 # torn tail from a crash
        n_complete = len(open(a.path, encoding="utf-8").read().splitlines()) - 1

        class Crash(BaseException):
            pass
        real_truncate = os.truncate

        def crashing_truncate(path, length):
            real_truncate(path, length)                   # the only rewrite step
            raise Crash()                                 # process dies right after
        os.truncate = crashing_truncate
        try:
            with self.assertRaises(Crash):
                _Srv(self.d, recover=True)
        finally:
            os.truncate = real_truncate
        # the lock file of the dead process: release it as the kernel would
        rows_left = len(Ledger.existing_rows(a.path))
        self.assertEqual(rows_left, n_complete,
                         "recovery truncated the ledger; complete evidence lost")


class F2ExpiredGrantHidesPriorEffect(_Base):
    """DEFECT (expected FAIL). prior_effect is attached only when the refusal
    text is 'effect grant consumed'. validate_use checks revoked/expired
    first, so a retry after a lost response on a grant that has since
    expired is refused 'expired' with NO prior_effect; the client re-raises
    it as a plain refusal, the agent cannot tell the effect landed, and a
    fresh grant lands a duplicate."""

    def test_retry_on_expired_consumed_grant_still_names_the_prior_effect(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.h("request_grant", T8, {"resource": THR, "operation": "send_message",
                                        "ttl": 5})["result"]["grant_id"]
        first = a.send(T8, gid, prov)                    # response "lost"
        self.assertTrue(first["ok"])
        retry = a.send(T8, gid, prov)                    # client's retry-once
        self.assertIn("expired", retry["error"]["message"])
        self.assertIsNotNone(retry["error"].get("prior_effect"),
                             "retry on an expired grant hides that its effect committed")
        a.stop()

    def test_agent_then_duplicates_on_a_fresh_grant(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.h("request_grant", T8, {"resource": THR, "operation": "send_message",
                                        "ttl": 5})["result"]["grant_id"]
        a.send(T8, gid, prov)                            # lands; response lost
        retry = a.send(T8, gid, prov)                    # 'expired', no prior_effect
        if retry["error"].get("prior_effect") is None:   # what the agent sees
            a.send(T8, a.grant(T8), prov)                # reasonable recovery: re-request
        self.assertEqual(retry["error"]["prior_effect"]["status"], "committed")
        self.assertEqual(len(_kinds(a.rows(), "effect")), 1,
                         "one intended effect landed twice after a lost response")
        a.stop()


class F3MissingFinalNewlineWedges(_Base):
    """DEFECT (expected FAIL). A crash that writes a complete JSON row but
    not its trailing newline is accepted as a complete row (no --recover
    needed). The next append is glued onto the same line, and from then on
    the ledger is 'corrupt beyond its last line': no restart, even with
    --recover, ever succeeds."""

    def test_complete_row_without_newline_does_not_wedge_restart(self):
        a = _Srv(self.d)
        a.begin_and_prov(T8)
        a.stop()
        raw = open(a.path, "rb").read()
        with open(a.path, "wb") as fh:
            fh.write(raw[:-1])                           # crash before the "\n"
        with self.assertRaises(ValueError):              # torn: never accepted silently
            _Srv(self.d)
        n = len(raw.splitlines())
        c = _Srv(self.d, recover=True)                   # the row is kept, terminated
        rs = _kinds(c.rows(), "trusted_restart")[-1]
        self.assertEqual(rs["torn_tail_recovered"]["action"], "terminated")
        self.assertEqual(len(c.rows()), n + 2)           # + trusted_restart + resource_policy
        c.stop()
        d = _Srv(self.d)                                 # and the ledger is sound again
        d.stop()


class F4AbortWriteFailure(_Base):
    """DEFECT (expected FAIL). When the handler fails and the durable ABORT
    row cannot be written, LedgerError (not EvidenceError/RefusalError)
    escapes broker.effect and server.handle; `degraded` is never set, the
    client gets no 'evidence'/'indeterminate' code (over the socket the
    connection thread dies in its own except handler), and restart records
    an effect that provably did not occur as indeterminate."""

    def test_abort_write_failure_is_an_explicit_outcome(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        a.fail_sink_on("refusal")
        try:
            r = a.send(T8, gid, prov, body=False)        # handler KeyError -> abort
        except LedgerError as exc:
            r = {"escaped": repr(exc)}
        self.assertEqual(a.s.degraded, "ledger_failed", f"degraded not set; got {r}")
        self.assertIn(r.get("error", {}).get("code"), ("evidence", "refusal"))
        a.stop()


class F5DegradedOnlyForEffects(_Base):
    """DEFECT (expected FAIL). A ledger failure on any non-effect row fails
    the ledger but leaves `degraded` None: hello keeps advertising a healthy
    server, and begin_task (control mode records nothing) still mutates
    server state on a failed ledger."""

    def test_read_ledger_failure_sets_degraded(self):
        a = _Srv(self.d)
        a.begin_and_prov(T8)
        g = a.h("request_grant", T8, {"resource": THR, "operation": "read",
                                      "projection": "conversation@1"})["result"]["grant_id"]
        a.fail_sink_on("read")
        r = a.h("read", T8, {"grant_id": g, "resource": THR, "projection": "conversation@1"})
        self.assertEqual(r["error"]["message"], "ledger_failed")
        self.assertEqual(a.h("begin_task", T11, {})["error"]["message"], "ledger_failed")
        hello = a.h("hello", "-", {})["result"]
        self.assertEqual(hello["degraded"], "ledger_failed",
                         "ledger failed but hello reports a healthy server")
        a.stop()


class F6IntentFsyncFailure(_Base):
    """ACCEPTED, conservative direction (documented). If the intent's
    write() lands but fsync() fails, the client is told the effect did NOT
    occur — which is certain: the handler runs only after the record call
    returned — while the row may be on disk; a restart then marks it
    indeterminate and counts it. The record over-counts an effect that did
    not happen; it never under-counts one that did."""

    def test_evidence_error_and_durable_record_agree(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        real = os.fsync
        calls = {"n": 0}

        def bad_fsync(fd):
            raise OSError(5, "EIO")
        os.fsync = bad_fsync
        try:
            r = a.send(T8, gid, prov)
        finally:
            os.fsync = real
        self.assertEqual(r["error"]["code"], "evidence")      # "did NOT occur" (true)
        self.assertEqual(len(a.s.corpus.outbox), 0)
        a.stop()
        b = _Srv(self.d)
        marks = _kinds(b.rows(), "effect_indeterminate")
        b.stop()
        self.assertEqual([m["effect_id"] for m in marks], ["e-1"])   # conservative


class F7UnexpectedHandlerException(_Base):
    """DEFECT, latent (expected FAIL). Only KeyError/EffectError abort the
    intent. Any other exception (TypeError from an unhashable id under a
    future wildcard policy, MemoryError, a handler bug) leaves the intent
    OPEN in a LIVE server that keeps serving: the grant is not consumed,
    live consumption is not advanced, and a retry on the same grant lands a
    second intent. The live state is in none of the three states, and the
    observer reports the open intent as agree=True ('pending recovery')."""

    def test_unexpected_handler_error_resolves_the_intent(self):
        from blackwall import effects
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        real = effects.EFFECTS["send_message"]
        effects.EFFECTS["send_message"] = lambda c, p: (_ for _ in ()).throw(TypeError("bug"))
        try:
            r = a.send(T8, gid, prov)
        finally:
            effects.EFFECTS["send_message"] = real
        self.assertEqual(r["error"]["code"], "indeterminate")
        self.assertIn("handler_exception:TypeError", r["error"]["message"])
        self.assertEqual(a.s.degraded, "ledger_failed")
        self.assertEqual(a.s.resources.task_total.get(T8, 0), 1)     # consumed
        r2 = a.send(T8, gid, prov, 1)                    # nothing further is served
        self.assertEqual(r2["error"]["message"], "ledger_failed")
        a.stop()
        b = _Srv(self.d)                                 # recovery marks it
        self.assertEqual([m["effect_id"] for m in _kinds(b.rows(), "effect_indeterminate")],
                         ["e-1"])
        b.stop()


class F8NonDurableRowsLost(_Base):
    """DEFECT (expected FAIL). Only intent/commit/abort/marker rows are
    fsynced. Power loss keeps at most the prefix up to the last fsync. A
    read's disclosure (the response was already sent: write-before-visible
    is page-cache-before-visible) and the task's closure vanish; the task
    reopens and the evidence history has no record of the disclosure."""

    def test_disclosed_read_survives_power_loss(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)                      # read response delivered
        a.stop()
        # power loss: nothing after the last fsync survives; no fsync happened
        from blackwall.ipc.server import DURABLE_KINDS
        rows = Ledger.existing_rows(a.path)
        last_durable = max(i for i, r in enumerate(rows) if r["kind"] in DURABLE_KINDS)
        self.assertEqual(rows[last_durable]["kind"], "read")
        with open(a.path, "w", encoding="utf-8") as fh:
            for r in rows[:last_durable + 1]:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        b = _Srv(self.d)
        reopened = b.h("begin_task", T8, {})
        b.stop()
        self.assertFalse(reopened["ok"], "task with a delivered disclosure reopened after "
                                         "power loss; the read receipt was never durable")


class F9LockBypassedBySecondPath(_Base):
    """DEFECT, low (expected FAIL). The exclusive-owner flock is on
    `ledger_path + '.lock'`, not on the ledger inode. A second server given
    the same ledger through another path (symlink, hard link, bind mount)
    takes a different lock and both append: colliding seq, grant and effect
    ids."""

    def test_second_server_via_symlink_refused(self):
        a = _Srv(self.d)
        os.symlink(a.path, os.path.join(self.d, "alias.jsonl"))
        try:
            b = TrustedServer(os.path.join(self.d, "s2"), os.path.join(self.d, "alias.jsonl"),
                              "control", tasks=[T8, T11], resource_policy=POLICY)
        except SystemExit:
            a.stop()
            return
        fcntl.flock(b._lock_fh, fcntl.LOCK_UN)
        a.stop()
        self.fail("two trusted servers own the same ledger")


class F10ForgedRowsSteerRecovery(_Base):
    """DEMONSTRATION, kept: the evidence invariant assumes an honest
    ledger file (same-UID limitation; phase 7). Anything that can append a
    complete row steers recovery. A forged abort for an open intent makes
    the restart record no marker, the rebuild release the budget, and the
    observer agree (no disagreement)."""

    def test_forged_abort_releases_indeterminate_effect(self):
        a = _Srv(self.d, policy=ResourcePolicy(effect_count=1))
        prov = a.begin_and_prov(T8)
        a.fail_sink_on("effect")
        a.send(T8, a.grant(T8), prov)                    # handler ran; indeterminate
        a.stop()
        intent = _kinds(a.rows(), "effect_intent")[0]
        last = Ledger.existing_rows(a.path)[-1]
        forged = {"kind": "refusal", "task_id": T8, "operation": "send_message",
                  "grant_id": intent["grant_id"], "resource": THR,
                  "reason": "effect target not available", "effect_id": intent["effect_id"],
                  "seq": last["seq"] + 1, "receipt_id": f"r-{last['seq'] + 1}",
                  "ts": last["ts"] + 1}
        with open(a.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(forged, sort_keys=True) + "\n")
        b = _Srv(self.d, policy=ResourcePolicy(effect_count=1))
        b.stop()
        rows = b.rows()
        from blackwall.lifecycle_metrics import disagreements
        self.assertEqual(_kinds(rows, "effect_indeterminate"), [])
        self.assertEqual(_kinds(rows, "resource_policy")[-1]["rebuilt_consumed"]["task_total"]
                         .get(T8, 0), 0)
        self.assertEqual(disagreements(rows), [])        # observer agrees with the lie


class F11RebuildVsObserver(_Base):
    """FIXED. Trusted rebuild and observer used different abort
    semantics; both are now order-aware per intent. rebuild: any refusal with the effect_id anywhere in the
    ledger un-counts EVERY intent with that id, order-free. observer: an
    abort counts only if it follows an open intent. So on (a) an abort after
    an indeterminate marker and (b) duplicate effect ids, the trusted rebuild
    concludes MORE authority remains than the observer does."""

    def _rebuild(self, rows):
        rl = ResourceLedger(ResourcePolicy(effect_count=5))
        rl.rebuild(rows, effect_resource)
        return rl.task_total.get(T8, 0)

    def _observer(self, rows):
        from blackwall.lifecycle_metrics import _Grants
        g = _Grants()
        for r in sorted(rows, key=lambda r: r["seq"]):
            g.apply(r)
        return g.task_total.get(T8, 0)

    def test_abort_after_marker_and_duplicate_id(self):
        it = {"kind": "effect_intent", "task_id": T8, "effect_id": "e-1", "grant_id": "g-1",
              "resource": THR, "effect": {"name": "send_message",
                                          "params": {"thread_id": "thr-alice"}},
              "seq": 1, "receipt_id": "r-1", "ts": 1.0}
        mark = {"kind": "effect_indeterminate", "task_id": T8, "effect_id": "e-1",
                "grant_id": "g-1", "seq": 2, "receipt_id": "r-2", "ts": 2.0}
        abort = {"kind": "refusal", "task_id": T8, "effect_id": "e-1", "grant_id": "g-1",
                 "operation": "send_message", "resource": THR, "reason": "x",
                 "seq": 3, "receipt_id": "r-3", "ts": 3.0}
        rows = [it, mark, abort]
        self.assertEqual((self._rebuild(rows), self._observer(rows)), (1, 1))
        it2 = dict(it, seq=4, receipt_id="r-4", ts=4.0, grant_id="g-2")
        commit2 = {"kind": "effect", "task_id": T8, "effect_id": "e-1", "grant_id": "g-2",
                   "effect": it["effect"], "seq": 5, "receipt_id": "r-5", "ts": 5.0}
        rows = [it, dict(abort, seq=2, receipt_id="r-2", ts=2.0), it2, commit2]
        with self.assertRaises(ValueError):              # duplicate id: refuse to start
            self._rebuild(rows)
        self.assertEqual(self._observer(rows), 1)
        # an abort from another grant does not release the intent
        rows = [it, dict(abort, grant_id="g-7")]
        self.assertEqual((self._rebuild(rows), self._observer(rows)), (1, 1))


class F12EquivalenceFilterHidesOpenIntent(_Base):
    """FIXED. The equivalence filter drops effect_intent rows and
    effect_id keys, and reconstruct() reports an open intent as agree=True,
    so a server run that left an intent unresolved is indistinguishable,
    under the test's two checks, from one that never attempted the effect."""

    def test_open_intent_invisible_to_equivalence_checks(self):
        from tests.test_ipc_equivalence import strip
        from blackwall.lifecycle_metrics import disagreements
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.stop()
        base = a.rows()
        last = base[-1]
        extra = base + [{"kind": "effect_intent", "task_id": T8, "effect_id": "e-1",
                         "grant_id": "g-9", "resource": THR,
                         "effect": {"name": "send_message", "params": {}},
                         "derived_from": prov, "seq": last["seq"] + 1,
                         "receipt_id": "r-x", "ts": last["ts"] + 1}]
        from tests.test_ipc_equivalence import intents_resolved
        self.assertEqual(strip(extra), strip(base))      # the filter still hides it ...
        self.assertEqual(disagreements(extra), [])       # ... and it is pending, not wrong
        self.assertTrue(intents_resolved(base))
        self.assertFalse(intents_resolved(extra))        # ... so equivalence checks this too


if __name__ == "__main__":
    unittest.main()
