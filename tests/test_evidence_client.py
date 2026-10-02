"""Phase 6C, untrusted side: a lost response after trusted completion is
recovered on the SAME grant (never a fresh one), and indeterminate /
evidence-unavailable outcomes reach the driver as what they are."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from beigewall.ipc.agent import RemoteFacade
from beigewall.ipc.client import IndeterminateError, RemoteBroker, TransportError
from beigewall.ipc import protocol as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T8 = "t8-send-reply"
THR = "messages/thread/thr-alice"


class _Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bw-evc-")
        os.chmod(self.tmp, 0o700)
        self.sock = os.path.join(self.tmp, "s")
        self.ledger = os.path.join(self.tmp, "receipts.jsonl")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "beigewall.ipc.server", "--socket", self.sock,
             "--ledger", self.ledger, "--condition", "control", "--tasks", T8],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
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

    def rows(self):
        with open(self.ledger, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def prov(self, rb):
        begun = rb.begin_task(T8)
        g = rb.request_grant(T8, THR, "read", projection="conversation@1")["grant_id"]
        h = rb.read(T8, g, THR, "conversation@1")["projection_meta"]["content_hash"]
        return [{"type": "projection", "ref": h}, {"type": "user_input", "ref": begun["prompt_hash"]}]


class LostResponse(_Server):
    def test_lost_response_after_completion_is_recovered_without_duplicate(self):
        rb = RemoteBroker(self.sock)
        prov = self.prov(rb)
        gid = rb.request_grant(T8, THR, "send_message")["grant_id"]
        real = rb._call
        state = {"dropped": False}

        def dropping(op, task_id, args):
            resp = real(op, task_id, args)
            if op == "effect" and not state["dropped"]:
                state["dropped"] = True           # the trusted side answered; we never saw it
                rb._drop()
                raise TransportError("transport failure: result unknown (simulated)")
            return resp
        rb._call = dropping
        out = RemoteFacade(rb, T8).effect(gid, "send_message",
                                          {"thread_id": "thr-alice", "body": "hi"}, prov)
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("recovered"))
        self.assertEqual(out["effect_id"], "e-1")
        rows = self.rows()
        effects = [r for r in rows if r["kind"] == "effect"]
        self.assertEqual(len(effects), 1)
        self.assertEqual(out["receipt_id"], effects[0]["receipt_id"])
        self.assertEqual(rb.telemetry()["effect_recoveries"], 1)
        # the recovery used the same grant: the refusal that resolved it is receipted
        self.assertTrue(any(r["kind"] == "refusal" and "effect grant consumed" in r["reason"]
                            for r in rows))
        rb.close()

    def test_lost_request_before_completion_is_simply_performed_on_retry(self):
        rb = RemoteBroker(self.sock)
        prov = self.prov(rb)
        gid = rb.request_grant(T8, THR, "send_message")["grant_id"]
        real = rb._call
        state = {"n": 0}

        def flaky(op, task_id, args):
            if op == "effect" and state["n"] == 0:
                state["n"] += 1
                rb._drop()                        # never sent
                raise TransportError("transport failure: result unknown (simulated)")
            return real(op, task_id, args)
        rb._call = flaky
        out = RemoteFacade(rb, T8).effect(gid, "send_message",
                                          {"thread_id": "thr-alice", "body": "hi"}, prov)
        self.assertTrue(out["ok"])
        self.assertFalse(out.get("recovered", False))
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "effect"]), 1)
        rb.close()

    def test_client_disappearance_after_acceptance_leaves_one_effect(self):
        rb = RemoteBroker(self.sock)
        prov = self.prov(rb)
        gid = rb.request_grant(T8, THR, "send_message")["grant_id"]
        rb.effect(T8, gid, "send_message", {"thread_id": "thr-alice", "body": "hi"}, prov)
        rb.close()                                 # the client is gone
        rb2 = RemoteBroker(self.sock)             # a successor continues the active task
        out = RemoteFacade(rb2, T8).effect(gid, "send_message",
                                           {"thread_id": "thr-alice", "body": "hi"}, prov)
        self.assertFalse(out["ok"])
        self.assertEqual(out["prior_effect"]["status"], "committed")
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "effect"]), 1)
        rb2.close()


class FacadeOutcomes(unittest.TestCase):
    def test_indeterminate_and_evidence_errors_become_explicit_observations(self):
        class Stub:
            def effect(self, *a):
                raise IndeterminateError("effect e-7 indeterminate", "e-7", "g-3")
        out = RemoteFacade(Stub(), T8).effect("g-3", "send_message", {}, [])
        self.assertEqual((out["ok"], out["indeterminate"], out["effect_id"]), (False, True, "e-7"))
        self.assertIn("do not retry", out["error"])

        from beigewall.ipc.client import EvidenceError

        class Stub2:
            def effect(self, *a):
                raise EvidenceError("evidence unavailable: ENOSPC")
        out = RemoteFacade(Stub2(), T8).effect("g-3", "send_message", {}, [])
        self.assertEqual((out["ok"], out["evidence_unavailable"]), (False, True))

    def test_indeterminate_wire_error_is_decoded(self):
        rb = RemoteBroker.__new__(RemoteBroker)
        err = P.error_response("r1", "indeterminate", "effect e-2 indeterminate")
        err["error"]["effect_id"] = "e-2"
        err["error"]["grant_id"] = "g-9"
        # drive the decode branch of _call directly
        rb._telemetry = {"requests": 0, "latency_ms": [], "bytes_in": 0, "bytes_out": 0,
                         "reconnects": 0, "transport_failures": 0, "effect_recoveries": 0}
        with self.assertRaises(IndeterminateError) as cm:
            RemoteBroker._raise_for(err)
        self.assertEqual((cm.exception.effect_id, cm.exception.grant_id), ("e-2", "g-9"))


if __name__ == "__main__":
    unittest.main()
