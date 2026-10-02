"""Phase 6C: evidence durability under controlled faults (docs/EVIDENCE_DURABILITY.md).

The trusted server is driven through handle() (the dispatch the socket path
uses under its lock) with a ledger whose sink can be made to fail at a
chosen row, and restarted over the resulting file. Every test asserts one
of the three recovery states for the effect in flight:

  1. did not occur, authority remains        (evidence unavailable / abort)
  2. occurred, durable authoritative record   (intent + commit)
  3. explicitly indeterminate, no duplication (intent, no commit -> marker)

and that trusted consumption is never behind the durable evidence.
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
from blackwall.resources import ResourcePolicy

T8, T11 = "t8-send-reply", "t11-fabricated-provenance"
THR = "messages/thread/thr-alice"
POLICY = ResourcePolicy(effect_count=3, task_effects=4)


def _req(op, task, args, n=[0]):
    n[0] += 1
    return {"v": 1, "id": f"r{n[0]}", "op": op, "task_id": task, "args": args}


def _dispatch(server, req):
    """handle() as the connection loop sees it: a protocol rejection is an
    error response, not an exception."""
    try:
        return server.handle(req)
    except P.ProtocolError as exc:
        return P.error_response(req["id"], "protocol", exc.reason)


class _Srv:
    def __init__(self, d, tasks=(T8, T11), policy=POLICY, recover=False):
        self.s = TrustedServer(os.path.join(d, "s"), os.path.join(d, "l.jsonl"), "control",
                               tasks=list(tasks), resource_policy=policy, recover=recover)
        self.path = os.path.join(d, "l.jsonl")

    def h(self, op, task, args):
        return _dispatch(self.s, _req(op, task, args))

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

    def send(self, task, gid, prov, i=0, body=True):
        params = {"thread_id": "thr-alice"}
        if body:
            params["body"] = f"b{i}"
        return self.h("effect", task, {"grant_id": gid, "effect_name": "send_message",
                                       "params": params, "derived_from": prov})

    def rows(self):
        return Ledger.existing_rows(self.path)

    def fail_sink_on(self, kind):
        """The next row of `kind` fails to reach the disk (ENOSPC)."""
        real = self.s.ledger._sink_write

        def failing(line, durable):
            if json.loads(line).get("kind") == kind:
                raise OSError(28, "No space left on device")
            return real(line, durable)
        self.s.ledger._sink_write = failing

    def stop(self):
        fcntl.flock(self.s._lock_fh, fcntl.LOCK_UN)
        self.s._lock_fh.close()


def _kinds(rows, kind):
    return [r for r in rows if r["kind"] == kind]


class FaultPoints(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bw-ev-")
        self.addCleanup(shutil.rmtree, self.d, True)

    # -- healthy path: intent then commit, one effect id -------------------
    def test_committed_effect_has_intent_and_commit_with_one_effect_id(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        r = a.send(T8, a.grant(T8), prov)
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["effect_id"], "e-1")
        rows = a.rows()
        intents, commits = _kinds(rows, "effect_intent"), _kinds(rows, "effect")
        self.assertEqual([i["effect_id"] for i in intents], ["e-1"])
        self.assertEqual([c["effect_id"] for c in commits], ["e-1"])
        self.assertLess(intents[0]["seq"], commits[0]["seq"])
        self.assertEqual(intents[0]["effect"], commits[0]["effect"])
        self.assertEqual(unresolved_intents(rows), [])
        a.stop()

    # -- before receipt write: the intent cannot be made durable ------------
    def test_intent_write_failure_means_effect_did_not_occur_and_server_stops(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        outbox = len(a.s.corpus.outbox)
        a.fail_sink_on("effect_intent")
        r = a.send(T8, gid, prov)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "evidence")
        self.assertEqual(len(a.s.corpus.outbox), outbox)                      # state 1
        self.assertEqual(a.s.resources.task_total.get(T8, 0), 0)
        self.assertEqual(_kinds(a.rows(), "effect_intent"), [])
        # fail-stop: nothing further is accepted, and hello says why
        self.assertEqual(a.h("request_grant", T8, {"resource": THR, "operation": "send_message"})
                         ["error"]["message"], "ledger_failed")
        self.assertEqual(a.h("hello", "-", {})["result"]["degraded"], "ledger_failed")
        a.stop()

    # -- after effect, before final receipt: commit write fails ------------
    def test_commit_write_failure_is_indeterminate_then_recovered_as_consumed(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        a.fail_sink_on("effect")
        r = a.send(T8, gid, prov)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "indeterminate")
        self.assertEqual(r["error"]["effect_id"], "e-1")
        self.assertEqual(len(a.s.corpus.outbox), 1)              # the world changed
        self.assertEqual(a.s.resources.task_total.get(T8, 0), 1)  # consumption ahead of evidence
        rows = a.rows()
        self.assertEqual(len(_kinds(rows, "effect_intent")), 1)
        self.assertEqual(_kinds(rows, "effect"), [])
        self.assertEqual([i["effect_id"] for i in unresolved_intents(rows)], ["e-1"])
        # the same grant cannot land a second effect, and says why
        r2 = a.send(T8, gid, prov, 1)
        self.assertEqual(r2["error"]["message"], "ledger_failed")   # fail-stop first
        a.stop()
        # trusted restart: the unresolved intent becomes an explicit marker
        b = _Srv(self.d)
        rows = b.rows()
        marks = _kinds(rows, "effect_indeterminate")
        self.assertEqual([m["effect_id"] for m in marks], ["e-1"])
        self.assertEqual(marks[0]["cause"], "unresolved_intent_at_restart")
        self.assertEqual(marks[0]["task_id"], T8)
        self.assertEqual(unresolved_intents(rows), [])
        pol = _kinds(rows, "resource_policy")[-1]
        self.assertEqual(pol["rebuilt_consumed"]["task_total"], {T8: 1})     # counted
        self.assertEqual(_kinds(rows, "trusted_restart")[-1]["effect_seq_resumed"], 1)
        # the task is closed; the next effect id continues after e-1
        self.assertEqual(b.h("begin_task", T8, {})["error"]["message"], "task_closed")
        prov = b.begin_and_prov(T11)
        self.assertEqual(b.send(T11, b.grant(T11), prov)["result"]["effect_id"], "e-2")
        b.stop()

    # -- crash after the intent, before the effect / before consumption ----
    def test_crash_between_intent_and_commit_is_indeterminate_not_lost(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov)
        a.stop()
        # keep every row up to and including the intent: the process died
        # before the commit (whether before or after the handler, the
        # evidence is the same, and so is the conservative conclusion)
        lines = open(a.path, encoding="utf-8").read().splitlines(keepends=True)
        cut = next(i for i, l in enumerate(lines) if json.loads(l)["kind"] == "effect_intent")
        with open(a.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:cut + 1])
        b = _Srv(self.d)
        rows = b.rows()
        self.assertEqual([m["effect_id"] for m in _kinds(rows, "effect_indeterminate")], ["e-1"])
        self.assertEqual(_kinds(rows, "resource_policy")[-1]["rebuilt_consumed"]["task_total"],
                         {T8: 1})
        b.stop()

    # -- during effect execution: the handler fails -------------------------
    def test_handler_failure_aborts_the_intent_and_consumes_nothing(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        r = a.send(T8, gid, prov, body=False)             # missing body -> handler KeyError
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "refusal")
        rows = a.rows()
        abort = [x for x in _kinds(rows, "refusal") if x.get("effect_id")]
        self.assertEqual([x["effect_id"] for x in abort], ["e-1"])
        self.assertEqual(unresolved_intents(rows), [])            # resolved by the abort
        self.assertEqual(a.s.resources.task_total.get(T8, 0), 0)
        self.assertEqual(len(a.s.corpus.outbox), 0)
        # the grant is still usable; the retry is a new effect
        r2 = a.send(T8, gid, prov, 1)
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["result"]["effect_id"], "e-2")
        a.stop()
        b = _Srv(self.d)
        self.assertEqual(_kinds(b.rows(), "effect_indeterminate"), [])
        self.assertEqual(_kinds(b.rows(), "resource_policy")[-1]["rebuilt_consumed"]["task_total"],
                         {T8: 1})
        b.stop()

    # -- during receipt write: a torn last line ----------------------------
    def test_torn_tail_refuses_restart_unless_recovered_and_is_receipted(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov)
        a.stop()
        lines = open(a.path, encoding="utf-8").read().splitlines(keepends=True)
        commit = lines[-1]
        self.assertEqual(json.loads(commit)["kind"], "effect")
        torn = commit[:len(commit) // 2]                    # the crash hit mid-commit
        with open(a.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1])
            fh.write(torn)
        with self.assertRaises(ValueError):
            _Srv(self.d)
        b = _Srv(self.d, recover=True)
        rows = b.rows()
        rs = _kinds(rows, "trusted_restart")[-1]
        self.assertEqual(rs["torn_tail_recovered"]["bytes"], len(torn.encode()))
        # the torn commit left its intent unresolved: indeterminate, consumed
        self.assertEqual([m["effect_id"] for m in _kinds(rows, "effect_indeterminate")], ["e-1"])
        self.assertEqual(_kinds(rows, "resource_policy")[-1]["rebuilt_consumed"]["task_total"],
                         {T8: 1})
        b.stop()

    def test_corruption_other_than_a_torn_tail_always_refuses(self):
        a = _Srv(self.d)
        a.begin_and_prov(T8)
        a.stop()
        lines = open(a.path, encoding="utf-8").read().splitlines(keepends=True)
        lines[1] = '{"kind": "effect", "task_\n'
        with open(a.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        with self.assertRaises(ValueError):
            _Srv(self.d, recover=True)

    # -- retry on the same grant learns the outcome -------------------------
    def test_retry_on_consumed_grant_names_the_prior_effect(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        first = a.send(T8, gid, prov)
        again = a.send(T8, gid, prov)
        self.assertFalse(again["ok"])
        self.assertIn("effect grant consumed", again["error"]["message"])
        self.assertEqual(again["error"]["prior_effect"],
                         {"effect_id": "e-1", "receipt_id": first["result"]["receipt_id"],
                          "status": "committed"})
        self.assertEqual(len(_kinds(a.rows(), "effect")), 1)
        a.stop()

    def test_retry_after_indeterminate_names_it_as_such(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        a.fail_sink_on("effect")
        a.send(T8, gid, prov)
        a.s.degraded = None            # only to reach the broker: what would it say?
        a.s.ledger.failed = None
        again = a.send(T8, gid, prov, 1)
        self.assertEqual(again["error"]["prior_effect"]["status"], "indeterminate")
        self.assertEqual(again["error"]["prior_effect"]["effect_id"], "e-1")
        a.stop()

    # -- replay of an accepted effect: same task, other task, after restart --
    def test_replay_never_lands_twice(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        gid = a.grant(T8)
        frame = _req("effect", T8, {"grant_id": gid, "effect_name": "send_message",
                                    "params": {"thread_id": "thr-alice", "body": "x"},
                                    "derived_from": prov})
        self.assertTrue(_dispatch(a.s, frame)["ok"])
        for _ in range(3):
            self.assertIn("effect grant consumed", _dispatch(a.s, frame)["error"]["message"])
        other = dict(frame, task_id=T11)
        self.assertEqual(_dispatch(a.s, other)["error"]["message"], "task_not_active")
        a.stop()
        b = _Srv(self.d)
        self.assertEqual(_dispatch(b.s, frame)["error"]["message"], "task_closed")
        prov = b.begin_and_prov(T11)
        self.assertIn("unknown grant", _dispatch(b.s, dict(frame, task_id=T11))["error"]["message"])
        self.assertEqual(len(_kinds(b.rows(), "effect")), 1)
        b.stop()

    # -- duplicate on a FRESH grant is not prevented by evidence, only bounded
    def test_fresh_grant_duplicate_is_distinguishable_and_budget_bounded(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        ids = [a.send(T8, a.grant(T8), prov, i)["result"]["effect_id"] for i in range(3)]
        self.assertEqual(ids, ["e-1", "e-2", "e-3"])
        r = a.send(T8, a.grant(T8), prov, 3)
        self.assertIn("budget exhausted", r["error"]["message"])
        self.assertEqual(len(_kinds(a.rows(), "effect")), 3)
        a.stop()

    # -- consumption is never behind the evidence --------------------------
    def test_consumed_state_never_behind_durable_evidence(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov, 0)
        a.send(T8, a.grant(T8), prov, 1, body=False)      # abort
        a.fail_sink_on("effect")
        a.send(T8, a.grant(T8), prov, 2)                  # indeterminate
        live = a.s.resources.task_total.get(T8, 0)
        rows = a.rows()
        intents = len(_kinds(rows, "effect_intent"))
        aborted = len([x for x in _kinds(rows, "refusal") if x.get("effect_id")])
        self.assertEqual(live, 2)
        self.assertEqual(intents - aborted, 2)             # evidence agrees exactly
        a.stop()
        b = _Srv(self.d)
        self.assertEqual(_kinds(b.rows(), "resource_policy")[-1]["rebuilt_consumed"]["task_total"],
                         {T8: 2})
        b.stop()


class LedgerOrdering(unittest.TestCase):
    def test_row_not_visible_in_memory_if_disk_write_failed(self):
        d = tempfile.mkdtemp(prefix="bw-led-")
        self.addCleanup(shutil.rmtree, d, True)
        led = Ledger(clock=lambda: 1.0, sink_path=os.path.join(d, "l.jsonl"))
        led.record("grant_request", "t")
        led._sink_write = lambda line, durable: (_ for _ in ()).throw(OSError(28, "ENOSPC"))
        with self.assertRaises(LedgerError):
            led.record("effect", "t")
        self.assertEqual([r.kind for r in led.all()], ["grant_request"])
        with self.assertRaises(LedgerError):            # failed for good
            led.record("task_end", "t")
        self.assertEqual(len(Ledger.existing_rows(os.path.join(d, "l.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()


class ObserverEvidence(unittest.TestCase):
    """The independent observer reconstructs every accepted effect from the
    evidence rows without trusting the client or the broker's words."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bw-evo-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _obs(self, rows):
        from blackwall.lifecycle_metrics import disagreements, reconstruct
        return reconstruct(rows), disagreements(rows)

    def test_committed_and_aborted_effects_reconstruct_cleanly(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov, 0)
        a.send(T8, a.grant(T8), prov, 1, body=False)
        a.stop()
        obs, dis = self._obs(a.rows())
        self.assertEqual(dis, [])
        self.assertFalse(any(o["observer_state"].startswith("intent_") for o in obs))

    def test_indeterminate_marker_is_accepted_and_counted(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.fail_sink_on("effect")
        a.send(T8, a.grant(T8), prov, 0)
        a.stop()
        rows = a.rows()
        obs, dis = self._obs(rows)                      # not yet restarted
        self.assertEqual(dis, [])
        self.assertEqual([o["broker_reason"] for o in obs
                          if o["observer_state"] == "intent_pending_recovery"], ["e-1"])
        b = _Srv(self.d)                                # recovery
        prov = b.begin_and_prov(T11)
        b.send(T11, b.grant(T11), prov, 1)
        b.stop()
        rows = b.rows()
        obs, dis = self._obs(rows)
        self.assertEqual(dis, [])
        self.assertFalse(any(o["observer_state"].startswith("intent_") for o in obs))

    def test_intent_unresolved_after_restart_is_a_disagreement(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.fail_sink_on("effect")
        a.send(T8, a.grant(T8), prov, 0)
        a.stop()
        b = _Srv(self.d)
        b.stop()
        rows = [r for r in b.rows() if r["kind"] != "effect_indeterminate"]   # marker dropped
        obs, dis = self._obs(rows)
        self.assertEqual([d["observer_state"] for d in dis], ["intent_unresolved_after_restart"])

    def test_commit_without_intent_and_duplicate_ids_are_disagreements(self):
        a = _Srv(self.d)
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov, 0)
        a.stop()
        rows = a.rows()
        no_intent = [r for r in rows if r["kind"] != "effect_intent"]
        _, dis = self._obs(no_intent)
        self.assertIn("commit_without_open_intent", [d["observer_state"] for d in dis])
        intent = next(r for r in rows if r["kind"] == "effect_intent")
        dup = rows + [dict(intent, receipt_id="r-999", seq=999, ts=rows[-1]["ts"] + 1)]
        _, dis = self._obs(dup)
        states = [d["observer_state"] for d in dis]
        self.assertIn("duplicate_effect_id", states)

    def test_indeterminate_counts_against_the_budget_for_the_observer(self):
        a = _Srv(self.d, policy=ResourcePolicy(effect_count=2))
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov, 0)
        a.fail_sink_on("effect")
        a.send(T8, a.grant(T8), prov, 1)                 # indeterminate: 2 consumed
        a.stop()
        b = _Srv(self.d, policy=ResourcePolicy(effect_count=2))
        b.stop()
        rows = b.rows()
        pol = [r for r in rows if r["kind"] == "resource_policy"][-1]
        self.assertEqual(pol["rebuilt_consumed"]["consumed"], {f"{T8}|send_message|{THR}": 2})
        # the observer's own count agrees: an intent past the cap would be flagged
        from blackwall.lifecycle_metrics import _Grants
        g = _Grants()
        for r in rows:
            g.apply(r)
        self.assertEqual(g.effects_by_key[(T8, "send_message", THR)], 2)

    def test_over_budget_intent_is_flagged(self):
        a = _Srv(self.d, policy=ResourcePolicy(effect_count=1))
        prov = a.begin_and_prov(T8)
        a.send(T8, a.grant(T8), prov, 0)
        a.stop()
        rows = a.rows()
        intent = next(r for r in rows if r["kind"] == "effect_intent")
        forged = rows + [dict(intent, receipt_id="r-998", seq=998, ts=rows[-1]["ts"] + 1,
                              effect_id="e-2", grant_id="g-9")]   # a broker that over-admits
        _, dis = self._obs(forged)
        self.assertIn("over_budget_intent", [d["observer_state"] for d in dis])
