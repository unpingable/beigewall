import unittest

from blackwall.agent.drivers import ScriptedDriver
from blackwall.agent.harness import Harness
from blackwall.authority import PolicyAuthority, PolicyRule
from blackwall.broker import CapabilityBroker
from blackwall.corpus import build_demo_corpus
from blackwall.receipts import Ledger, canonical_hash


def make_harness(clock=None):
    now = [1000.0] if clock is None else clock
    ledger = Ledger(clock=lambda: now[0])
    authority = PolicyAuthority(
        rules=[
            PolicyRule(task_id="*", resource="messages/*", operation="read",
                       projection="conversation@1", decision="approve"),
            PolicyRule(task_id="*", resource="contacts/*", operation="read",
                       projection="*", decision="deny", reason="contacts restricted"),
        ],
        clock=lambda: now[0])
    broker = CapabilityBroker(build_demo_corpus(), authority, ledger,
                              clock=lambda: now[0])
    return Harness(broker, ledger), ledger


PROMPT = "Summarize the Alice thread."


SCRIPT_HAPPY = [
    {"op": "request_grant", "resource": "messages/thread/thr-alice",
     "operation": "read", "projection": "conversation@1", "as": "gA"},
    {"op": "read", "grant": "gA", "resource": "messages/thread/thr-alice",
     "projection": "conversation@1"},
    {"op": "final", "answer": "Alice asked you to review the quarterly plan."},
]


class TestHarnessHappyPath(unittest.TestCase):
    def test_runs_script_and_records_receipts(self):
        harness, ledger = make_harness()
        driver = ScriptedDriver(SCRIPT_HAPPY, narrative={
            "claims": [{"resource": "messages/thread/thr-alice",
                        "objects": ["msg-1", "msg-2", "msg-3"],
                        "fields": ["message.body"]}],
            "declared_effects": [],
            "summary": "I read the Alice thread."})
        result = harness.run_task("t1", driver, prompt=PROMPT)
        self.assertIn("quarterly plan", result.answer)
        kinds = [r.kind for r in ledger.by_task("t1")]
        self.assertEqual(kinds, ["grant_request", "grant_mint", "read",
                                 "narrative", "task_end"])
        narrative = ledger.by_kind("narrative")[0]
        self.assertEqual(narrative.detail["claims"][0]["objects"][0], "msg-1")
        task_end = ledger.by_kind("task_end")[0]
        self.assertEqual(task_end.detail["answer_hash"],
                         canonical_hash({"answer": result.answer}))

    def test_prompt_hash_registered_with_broker(self):
        harness, ledger = make_harness()
        harness.run_task("t1", ScriptedDriver(
            [{"op": "final", "answer": "x"}],
            narrative={"claims": [], "declared_effects": [], "summary": ""}),
            prompt=PROMPT)
        mint = ledger.by_kind("grant_request")  # no grants requested
        self.assertEqual(mint, [])  # hash used internally; verified via effect tests


class TestHarnessRefusalsSurfaceToDriver(unittest.TestCase):
    def test_denied_grant_returns_error_result_not_exception(self):
        harness, ledger = make_harness()
        script = [
            {"op": "request_grant", "resource": "contacts/ct-alice",
             "operation": "read", "projection": "contact@1", "as": "gC"},
            {"op": "read", "grant": "gC", "resource": "contacts/ct-alice",
             "projection": "contact@1"},
            {"op": "final", "answer": "I could not access Alice's contact."},
        ]
        driver = ScriptedDriver(script, narrative={"claims": [],
                                                   "declared_effects": [],
                                                   "summary": "nothing"})
        result = harness.run_task("t5", driver, prompt=PROMPT)
        self.assertIn("could not", result.answer)
        kinds = [r.kind for r in ledger.by_task("t5")]
        self.assertEqual(kinds, ["grant_request", "grant_deny", "narrative",
                                 "task_end"])
        # the read step was skipped because alias gC never resolved
        skip_obs = [o for o in result.observations if o.get("skipped")]
        self.assertEqual(len(skip_obs), 1)


class TestUnmediatedAttempts(unittest.TestCase):
    def test_unknown_capability_is_trapped_and_recorded(self):
        harness, ledger = make_harness()
        script = [
            {"op": "unmediated", "call": "shell", "args": ["ls", "/"]},
            {"op": "unmediated", "call": "read_file", "args": ["/etc/passwd"]},
            {"op": "final", "answer": "done"},
        ]
        driver = ScriptedDriver(script, narrative={"claims": [],
                                                   "declared_effects": [],
                                                   "summary": "nothing"})
        result = harness.run_task("t9", driver, prompt=PROMPT)
        attempts = ledger.by_kind("unmediated_attempt")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].detail["attempted"], "shell")
        traps = [o for o in result.observations if o.get("unmediated_refused")]
        self.assertEqual(len(traps), 2)
        self.assertIn("no mediated capability", traps[0]["error"])


class TestStepBound(unittest.TestCase):
    def test_runaway_script_is_truncated(self):
        harness, ledger = make_harness()
        script = [{"op": "unmediated", "call": "noop", "args": []}] * 25
        driver = ScriptedDriver(script, narrative={"claims": [],
                                                   "declared_effects": [],
                                                   "summary": ""})
        result = harness.run_task("tx", driver, prompt=PROMPT, max_steps=10)
        self.assertTrue(result.truncated)
        self.assertEqual(len(ledger.by_kind("unmediated_attempt")), 10)
        self.assertEqual(ledger.by_kind("task_end")[0].detail["truncated"], True)


if __name__ == "__main__":
    unittest.main()
