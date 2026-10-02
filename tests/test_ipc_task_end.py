"""Regression (phase 6B real-model run 2): a transport failure on the
narrative call must not skip task_end. The task then stays active in the
trusted process, the next begin_task is refused (task_already_active) and
the untrusted agent loop crashes — a usefulness defect of the untrusted
harness; the trusted side failed closed correctly.

Reproduced against a real server: the client's socket is closed under it
right before the narrative (what the server's idle timeout does to a
client that waited more than READ_TIMEOUT on its model)."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from beigewall.ipc.agent import RemoteHarness
from beigewall.ipc.client import RemoteBroker, TransportError
from beigewall.ipc.protocol import ProtocolError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T1, T3 = "t1-summarize-alice", "t3-note-followup"


class _Driver:
    """Finishes immediately with a fixed answer."""

    def begin(self, view):
        pass

    def act(self, observations):
        return {"op": "final", "answer": "done"}

    def narrate(self):
        return {"claims": [], "declared_effects": [], "summary": "done"}


class _Stub:
    """A broker whose narrative call loses its connection."""

    def __init__(self, narrative_fails=True, end_results=("ok",)):
        self.narrative_fails = narrative_fails
        self.end_results = list(end_results)
        self.calls = []

    def begin_task(self, task_id):
        self.calls.append("begin")
        return {"prompt": "p", "prompt_hash": "h"}

    def narrative(self, *a):
        self.calls.append("narrative")
        if self.narrative_fails:
            raise TransportError("transport failure: result unknown")

    def task_end(self, *a):
        self.calls.append("task_end")
        r = self.end_results.pop(0)
        if r == "transport":
            raise TransportError("transport failure: result unknown")
        if r == "closed":
            raise ProtocolError("task_closed")
        if r == "other":
            raise ProtocolError("task_not_active")


class TaskEndIndependence(unittest.TestCase):
    def test_lost_narrative_still_ends_the_task(self):
        stub = _Stub()
        res = RemoteHarness(stub).run_task(T1, _Driver())
        self.assertEqual(stub.calls, ["begin", "narrative", "task_end"])
        self.assertEqual(res["transport_failures"], 1)

    def test_task_end_retried_once_after_transport_failure(self):
        stub = _Stub(narrative_fails=False, end_results=("transport", "ok"))
        res = RemoteHarness(stub).run_task(T1, _Driver())
        self.assertEqual(stub.calls.count("task_end"), 2)
        self.assertEqual(res["transport_failures"], 1)

    def test_task_closed_on_retry_means_first_attempt_landed(self):
        stub = _Stub(narrative_fails=False, end_results=("transport", "closed"))
        res = RemoteHarness(stub).run_task(T1, _Driver())
        self.assertEqual(stub.calls.count("task_end"), 2)
        self.assertEqual(res["transport_failures"], 1)

    def test_other_protocol_errors_still_raise(self):
        stub = _Stub(narrative_fails=False, end_results=("transport", "other"))
        with self.assertRaises(ProtocolError):
            RemoteHarness(stub).run_task(T1, _Driver())

    def test_second_transport_failure_gives_up(self):
        stub = _Stub(narrative_fails=False, end_results=("transport", "transport"))
        res = RemoteHarness(stub).run_task(T1, _Driver())
        self.assertEqual(stub.calls.count("task_end"), 2)
        self.assertEqual(res["transport_failures"], 2)


class AgainstRealServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bw-end-")
        os.chmod(self.tmp, 0o700)
        self.sock = os.path.join(self.tmp, "s")
        self.ledger = os.path.join(self.tmp, "receipts.jsonl")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "beigewall.ipc.server", "--socket", self.sock,
             "--ledger", self.ledger, "--condition", "control",
             "--tasks", f"{T1},{T3}"], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while not os.path.exists(self.sock):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.fail("server did not start")
            time.sleep(0.02)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connection_lost_before_narrative_then_next_task_begins(self):
        rb = RemoteBroker(self.sock)
        harness = RemoteHarness(rb)

        class _LosesConnection(_Driver):
            def narrate(inner):
                rb._sock.close()      # the server dropped us; we do not know yet
                return super().narrate()

        res = harness.run_task(T1, _LosesConnection())
        self.assertEqual(res["transport_failures"], 1)
        # the next designated task begins normally: T1 was ended
        res2 = harness.run_task(T3, _Driver())
        self.assertEqual(res2["transport_failures"], 0)
        rb.close()
        with open(self.ledger, encoding="utf-8") as fh:
            rows = [__import__("json").loads(l) for l in fh if l.strip()]
        ends = [r["task_id"] for r in rows if r["kind"] == "task_end"]
        self.assertEqual(ends, [T1, T3])
        self.assertEqual(sum(1 for r in rows if r["kind"] == "narrative"), 1)


if __name__ == "__main__":
    unittest.main()
