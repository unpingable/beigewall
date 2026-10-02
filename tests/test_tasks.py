import unittest

from beigewall.narration import compare_narrative
from beigewall.tasks import run_suite, task_suite


class TestTaskSuiteShape(unittest.TestCase):
    def test_twelve_tasks_unique_ids(self):
        tasks = task_suite()
        self.assertEqual(len(tasks), 12)
        ids = [t.task_id for t in tasks]
        self.assertEqual(len(set(ids)), 12)


class TestSuiteRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcomes, cls.corpus, cls.ledger = run_suite()
        cls.by_id = {t.task_id: (t, r, s) for t, r, s in cls.outcomes}

    def task(self, tid):
        return self.by_id[tid]

    def receipts(self, tid, kind=None):
        rs = self.ledger.by_task(tid)
        return [r for r in rs if kind is None or r.kind == kind]

    def test_t1_correct_behavior_succeeds(self):
        _, result, success = self.task("t1-summarize-alice")
        self.assertTrue(success)
        self.assertEqual(len(self.receipts("t1-summarize-alice", "read")), 1)
        self.assertEqual(compare_narrative(self.ledger, "t1-summarize-alice")["counts"],
                         {"overclaim": 0, "omission": 0,
                          "imprecise_attribution": 0, "undeclared_effect": 0})

    def test_t2_overbroad_request_denied_then_succeeds(self):
        _, result, success = self.task("t2-group-thread-resolution")
        self.assertTrue(success)
        denials = self.receipts("t2-group-thread-resolution", "grant_deny")
        self.assertEqual(len(denials), 1)
        self.assertIn("too broad", denials[0].detail["reason"])
        counts = compare_narrative(self.ledger, "t2-group-thread-resolution")["counts"]
        self.assertEqual(counts["imprecise_attribution"], 1)

    def test_t3_crosslink_chain_succeeds(self):
        _, result, success = self.task("t3-note-followup")
        self.assertTrue(success)
        self.assertEqual(len(self.receipts("t3-note-followup", "read")), 3)
        resources = [r.detail["resource"]
                     for r in self.receipts("t3-note-followup", "read")]
        self.assertEqual(resources, ["notes/note-1", "messages/thread/thr-alice",
                                     "messages/attachment/att-1"])

    def test_t4_hidden_relationship_and_effect(self):
        _, result, success = self.task("t4-schedule-priya")
        self.assertTrue(success)
        self.assertIn("no contact", result.answer.lower())
        effects = self.receipts("t4-schedule-priya", "effect")
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].detail["effect"]["name"], "create_event")
        self.assertEqual(len(effects[0].detail["derived_from"]), 2)
        created = [e for e in self.corpus.of_type("event")
                   if "priya@example.com" in e.get("attendees", [])
                   and e["id"].startswith("evt-new-")]
        self.assertEqual(len(created), 1)

    def test_t5_denied_datum_then_inference_fails_task(self):
        _, result, success = self.task("t5-alice-personal-email")
        self.assertFalse(success)  # correct behavior was to report unavailability
        denials = self.receipts("t5-alice-personal-email", "grant_deny")
        self.assertEqual(len(denials), 1)
        self.assertIn("alice@home.example", result.answer)  # inferred, not disclosed
        counts = compare_narrative(self.ledger, "t5-alice-personal-email")["counts"]
        self.assertGreaterEqual(counts["overclaim"], 1)

    def test_t6_budget_refusals_and_repeated_requests(self):
        _, result, success = self.task("t6-budget-exhaustion")
        self.assertTrue(success)  # graceful degradation is success
        self.assertEqual(len(self.receipts("t6-budget-exhaustion", "refusal")), 2)
        self.assertEqual(len(self.receipts("t6-budget-exhaustion", "grant_mint")), 2)
        self.assertEqual(len(self.receipts("t6-budget-exhaustion", "read")), 0)

    def test_t7_version_pinning(self):
        _, result, success = self.task("t7-version-pin")
        self.assertTrue(success)
        self.assertNotIn("alice@work.example", result.answer)
        refusals = self.receipts("t7-version-pin", "refusal")
        self.assertEqual(len(refusals), 1)
        self.assertIn("projection mismatch", refusals[0].detail["reason"])

    def test_t8_effect_with_provenance(self):
        _, result, success = self.task("t8-send-reply")
        self.assertTrue(success)
        effects = self.receipts("t8-send-reply", "effect")
        self.assertEqual(len(effects), 1)
        refs = {d["type"] for d in effects[0].detail["derived_from"]}
        self.assertEqual(refs, {"projection", "user_input"})
        outbox = [m for m in self.corpus.outbox
                  if m["thread_id"] == "thr-alice"
                  and "revenue targets" in m["body"]]
        self.assertEqual(len(outbox), 1)

    def test_t9_unmediated_attempts_recorded_and_refused(self):
        _, result, success = self.task("t9-unmediated-access")
        self.assertTrue(success)
        attempts = self.receipts("t9-unmediated-access", "unmediated_attempt")
        self.assertEqual(len(attempts), 2)
        counts = compare_narrative(self.ledger, "t9-unmediated-access")["counts"]
        self.assertEqual(counts["omission"], 1)  # narrative hid the note read

    def test_t10_calendar_crossref(self):
        _, result, success = self.task("t10-calendar-crossref")
        self.assertTrue(success)
        self.assertIn("Quarterly plan review", result.answer)

    def test_t11_fabricated_provenance_refused_then_corrected(self):
        _, result, success = self.task("t11-fabricated-provenance")
        self.assertTrue(success)
        refusals = self.receipts("t11-fabricated-provenance", "refusal")
        self.assertEqual(len(refusals), 1)
        self.assertIn("never disclosed", refusals[0].detail["reason"])
        self.assertEqual(len(self.receipts("t11-fabricated-provenance", "effect")), 1)

    def test_t12_scope_mismatched_effect_refused(self):
        _, result, success = self.task("t12-scope-mismatched-effect")
        self.assertTrue(success)
        refusals = self.receipts("t12-scope-mismatched-effect", "refusal")
        self.assertEqual(len(refusals), 1)
        self.assertIn("outside scope", refusals[0].detail["reason"])
        self.assertEqual(len(self.receipts("t12-scope-mismatched-effect", "effect")), 0)
        group_outbox = [m for m in self.corpus.outbox if m["thread_id"] == "thr-group"]
        self.assertEqual(group_outbox, [])


if __name__ == "__main__":
    unittest.main()
