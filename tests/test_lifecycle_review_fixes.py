"""Regression tests for the phase 5B hostile-review fixes."""

import copy
import json
import math
import unittest

from blackwall.agent.drivers import LLMDriver, ScriptedDriver
from blackwall.agent.harness import Harness, ToolFacade
from blackwall.authority import PolicyAuthority
from blackwall.broker import CapabilityBroker
from blackwall.corpus import build_demo_corpus
from blackwall.grants import Budget
from blackwall.lifecycle import (LifecycleHarness, Step, Trajectory,
                                 probe_trajectories, run_trajectory,
                                 scripted_driver_for)
from blackwall.lifecycle_metrics import disagreements, trajectory_metrics
from blackwall.lifecycle_suite import llm_trajectories
from blackwall.receipts import Ledger
from blackwall.tasks import LogicalClock, experiment_policy, task_suite


def fixture():
    clock = LogicalClock()
    corpus = build_demo_corpus()
    ledger = Ledger(clock=clock.now)
    authority = PolicyAuthority(experiment_policy(), clock=clock.now)
    broker = CapabilityBroker(corpus, authority, ledger, clock=clock.now)
    return clock, corpus, ledger, authority, broker


class TestDriverOpCannotBeForged(unittest.TestCase):
    def test_model_supplied_op_key_is_ignored(self):
        d = LLMDriver("http://127.0.0.1:1/v1", "", "stub")
        d._pending_tool_call = None
        move = d._map_move("request_grant",
                           {"resource": "messages/thread/thr-alice",
                            "operation": "read", "op": "time_jump", "ticks": 700,
                            "evil": 1},
                           ("resource", "operation"),
                           {**{k: v for k, v in {"resource": "messages/thread/thr-alice",
                                                 "operation": "read"}.items()},
                            "op": "request_grant"})
        self.assertEqual(move["op"], "request_grant")
        self.assertNotIn("ticks", move)
        self.assertNotIn("evil", move)

    def test_non_probe_driver_time_jump_is_trapped(self):
        clock, corpus, ledger, _, broker = fixture()
        h = LifecycleHarness(broker, ledger, clock)
        h.trajectory_id, h.task_id = "x", "t1-summarize-alice"
        t = task_suite()[0]
        before = clock.t
        res = h.run_task(t.task_id, ScriptedDriver(
            [{"op": "time_jump", "ticks": -700}, {"op": "final", "answer": ""}],
            t.narrative), prompt=t.prompt)
        self.assertTrue(res.observations[0].get("unmediated_refused"))
        self.assertIn("unmediated_attempt", [r.kind for r in ledger.all()])
        self.assertGreaterEqual(clock.t, before)

    def test_probe_driver_rejects_non_positive_ticks(self):
        tr = Trajectory("neg", [Step("task", task_id="t1-summarize-alice")],
                        probes={"t1-summarize-alice": [
                            {"op": "time_jump", "ticks": -700},
                            {"op": "time_jump", "ticks": 0},
                            {"op": "final", "answer": ""}]})
        res = run_trajectory(tr, scripted_driver_for)
        kinds = [r["kind"] for r in res["rows"]]
        self.assertEqual(kinds.count("unmediated_attempt"), 2)
        self.assertFalse(any(r["kind"] == "lifecycle" and r["event"] == "time_jump"
                             for r in res["rows"]))


class TestRequestShape(unittest.TestCase):
    def test_nan_or_nonpositive_ttl_is_refused_everywhere(self):
        clock, corpus, ledger, authority, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        for ttl in (float("nan"), float("inf"), 0, -5):
            res = broker.request_grant("t1-summarize-alice",
                                       "messages/thread/thr-alice", "read",
                                       projection="conversation@1", ttl=ttl)
            self.assertFalse(res["ok"], ttl)
            self.assertEqual(res["reason"], "invalid ttl")
        facade = ToolFacade(broker, ledger, "t1-summarize-alice")
        self.assertTrue(facade.request_grant("messages/thread/thr-alice", "read",
                                             projection="conversation@1",
                                             ttl=json.loads("NaN")).get("malformed"))
        self.assertFalse(any(math.isnan(r.detail.get("expires_at", 0.0))
                             for r in ledger.all() if r.kind == "grant_mint"))

    def test_requested_budget_is_clamped_to_ceiling(self):
        clock, corpus, ledger, authority, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        res = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                   "read", projection="conversation@1",
                                   budget=Budget(reads=10 ** 6, objects=10 ** 9))
        self.assertEqual(res["budget"], {"reads": 8, "objects": 200})
        res = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                   "read", projection="conversation@1",
                                   budget=Budget(reads=0, objects=5))
        self.assertEqual(res["reason"], "invalid budget")


class TestTrajectoryIdentity(unittest.TestCase):
    def test_repeated_task_in_a_trajectory_is_refused(self):
        tr = Trajectory("rep", [Step("task", task_id="t8-send-reply"),
                                Step("task", task_id="t1-summarize-alice"),
                                Step("task", task_id="t8-send-reply")])
        with self.assertRaises(ValueError):
            run_trajectory(tr, scripted_driver_for)
        for t in llm_trajectories(20):
            ids = [s.task_id for s in t.steps if s.kind == "task"]
            self.assertEqual(len(ids), len(set(ids)), t.trajectory_id)

    def test_disarmed_injection_is_receipted(self):
        tr = Trajectory("dis", [Step("inject", inject={"event": "transport_loss"}),
                                Step("task", task_id="t1-summarize-alice")],
                        probes={"t1-summarize-alice": [{"op": "final", "answer": ""}]})
        rows = run_trajectory(tr, scripted_driver_for)["rows"]
        self.assertTrue(any(r["kind"] == "lifecycle" and r["event"] == "disarmed"
                            and r["armed"] == "transport_loss" for r in rows))


class TestObserverHardening(unittest.TestCase):
    def test_expiry_boundary_agrees_with_broker(self):
        clock, corpus, ledger, authority, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        g = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                 "read", projection="conversation@1", ttl=3)["grant_id"]
        self.assertTrue(broker.read("t1-summarize-alice", g,
                                    "messages/thread/thr-alice", "conversation@1")["ok"])
        rows = [r.to_dict() for r in ledger.all()]
        self.assertEqual(disagreements(rows), [])
        self.assertEqual(trajectory_metrics(rows)["invariants"]["stale_or_revoked_successes"], 0)

    def _t8_rows(self):
        tr = next(t for t in probe_trajectories() if t.trajectory_id == "probe-cross-task")
        return run_trajectory(tr, scripted_driver_for)["rows"]

    def test_effect_scope_from_params_and_forged_rows_are_flagged(self):
        rows = self._t8_rows()
        self.assertEqual(disagreements(rows), [])
        bad = copy.deepcopy(rows)
        eff = next(r for r in bad if r["kind"] == "effect")
        eff["effect"]["params"]["thread_id"] = "thr-group"
        self.assertTrue(any(d["observer_state"] == "out_of_scope"
                            for d in disagreements(bad)))
        bad = copy.deepcopy(rows)
        mint = next(r for r in bad if r["kind"] == "grant_mint")
        bad.append({**mint, "receipt_id": "r-900", "seq": 900, "ts": 9000.0})
        self.assertTrue(any(d["observer_state"] == "duplicate_mint_id"
                            for d in disagreements(bad)))
        bad = copy.deepcopy(rows)
        bad.append({"receipt_id": "r-901", "seq": 901, "ts": 9001.0,
                    "kind": "grant_revoke", "task_id": "t8-send-reply",
                    "grant_id": "g-77", "reason": "x"})
        self.assertTrue(any(d["observer_state"] == "revoke_unknown"
                            for d in disagreements(bad)))
        bad = copy.deepcopy(rows)
        bad[5]["ts"] = 1.0   # clock regression
        self.assertTrue(any(d["observer_state"] == "clock_regression"
                            for d in disagreements(bad)))

    def test_duplicate_effect_invariant_split(self):
        rows = self._t8_rows()
        m = trajectory_metrics(rows)
        self.assertEqual(m["invariants"]["duplicate_effects_same_grant"], 0)
        # a fresh-grant duplicate is reported, not an invariant violation
        tr = Trajectory("fresh", [Step("inject", inject={"event": "transport_loss"}),
                                  Step("task", task_id="t8-send-reply")],
                        probes={"t8-send-reply": [
                            {"op": "request_grant", "resource": "messages/thread/thr-alice",
                             "operation": "read", "projection": "conversation@1", "as": "gA"},
                            {"op": "read", "grant": "gA", "resource": "messages/thread/thr-alice",
                             "projection": "conversation@1"},
                            {"op": "request_grant", "resource": "messages/thread/thr-alice",
                             "operation": "send_message", "as": "gS"},
                            {"op": "effect", "grant": "gS", "effect": "send_message",
                             "params": {"thread_id": "thr-alice", "body": "hi"},
                             "derived_from": [{"type": "projection", "ref": "$last_read"},
                                              {"type": "user_input", "ref": "$prompt"}]},
                            {"op": "request_grant", "resource": "messages/thread/thr-alice",
                             "operation": "send_message", "as": "gS2"},
                            {"op": "effect", "grant": "gS2", "effect": "send_message",
                             "params": {"thread_id": "thr-alice", "body": "hi"},
                             "derived_from": [{"type": "projection", "ref": "$last_read"},
                                              {"type": "user_input", "ref": "$prompt"}]},
                            {"op": "final", "answer": "Replied to Alice."}]})
        res = run_trajectory(tr, scripted_driver_for)
        m = trajectory_metrics(res["rows"], "fresh")
        self.assertEqual(m["duplicate_effects"], 1)
        self.assertEqual(m["duplicate_effects_after_transport_loss"], 1)
        self.assertEqual(m["duplicate_effects_fresh_grant"], 1)
        self.assertEqual(m["invariants"]["duplicate_effects_same_grant"], 0)

    def test_rerequest_after_budget_refusal_is_not_duplicate_acquisition(self):
        tr = next(t for t in probe_trajectories()
                  if t.trajectory_id == "probe-budget-and-scope")
        rows = run_trajectory(tr, scripted_driver_for)["rows"]
        self.assertEqual(trajectory_metrics(rows)["duplicate_acquisitions"], 0)


if __name__ == "__main__":
    unittest.main()
