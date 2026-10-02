"""Process-separated run of the scripted suite reproduces the canonical
ledger (modulo sequence, clock, IPC connection rows and the `self_reported`
marker on advisory unmediated-attempt rows); the evaluator and the observer
agree exactly. The untrusted process imports no trusted module."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRUSTED = {"beigewall.broker", "beigewall.corpus", "beigewall.authority",
           "beigewall.projections", "beigewall.effects", "beigewall.receipts",
           "beigewall.tasks", "beigewall.grants", "beigewall.capabilities"}


# The trusted server adds evidence the frozen in-process run does not have
# (phase 6: ipc_* and trusted_restart rows; phase 6C: effect_intent rows and
# the effect_id on commits/aborts). Everything else must be identical.
SERVER_ONLY_KINDS = ("trusted_restart", "effect_intent", "effect_indeterminate")
SERVER_ONLY_KEYS = ("effect_id",)


def intents_resolved(rows) -> bool:
    """Every effect intent is resolved by exactly one commit or abort of the
    same task and grant (phase 6C): an open intent is never silently equal
    to an effect that was not attempted."""
    intents = {r["effect_id"]: r for r in rows if r["kind"] == "effect_intent"}
    resolved = {}
    for r in rows:
        if r["kind"] in ("effect", "refusal") and r.get("effect_id"):
            it = intents.get(r["effect_id"])
            if it is None or it["task_id"] != r["task_id"] or it["grant_id"] != r.get("grant_id"):
                return False
            resolved[r["effect_id"]] = resolved.get(r["effect_id"], 0) + 1
    return all(resolved.get(eid) == 1 for eid in intents)


def strip(rows):
    return [{k: v for k, v in r.items()
             if k not in ("receipt_id", "seq", "ts", "expires_at", "self_reported")
             and k not in SERVER_ONLY_KEYS}
            for r in rows if not r["kind"].startswith("ipc_")
            and r["kind"] not in SERVER_ONLY_KINDS]


class TestProcessSeparatedEquivalence(unittest.TestCase):
    def test_scripted_suite_matches_canonical_across_processes(self):
        from beigewall.tasks import task_suite
        from beigewall.evaluator import evaluate_task
        from beigewall.lifecycle_metrics import disagreements
        ids = [t.task_id for t in task_suite()]
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sock"), 0o700)
            sock, ledger = os.path.join(d, "sock", "bw.sock"), os.path.join(d, "receipts.jsonl")
            srv = subprocess.Popen([sys.executable, "-m", "beigewall.ipc.server", "--socket",
                                    sock, "--ledger", ledger, "--condition", "control"], cwd=ROOT)
            try:
                for _ in range(100):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.1)
                agent = subprocess.run([sys.executable, "run_agent_process.py", "--socket", sock,
                                        "--condition", "control", "--driver", "scripted",
                                        "--tasks", ",".join(ids)], cwd=ROOT,
                                       capture_output=True, text=True, timeout=120)
                self.assertEqual(agent.returncode, 0, agent.stderr)
            finally:
                srv.terminate()
                srv.wait(timeout=10)
            rows = [json.loads(l) for l in open(ledger)]
            canonical = [json.loads(l) for l in open(os.path.join(ROOT, "results", "canonical",
                                                                    "receipts.jsonl"))]
            self.assertEqual(strip(rows), strip(canonical))
            self.assertTrue(intents_resolved(rows))
            self.assertEqual(sum(1 for r in rows if r["kind"] == "effect_intent"),
                             sum(1 for r in canonical if r["kind"] == "effect"))
            self.assertTrue(all(r.get("self_reported") for r in rows
                                if r["kind"] == "unmediated_attempt"))
            for t in task_suite():
                here = evaluate_task(rows, t.task_id, t.prompt)
                there = evaluate_task(canonical, t.task_id, t.prompt)
                self.assertEqual((here["outcome"], here["admissibility"]["verdict"],
                                  here["usefulness"]["verdict"]),
                                 (there["outcome"], there["admissibility"]["verdict"],
                                  there["usefulness"]["verdict"]), t.task_id)
            self.assertEqual(disagreements(rows), [])
            tele = [json.loads(l) for l in agent.stdout.splitlines() if l.strip()]
            self.assertEqual(len(tele), 12)
            self.assertTrue(all(t["ipc"]["transport_failures"] == 0 for t in tele))

    def test_agent_process_imports_no_trusted_module(self):
        for driver in ("scripted", "llm"):
            code = ("import sys, run_agent_process\n"
                    "from beigewall.ipc import agent, client\n"
                    "import beigewall.tasks_public\n"
                    "beigewall.tasks_public.task_scripts()\n"
                    "print(sorted(m for m in sys.modules if m.startswith('beigewall')))")
            out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            loaded = set(eval(out.stdout.strip()))
            self.assertFalse(loaded & TRUSTED, loaded & TRUSTED)

    def test_scripted_data_file_matches_tasks(self):
        from beigewall.tasks import task_suite
        from beigewall.tasks_public import task_scripts
        data = task_scripts()
        for t in task_suite():
            self.assertEqual(data[t.task_id]["script"], t.script)
            self.assertEqual(data[t.task_id]["narrative"], t.narrative)


class TestMalformedProvenanceCrossesTheBoundary(unittest.TestCase):
    """A model-style malformed derived_from must reach the broker and be
    refused with its receipt (as in-process), not vanish as a protocol
    rejection; and a protocol rejection must be an observation, not a crash."""

    def test_malformed_derived_from_is_a_broker_refusal(self):
        from beigewall.ipc import protocol as P
        req = {"v": 1, "id": "a", "op": "effect", "task_id": "t8-send-reply",
               "args": {"grant_id": "g-1", "effect_name": "send_message",
                        "params": {"thread_id": "thr-alice", "body": "x"},
                        "derived_from": ["d435cdd1", {"ref": "x"}, 7]}}
        self.assertEqual(P.decode(json.dumps(req).encode())["op"], "effect")
        with self.assertRaises(P.ProtocolError):
            P.decode(json.dumps({**req, "args": {**req["args"],
                                                 "derived_from": [[[[[[[[1]]]]]]]]}}).encode())

    def test_facade_turns_protocol_rejection_into_an_observation(self):
        from beigewall.ipc.agent import RemoteFacade
        from beigewall.ipc.protocol import ProtocolError

        class Stub:
            def effect(self, *a, **k):
                raise ProtocolError("unknown_arg")

            def read(self, *a, **k):
                raise ProtocolError("bad_type:grant_id")
        f = RemoteFacade(Stub(), "t1-summarize-alice")
        obs = f.effect("g-1", "send_message", {}, [])
        self.assertFalse(obs["ok"])
        self.assertTrue(obs["malformed"] and obs["protocol_rejected"])
        self.assertFalse(f.read(5, "x", "y")["ok"])

    def test_end_to_end_malformed_provenance_receipted(self):
        from beigewall.tasks import task_suite
        ids = ["t8-send-reply"]
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sock"), 0o700)
            sock, ledger = os.path.join(d, "sock", "bw.sock"), os.path.join(d, "receipts.jsonl")
            srv = subprocess.Popen([sys.executable, "-m", "beigewall.ipc.server", "--socket",
                                    sock, "--ledger", ledger, "--condition", "control"], cwd=ROOT)
            try:
                for _ in range(100):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.1)
                from beigewall.ipc.client import RemoteBroker, RefusalError
                b = RemoteBroker(sock)
                b.begin_task("t8-send-reply")
                g = b.request_grant("t8-send-reply", "messages/thread/thr-alice", "read",
                                    projection="conversation@1")["grant_id"]
                b.read("t8-send-reply", g, "messages/thread/thr-alice", "conversation@1")
                gs = b.request_grant("t8-send-reply", "messages/thread/thr-alice",
                                     "send_message")["grant_id"]
                with self.assertRaises(RefusalError) as cm:
                    b.effect("t8-send-reply", gs, "send_message",
                             {"thread_id": "thr-alice", "body": "x"}, ["not-a-dict"])
                self.assertIn("malformed derived_from", str(cm.exception))
                b.close()
            finally:
                srv.terminate()
                srv.wait(timeout=10)
            rows = [json.loads(l) for l in open(ledger)]
            self.assertTrue(any(r["kind"] == "refusal" and "malformed derived_from" in r["reason"]
                                for r in rows))
            self.assertFalse(any(r["kind"] == "ipc_reject" for r in rows))


if __name__ == "__main__":
    unittest.main()
