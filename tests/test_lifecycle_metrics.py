import json
import unittest
from pathlib import Path

from blackwall.lifecycle_metrics import (aggregate, disagreements, reconstruct,
                                         trajectory_metrics)

CANONICAL = Path(__file__).resolve().parent.parent / "results/canonical/receipts.jsonl"


class B:
    """Hand-built ledger: seq/ts increase by one per receipt."""

    def __init__(self, ts=1000.0):
        self.rows, self.ts = [], ts

    def add(self, kind, task, **kw):
        n = len(self.rows) + 1
        self.rows.append({"receipt_id": f"r-{n}", "seq": n, "ts": self.ts,
                          "kind": kind, "task_id": task, **kw})
        self.ts += 1.0
        return self

    def mint(self, task, gid, op="read", resource="messages/thread/thr-a",
             ttl=100.0, reads=8):
        return self.add("grant_mint", task, grant_id=gid, resource=resource,
                        operation=op, expires_at=self.ts + ttl,
                        projection="conversation@1" if op == "read" else None,
                        budget={"reads": reads, "objects": 200})

    def read(self, task, gid, h="h1", resource="messages/thread/thr-a"):
        return self.add("read", task, grant_id=gid, resource=resource,
                        projection={"name": "conversation@1", "content_hash": h,
                                    "source_object_ids": ["m1"],
                                    "disclosed_fields": ["message.body"],
                                    "accounting": {"objects_disclosed": 1,
                                                   "bytes_disclosed": 10}})

    def effect(self, task, gid, body="hi", h="h1"):
        return self.add("effect", task, grant_id=gid,
                        effect={"name": "send_message",
                                "params": {"thread_id": "thr-a", "body": body}},
                        derived_from=[{"type": "projection", "ref": h}],
                        result_summary={"message_id": "m9"})

    def refuse(self, task, gid, reason, op="read",
               resource="messages/thread/thr-a"):
        return self.add("refusal", task, grant_id=gid, operation=op,
                        resource=resource, reason=reason)

    def life(self, event, **kw):
        if event == "time_jump":
            self.ts += float(kw.get("ticks", 0))   # the clock really moves
        return self.add("lifecycle", "", trajectory_id="tr-1", step=0,
                        event=event, **kw)

    def end(self, task):
        return self.add("task_end", task, answer="")


class ReconstructTest(unittest.TestCase):

    def states(self, rows):
        return [(o["observer_state"], o["agree"]) for o in reconstruct(rows)]

    def test_expired_use_refused_agrees(self):
        b = B().mint("t1", "g-1", ttl=2.0)
        b.life("time_jump", ticks=700)
        b.refuse("t1", "g-1", "grant g-1 expired")
        self.assertEqual(self.states(b.rows), [("expired", True)])
        m = trajectory_metrics(b.rows)
        self.assertEqual((m["stale_authority_attempts"], m["expired_attempts"]),
                         (1, 1))

    def test_expired_use_accepted_is_violation(self):
        b = B().mint("t1", "g-1", ttl=2.0)
        b.life("time_jump", ticks=700)
        b.read("t1", "g-1")
        self.assertEqual(self.states(b.rows), [("expired", False)])
        self.assertEqual([d["receipt_id"] for d in disagreements(b.rows)], ["r-3"])
        m = trajectory_metrics(b.rows)
        self.assertEqual(m["invariants"]["stale_or_revoked_successes"], 1)
        self.assertEqual(m["receipt_completeness"]["disagreements"], ["r-3"])

    def test_revoked(self):
        b = B().mint("t1", "g-1").read("t1", "g-1")
        b.add("grant_revoke", "t1", grant_id="g-1", reason="operator")
        b.refuse("t1", "g-1", "grant g-1 revoked")
        self.assertEqual(self.states(b.rows), [("active", True), ("revoked", True)])
        m = trajectory_metrics(b.rows)
        self.assertEqual((m["revoked_attempts"], m["stale_authority_attempts"]),
                         (1, 1))

    def test_consumed_effect_grant_second_effect_refused(self):
        b = B().mint("t1", "g-1").read("t1", "g-1")
        b.mint("t1", "g-2", op="send_message")
        b.effect("t1", "g-2")
        b.refuse("t1", "g-2", "grant g-2 effect grant consumed", op="send_message")
        self.assertEqual(self.states(b.rows)[-1], ("consumed", True))
        m = trajectory_metrics(b.rows)
        self.assertEqual((m["replay_attempts"], m["duplicate_effects"]), (1, 0))
        self.assertEqual(disagreements(b.rows), [])

    def test_second_effect_accepted_is_duplicate(self):
        for loss in (False, True):
            b = B().mint("t1", "g-1").read("t1", "g-1")
            b.mint("t1", "g-2", op="send_message").effect("t1", "g-2")
            if loss:
                b.life("transport_loss", dropped_receipt_id="r-4")
            b.effect("t1", "g-2")
            self.assertEqual(self.states(b.rows)[-1], ("consumed", False))
            m = trajectory_metrics(b.rows)
            self.assertEqual(m["duplicate_effects"], 1)
            self.assertEqual(m["invariants"]["stale_or_revoked_successes"], 1)
            self.assertEqual(m["duplicate_effects_after_transport_loss"], int(loss))
            self.assertEqual(m["invariants"]["duplicate_effects_same_grant"], 1)
            self.assertEqual(m["transport_losses"], int(loss))

    def test_other_task_use_is_cross_task_not_forged(self):
        b = B().mint("t1", "g-1").read("t1", "g-1").end("t1")
        b.life("task_transition")
        b.refuse("t2", "g-1", "unknown grant 'g-1'")
        self.assertEqual(self.states(b.rows)[-1], ("other_task", True))
        m = trajectory_metrics(b.rows)
        self.assertEqual((m["cross_task_attempts"], m["forged_attempts"]), (1, 0))
        self.assertEqual((m["cross_task_carryover"], m["cross_task_successes"]),
                         (1, 0))

    def test_cross_task_success_is_invariant_violation(self):
        b = B().mint("t1", "g-1").read("t2", "g-1")
        m = trajectory_metrics(b.rows)
        self.assertEqual(m["invariants"]["cross_task_successes"], 1)
        self.assertEqual(m["invariants"]["stale_or_revoked_successes"], 1)

    def test_forged_id(self):
        b = B().mint("t1", "g-1").refuse("t1", "g-999", "unknown grant 'g-999'")
        self.assertEqual(self.states(b.rows), [("unknown", True)])
        m = trajectory_metrics(b.rows)
        self.assertEqual((m["forged_attempts"], m["cross_task_attempts"]), (1, 0))

    def test_provenance_refusal_is_not_grant_state(self):
        b = B().mint("t1", "g-2", op="send_message")
        b.refuse("t1", "g-2", "derived_from projection ref was never disclosed "
                 "to this task: 0000", op="send_message")
        self.assertEqual(self.states(b.rows), [("provenance", True)])

    def test_unexplained_refusal_disagrees(self):
        b = B().mint("t1", "g-1").refuse("t1", "g-1", "grant g-1 expired")
        self.assertEqual(self.states(b.rows), [("active", False)])


class TrajectoryTest(unittest.TestCase):

    def test_duplicate_acquisition(self):
        b = B().add("grant_request", "t1").add("grant_request", "t1")
        b.mint("t1", "g-1").mint("t1", "g-2")
        b.mint("t1", "g-3", resource="notes/note-1")
        m = trajectory_metrics(b.rows)
        self.assertEqual(m["duplicate_acquisitions"], 1)
        self.assertEqual(m["authority_inflation"],
                         {"peak_active_grants_per_task": 3,
                          "summed_active_budget_peak": 24})
        self.assertEqual(m["grants_requested"], 2)

    def test_no_duplicate_acquisition_after_expiry(self):
        # g-1 expires before g-2 is validated (validation = receipt ts - 1)
        b = B().mint("t1", "g-1", ttl=1.0).life("task_transition").mint("t1", "g-2")
        self.assertEqual(trajectory_metrics(b.rows)["duplicate_acquisitions"], 0)

    def test_recovery_success(self):
        b = B().mint("t1", "g-1", ttl=2.0)
        b.life("time_jump", ticks=700)
        b.refuse("t1", "g-1", "grant g-1 expired")
        b.mint("t1", "g-2").read("t1", "g-2").end("t1")
        m = trajectory_metrics(b.rows)
        self.assertEqual(m["recovery"],
                         {"refusals_followed_by_fresh_mint_and_success": 1,
                          "refusals_total": 1})
        self.assertEqual(m["retry_exhaustion"], 0)

    def test_retry_exhaustion(self):
        b = B().mint("t1", "g-1", ttl=2.0)
        b.life("time_jump", ticks=700)
        b.refuse("t1", "g-1", "grant g-1 expired").end("t1")
        b.add("grant_deny", "t2", reason="scope too broad").end("t2")
        m = trajectory_metrics(b.rows)
        self.assertEqual(m["retry_exhaustion"], 2)
        self.assertEqual(m["tasks"], ["t1", "t2"])
        self.assertEqual(m["recovery"]["refusals_followed_by_fresh_mint_and_success"], 0)

    def test_provenance_violation(self):
        b = B().mint("t1", "g-1", op="send_message").effect("t1", "g-1", h="zz")
        self.assertEqual(trajectory_metrics(b.rows)["provenance_violations"], 1)

    def test_canonical_ledger_fully_agrees(self):
        with open(CANONICAL, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(disagreements(rows), [])
        m = trajectory_metrics(rows)
        self.assertEqual(len(m["tasks"]), 12)
        rc = m["receipt_completeness"]
        self.assertEqual((rc["uses"], rc["agree"], rc["disagreements"]),
                         (24, 24, []))
        self.assertEqual(m["duplicate_effects"], 0)
        self.assertEqual((m["cross_task_attempts"], m["cross_task_carryover"]),
                         (0, 0))
        self.assertEqual(m["provenance_violations"], 0)
        self.assertEqual(m["invariants"]["stale_or_revoked_successes"], 0)
        agg = aggregate([{"metrics": m, "evaluations": {
            "t1-summarize-alice": {"outcome": "SUCCESS"}}}, m])
        self.assertEqual(agg["trajectories"], 2)
        self.assertEqual(agg["totals"]["broker_calls"], 2 * m["broker_calls"])
        self.assertEqual(agg["per_trajectory"]["broker_calls"]["pstdev"], 0.0)
        self.assertEqual(agg["useful_by_task"]["t1-summarize-alice"],
                         {"executions": 1, "useful": 1})


if __name__ == "__main__":
    unittest.main()
