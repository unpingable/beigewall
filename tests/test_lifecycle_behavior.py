"""Agent-behaviour analysis on the deterministic probes."""

import unittest

from beigewall.lifecycle import probe_trajectories, run_trajectory, scripted_driver_for
from beigewall.lifecycle_behavior import (aggregate_behaviour, render_behaviour,
                                          trajectory_behaviour)


class TestBehaviourOnProbes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {t.trajectory_id: run_trajectory(t, scripted_driver_for)
                       for t in probe_trajectories()}
        cls.b = {k: trajectory_behaviour(v["rows"], v["evaluations"])
                 for k, v in cls.results.items()}

    def test_expiry_recovery_and_standing_authority(self):
        exp = self.b["probe-expiry"]
        self.assertEqual(exp["understands_expiry"], {"recovered_with_fresh_request": 1})
        self.assertEqual(exp["standing_authority"], {"other_task_grant": 1})
        rev = self.b["probe-revoke"]
        self.assertEqual(rev["understands_expiry"], {"recovered_with_fresh_request": 1})

    def test_cross_task_and_duplicates(self):
        ct = self.b["probe-cross-task"]
        self.assertEqual(ct["cross_task"]["uses_of_other_task_grant"], 2)
        self.assertEqual(ct["cross_task"]["successes"], 0)
        self.assertEqual(ct["standing_authority"], {"other_task_grant": 2, "never_minted": 1})
        rp = self.b["probe-replay-effect"]
        self.assertEqual(rp["duplicates"]["same_grant_replays_refused"], 1)
        self.assertEqual(rp["duplicates"]["fresh_grant_duplicates_after_loss"], 0)
        self.assertEqual(rp["duplicates"]["post_loss_behaviour"], {"retried_same_grant_refused": 1})

    def test_restart_chain_and_render(self):
        self.assertEqual(self.b["probe-restart"]["restart"], {"requested_fresh_authority": 1})
        agg = aggregate_behaviour(list(self.b.values()))
        self.assertEqual(agg["uses_agree"], agg["uses"])
        self.assertEqual(agg["injections_visible"], agg["injections"])
        self.assertEqual(agg["bleed_claims"], 0)
        text = render_behaviour(agg, 1.4)
        for q in ("understand expiration", "standing", "bleed", "duplicate effects",
                  "inflate", "restart", "chain sufficient"):
            self.assertIn(q, text)


if __name__ == "__main__":
    unittest.main()
