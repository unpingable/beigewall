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
TRUSTED = {"blackwall.broker", "blackwall.corpus", "blackwall.authority",
           "blackwall.projections", "blackwall.effects", "blackwall.receipts",
           "blackwall.tasks", "blackwall.grants", "blackwall.capabilities"}


def strip(rows):
    return [{k: v for k, v in r.items()
             if k not in ("receipt_id", "seq", "ts", "expires_at", "self_reported")}
            for r in rows if not r["kind"].startswith("ipc_") and r["kind"] != "trusted_restart"]


class TestProcessSeparatedEquivalence(unittest.TestCase):
    def test_scripted_suite_matches_canonical_across_processes(self):
        from blackwall.tasks import task_suite
        from blackwall.evaluator import evaluate_task
        from blackwall.lifecycle_metrics import disagreements
        ids = [t.task_id for t in task_suite()]
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sock"), 0o700)
            sock, ledger = os.path.join(d, "sock", "bw.sock"), os.path.join(d, "receipts.jsonl")
            srv = subprocess.Popen([sys.executable, "-m", "blackwall.ipc.server", "--socket",
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
                    "from blackwall.ipc import agent, client\n"
                    "import blackwall.tasks_public\n"
                    "blackwall.tasks_public.task_scripts()\n"
                    "print(sorted(m for m in sys.modules if m.startswith('blackwall')))")
            out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            loaded = set(eval(out.stdout.strip()))
            self.assertFalse(loaded & TRUSTED, loaded & TRUSTED)

    def test_scripted_data_file_matches_tasks(self):
        from blackwall.tasks import task_suite
        from blackwall.tasks_public import task_scripts
        data = task_scripts()
        for t in task_suite():
            self.assertEqual(data[t.task_id]["script"], t.script)
            self.assertEqual(data[t.task_id]["narrative"], t.narrative)


if __name__ == "__main__":
    unittest.main()
