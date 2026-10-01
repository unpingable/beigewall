import unittest

from blackwall.llm_metrics import aggregate_runs, denial_recovery
from blackwall.receipts import Ledger


def deny(led, task, resource, op="read", proj="conversation@1", reason="no"):
    led.record("grant_deny", task, resource=resource, operation=op,
               projection=proj, reason=reason)


def greq(led, task, resource, op="read", proj="conversation@1"):
    led.record("grant_request", task, resource=resource, operation=op,
               projection=proj, ttl=600, budget={"reads": 8, "objects": 200})


def mint(led, task, resource, op="read", proj="conversation@1"):
    led.record("grant_mint", task, grant_id="g-x", resource=resource,
               operation=op, projection=proj, expires_at=1, budget={})


class TestDenialRecovery(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(clock=lambda: 1.0)

    def test_repeated_identical_request(self):
        deny(self.led, "t", "contacts/*")
        greq(self.led, "t", "contacts/*")  # identical retry
        rep = denial_recovery(self.led, "t")
        self.assertEqual(rep["events"][0]["outcome"], "repeated")
        self.assertEqual(rep["summary"]["repeated"], 1)

    def test_adjusted_request_minted(self):
        deny(self.led, "t", "messages/*", reason="too broad")
        greq(self.led, "t", "messages/thread/thr-group")  # narrower
        mint(self.led, "t", "messages/thread/thr-group")
        rep = denial_recovery(self.led, "t")
        self.assertEqual(rep["events"][0]["outcome"], "adjusted_minted")

    def test_adjusted_request_also_denied(self):
        deny(self.led, "t", "messages/*")
        greq(self.led, "t", "messages/thread/thr-group")
        deny(self.led, "t", "messages/thread/thr-group")
        rep = denial_recovery(self.led, "t")
        self.assertEqual(rep["events"][0]["outcome"], "adjusted_denied")

    def test_no_retry(self):
        deny(self.led, "t", "contacts/ct-alice", proj="contact@1")
        rep = denial_recovery(self.led, "t")
        self.assertEqual(rep["events"][0]["outcome"], "no_retry")
        self.assertEqual(rep["summary"]["no_retry"], 1)

    def test_refusals_classified_too(self):
        self.led.record("refusal", "t", operation="read", grant_id="g-1",
                        resource="messages/thread/thr-group",
                        reason="object budget")
        greq(self.led, "t", "messages/thread/thr-group")
        rep = denial_recovery(self.led, "t")
        self.assertEqual(rep["events"][0]["kind"], "refusal")
        self.assertEqual(rep["events"][0]["outcome"], "repeated")

    def test_no_denials_empty(self):
        rep = denial_recovery(self.led, "t")
        self.assertEqual(rep["events"], [])
        self.assertEqual(rep["summary"]["denials"], 0)


def fake_run(run_id, per_task):
    return {"run_id": run_id, "per_task": per_task,
            "suite": {"task_success_rate":
                      sum(t["success"] for t in per_task.values()) / len(per_task)}}


def fake_task(success, grants=2, calls=5, turns=4, latency=10.0, unmediated=0,
              overclaim=0):
    return {"success": success, "grants_requested": grants,
            "broker_calls": calls, "unmediated_attempts": unmediated,
            "telemetry": {"turns": turns, "latency_total": latency,
                          "malformed_requests": 0, "transport_retries": 0},
            "divergence": {"counts": {"overclaim": overclaim, "omission": 0,
                                      "imprecise_attribution": 0,
                                      "undeclared_effect": 0}},
            "denial_recovery": {"summary": {"denials": 0, "refusals": 0,
                                            "repeated": 0, "adjusted_minted": 0,
                                            "adjusted_denied": 0, "no_retry": 0}}}


class TestAggregateRuns(unittest.TestCase):
    def test_per_task_success_frequency_and_variance(self):
        runs = [
            fake_run("r1", {"a": fake_task(True, grants=2),
                            "b": fake_task(True, grants=2)}),
            fake_run("r2", {"a": fake_task(False, grants=4),
                            "b": fake_task(True, grants=2)}),
            fake_run("r3", {"a": fake_task(True, grants=2),
                            "b": fake_task(True, grants=2)}),
        ]
        agg = aggregate_runs(runs)
        a = agg["per_task"]["a"]
        self.assertAlmostEqual(a["success_frequency"], 2 / 3)
        self.assertEqual(a["grants_requested"]["mean"], 8 / 3)
        self.assertEqual(a["grants_requested"]["min"], 2)
        self.assertEqual(a["grants_requested"]["max"], 4)
        self.assertGreater(a["grants_requested"]["pstdev"], 0)
        b = agg["per_task"]["b"]
        self.assertEqual(b["success_frequency"], 1.0)
        self.assertEqual(b["grants_requested"]["pstdev"], 0.0)

    def test_suite_level_stats(self):
        runs = [fake_run("r1", {"a": fake_task(True), "b": fake_task(True)}),
                fake_run("r2", {"a": fake_task(False), "b": fake_task(True)})]
        agg = aggregate_runs(runs)
        self.assertEqual(agg["suite"]["runs"], 2)
        self.assertEqual(agg["suite"]["success_rate"]["per_run"], [1.0, 0.5])
        self.assertAlmostEqual(agg["suite"]["success_rate"]["mean"], 0.75)
        self.assertAlmostEqual(agg["suite"]["success_rate"]["pstdev"], 0.25)

    def test_totals_for_probing_and_divergence(self):
        runs = [fake_run("r1", {"a": fake_task(True, unmediated=2, overclaim=1)}),
                fake_run("r2", {"a": fake_task(True, unmediated=1)})]
        agg = aggregate_runs(runs)
        self.assertEqual(agg["per_task"]["a"]["unmediated_attempts_total"], 3)
        self.assertEqual(agg["per_task"]["a"]["divergence_totals"]["overclaim"], 1)


if __name__ == "__main__":
    unittest.main()
