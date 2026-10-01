"""Capability discovery: shape of authority only — grants nothing, shows no
corpus value, is hashed/versioned and receipted, exists only in the treatment
facade, and leaves grant semantics and canonical artifacts unchanged."""

import json
import os
import tempfile
import unittest

from blackwall.agent.drivers import ScriptedDriver
from blackwall.agent.harness import Harness
from blackwall.authority import PolicyAuthority
from blackwall.broker import CapabilityBroker
from blackwall.capabilities import (PROJECTION_SHAPES, SCHEMA, CapabilityCatalog,
                                    policy_decision, policy_version)
from blackwall.corpus import build_demo_corpus
from blackwall.experiment import run_experiment
from blackwall.negotiation import negotiation_telemetry
from blackwall.projections import run_projection
from blackwall.receipts import Ledger
from blackwall.tasks import LogicalClock, experiment_policy, task_suite


def fixture():
    clock = LogicalClock()
    corpus = build_demo_corpus()
    ledger = Ledger(clock=clock.now)
    authority = PolicyAuthority(experiment_policy(), clock=clock.now)
    broker = CapabilityBroker(corpus, authority, ledger, clock=clock.now)
    return corpus, ledger, authority, broker


def corpus_values(corpus) -> set[str]:
    out = set()
    for obj in corpus._objects.values():
        for k, v in obj.items():
            if k in ("id", "type"):
                continue
            vals = v if isinstance(v, list) else [v]
            out |= {str(x) for x in vals if x and len(str(x)) > 3}
    return out


class TestCatalogShape(unittest.TestCase):
    def test_catalog_has_schema_version_and_hash(self):
        _, _, _, broker = fixture()
        broker.begin_task("t2-group-thread-resolution", "h")
        res = broker.discover("t2-group-thread-resolution")
        cat = res["catalog"]
        self.assertEqual(cat["schema"], SCHEMA)
        self.assertEqual(len(cat["catalog_hash"]), 64)
        self.assertEqual(cat["policy_version"],
                         policy_version(experiment_policy()))
        body = {k: v for k, v in cat.items() if k != "catalog_hash"}
        from blackwall.receipts import canonical_hash
        self.assertEqual(canonical_hash(body), cat["catalog_hash"])

    def test_catalog_is_deterministic_and_task_scoped(self):
        _, _, _, b1 = fixture()
        _, _, _, b2 = fixture()
        a = b1.discover("t5-alice-personal-email")["catalog"]
        b = b2.discover("t5-alice-personal-email")["catalog"]
        self.assertEqual(a["catalog_hash"], b["catalog_hash"])
        other = b1.discover("t1-summarize-alice")["catalog"]
        self.assertNotEqual(a["catalog_hash"], other["catalog_hash"])
        # t5 sees its own deny rule; t1 does not
        self.assertTrue(any(r["decision"] == "deny" and r["resource"] == "contacts/*"
                            for r in a["rules"]))
        self.assertFalse(any(r["decision"] == "deny" and r["resource"] == "contacts/*"
                             for r in other["rules"]))

    def test_catalog_discloses_no_corpus_values(self):
        corpus, _, _, broker = fixture()
        values = corpus_values(corpus)
        from blackwall.disclosure import MANIFEST_IDS
        for task in task_suite():
            cat = broker.discover(task.task_id)["catalog"]
            blob = json.dumps({k: v for k, v in cat.items()
                               if k != "task_id"}).lower()
            for v in values:
                # ids the policy itself names (and every driver is handed as
                # addressing metadata) are not corpus content
                if v in MANIFEST_IDS or any(v.endswith("/" + m)
                                            for m in MANIFEST_IDS):
                    continue
                self.assertNotIn(v.lower(), blob, (task.task_id, v))
        # neither contact ids nor attachment ids nor event ids are shown
        cat = broker.discover("t4-schedule-priya")["catalog"]
        blob = json.dumps({k: v for k, v in cat.items() if k != "task_id"})
        for oid in ("ct-alice", "ct-bob", "att-1", "evt-1", "evt-2", "msg-1",
                    "priya"):
            self.assertNotIn(oid, blob)

    def test_query_cannot_confirm_existence(self):
        _, _, _, broker = fixture()
        real = broker.discover("t1-summarize-alice",
                               resource="messages/thread/thr-alice")["catalog"]
        fake = broker.discover("t1-summarize-alice",
                               resource="messages/thread/thr-nobody")["catalog"]
        reads = lambda c: [r for r in c["rules"] if r["operation"] == "read"]
        self.assertEqual(reads(real), reads(fake))
        self.assertTrue(real["query_matched"] and fake["query_matched"])
        # the only difference is the policy's own literal send rule, which is
        # policy metadata, not a statement that the thread exists
        extra = [r for r in real["rules"] if r not in fake["rules"]]
        self.assertEqual([r["operation"] for r in extra], ["send_message"])
        unknown = broker.discover("t1-summarize-alice",
                                  resource="files/secret")["catalog"]
        self.assertFalse(unknown["query_matched"])
        self.assertEqual(unknown["rules"], [])

    def test_projection_shapes_match_registry(self):
        corpus = build_demo_corpus()
        samples = {"conversation@1": "messages/thread/thr-alice",
                   "conversation@2": "messages/thread/thr-alice",
                   "contact@1": "contacts/ct-alice",
                   "contact-directory@1": "contacts/*",
                   "note@1": "notes/note-1", "agenda@1": "calendar/events",
                   "attachment@1": "messages/attachment/att-1"}
        for name, resource in samples.items():
            result = run_projection(name, corpus, resource)
            self.assertEqual(result.disclosed_fields,
                             PROJECTION_SHAPES[name]["fields"], name)

    def test_policy_decision_replicates_authority(self):
        _, _, authority, _ = fixture()
        from blackwall.grants import Budget
        cases = [("t2-group-thread-resolution", "messages/*", "read", "conversation@1"),
                 ("t1-summarize-alice", "messages/thread/thr-alice", "read", "conversation@1"),
                 ("t1-summarize-alice", "messages/thread/thr-alice", "read", "conversation@2"),
                 ("t5-alice-personal-email", "contacts/ct-alice", "read", "contact@1"),
                 ("t12-scope-mismatched-effect", "messages/thread/thr-group", "send_message", None),
                 ("t4-schedule-priya", "calendar/events", "create_event", None),
                 ("t9-unmediated-access", "files/x", "read", "note@1")]
        for tid, res, op, proj in cases:
            pure, _ = policy_decision(experiment_policy(), task_id=tid,
                                      resource=res, operation=op, projection=proj)
            grant, _ = authority.decide(task_id=tid, resource=res, operation=op,
                                        projection=proj, ttl=10.0,
                                        budget=Budget(1, 1))
            self.assertEqual(pure == "approve", grant is not None, (tid, res))


class TestDiscoveryGrantsNothing(unittest.TestCase):
    def test_discovery_creates_no_grant_and_changes_no_grant_state(self):
        _, ledger, authority, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        broker.discover("t1-summarize-alice")
        broker.discover("t1-summarize-alice", resource="contacts/*")
        self.assertEqual(authority.authorization_events, 0)
        self.assertEqual(authority.grants_minted, 0)
        self.assertEqual(broker._grants, {})
        # a read with a made-up grant id after discovery is refused as before
        from blackwall.broker import RefusalError
        with self.assertRaises(RefusalError):
            broker.read("t1-summarize-alice", "g-1",
                        "messages/thread/thr-alice", "conversation@1")
        kinds = [r.kind for r in ledger.by_task("t1-summarize-alice")]
        self.assertEqual(kinds, ["discovery", "discovery", "refusal"])

    def test_discovery_receipt_is_distinguishable_from_reads(self):
        _, ledger, _, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        res = broker.discover("t1-summarize-alice", resource="notes/*")
        r = ledger.by_task("t1-summarize-alice")[-1].to_dict()
        self.assertEqual(r["kind"], "discovery")
        self.assertEqual(r["catalog_hash"], res["catalog"]["catalog_hash"])
        self.assertEqual(r["policy_version"], res["catalog"]["policy_version"])
        self.assertEqual(r["query"], "notes/*")
        self.assertNotIn("projection", r)
        self.assertNotIn("content", r)
        # the receipt-grounded evaluator's disclosure set ignores it
        from blackwall.disclosure import disclosure_from_receipts
        d = disclosure_from_receipts([r], "t1-summarize-alice", "p")
        self.assertEqual(d.pairs, set())

    def test_grants_unchanged_with_discovery_present(self):
        _, ledger, _, broker = fixture()
        broker.begin_task("t1-summarize-alice", "h")
        broker.discover("t1-summarize-alice")
        g = broker.request_grant("t1-summarize-alice", "messages/thread/thr-alice",
                                 "read", projection="conversation@1")
        self.assertTrue(g["ok"])
        self.assertEqual(g["budget"], {"reads": 8, "objects": 200})
        res = broker.read("t1-summarize-alice", g["grant_id"],
                          "messages/thread/thr-alice", "conversation@1")
        self.assertTrue(res["ok"])
        denied = broker.request_grant("t1-summarize-alice", "contacts/ct-alice",
                                      "read", projection="contact@1")
        self.assertFalse(denied["ok"])


class TestFacadeConditions(unittest.TestCase):
    def _run(self, discovery, script):
        _, ledger, _, broker = fixture()
        harness = Harness(broker, ledger, discovery=discovery)
        task = task_suite()[0]
        result = harness.run_task(task.task_id, ScriptedDriver(
            script, {"claims": [], "declared_effects": [], "summary": ""}),
            prompt=task.prompt)
        return result, [r.to_dict() for r in ledger.by_task(task.task_id)]

    def test_control_facade_traps_discover_as_unmediated(self):
        result, rows = self._run(False, [
            {"op": "discover"},
            {"op": "final", "answer": "x"}])
        self.assertEqual([r["kind"] for r in rows][:1], ["unmediated_attempt"])
        self.assertEqual(rows[0]["attempted"], "discover")
        self.assertTrue(result.observations[0].get("unmediated_refused"))

    def test_treatment_facade_serves_discovery(self):
        result, rows = self._run(True, [
            {"op": "discover", "resource": "messages/*"},
            {"op": "final", "answer": "x"}])
        self.assertEqual(rows[0]["kind"], "discovery")
        self.assertIn("catalog", result.observations[0])
        self.assertNotIn("unmediated_attempt", [r["kind"] for r in rows])


class TestScriptedTreatmentDriver(unittest.TestCase):
    """Plumbing control: scripts that consult discovery before each grant
    produce the canonical read/effect receipts plus discovery receipts."""

    def test_discover_then_canonical_script(self):
        from blackwall.llm_experiment import run_llm_experiment

        def factory(task):
            script = [{"op": "discover"}] + list(task.script)
            return ScriptedDriver(script, task.narrative)

        with tempfile.TemporaryDirectory() as d:
            res = run_llm_experiment(factory, d, label="treat-selfcheck",
                                     model="scripted", base_url="n/a", runs=1,
                                     condition="treatment")
            meta = json.load(open(os.path.join(d, "meta.json")))
            self.assertEqual(meta["condition"], "treatment")
            self.assertTrue(meta["capability_discovery"])
            rows = [json.loads(l) for l in
                    open(os.path.join(d, "run-1", "receipts.jsonl"))]
        canonical = [json.loads(l) for l in open("results/canonical/receipts.jsonl")]
        # discovery receipts consume logical-clock ticks, so clock-derived
        # fields shift; everything else must be identical
        strip = lambda rs: [{k: v for k, v in r.items()
                             if k not in ("receipt_id", "seq", "ts",
                                          "expires_at")}
                            for r in rs if r["kind"] != "discovery"]
        self.assertEqual(strip(rows), strip(canonical))
        self.assertEqual(sum(1 for r in rows if r["kind"] == "discovery"), 12)
        t1 = res["runs"][0]["per_task"]["t1-summarize-alice"]
        self.assertEqual(t1["negotiation"]["discovery"]["calls"], 1)
        self.assertEqual(t1["negotiation"]["discovery"]["before_first_request"], 1)
        self.assertEqual(res["runs"][0]["suite"]["receipt_grounded"]["succeeded"], 9)


class TestNegotiationTelemetry(unittest.TestCase):
    def test_classifies_requests_from_canonical_receipts(self):
        rows = [json.loads(l) for l in open("results/canonical/receipts.jsonl")]
        t2 = negotiation_telemetry(rows, "t2-group-thread-resolution",
                                   experiment_policy())
        s = t2["summary"]
        self.assertEqual(s["requests"], 3)
        self.assertEqual(s["overbroad"], 2)        # messages/* and contacts/*
        self.assertEqual(s["unavailable"], 1)      # messages/* (exact deny)
        self.assertEqual(s["narrowed_after_denial"], 1)
        t6 = negotiation_telemetry(rows, "t6-budget-exhaustion",
                                   experiment_policy())
        self.assertEqual(t6["summary"]["repeated"], 1)
        self.assertEqual(t6["discovery"]["calls"], 0)


class TestCanonicalUnchanged(unittest.TestCase):
    def test_canonical_byte_identical_with_discovery_in_tree(self):
        with tempfile.TemporaryDirectory() as d:
            run_experiment(d)
            for name in ("metrics.json", "receipts.jsonl", "report.md"):
                with open(os.path.join(d, name)) as a, \
                        open(os.path.join("results", "canonical", name)) as b:
                    self.assertEqual(a.read(), b.read(), name)


if __name__ == "__main__":
    unittest.main()
