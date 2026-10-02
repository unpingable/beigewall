"""Regression tests for the phase 6 hostile-review fixes."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from beigewall.ipc import protocol as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def req(op, task="t1-summarize-alice", rid="r", **args):
    return {"v": 1, "id": rid, "op": op, "task_id": task, "args": args}


def decode(obj):
    return P.decode(json.dumps(obj).encode())


def decode_raw(text):
    return P.decode(text.encode())


class TestProtocolHardening(unittest.TestCase):
    def test_nested_non_finite_and_constants_rejected(self):
        for text in ('{"v":1,"id":"a","op":"narrative","task_id":"t","args":{"claims":[],"declared_effects":[],"summary":"x","extra":NaN}}',
                     '{"v":1,"id":"a","op":"effect","task_id":"t","args":{"grant_id":"g","effect_name":"send_message","params":{"body":Infinity},"derived_from":[]}}',
                     '{"v":1,"id":"a","op":"effect","task_id":"t","args":{"grant_id":"g","effect_name":"send_message","params":{"n":1e400},"derived_from":[]}}'):
            with self.assertRaises(P.ProtocolError):
                decode_raw(text)

    def test_surrogates_and_huge_numbers_rejected(self):
        with self.assertRaises(P.ProtocolError):
            decode(req("request_grant", resource="thr-\ud800", operation="read"))
        with self.assertRaises(P.ProtocolError):
            decode(req("request_grant", resource="x", operation="read", ttl=10 ** 400))
        with self.assertRaises(P.ProtocolError):
            decode(req("request_grant", resource="x", operation="read",
                       budget={"reads": 10 ** 18, "objects": 1}))
        with self.assertRaises(P.ProtocolError):
            decode(req("effect", grant_id="g", effect_name="send_message",
                       params={"k": 10 ** 400}, derived_from=[]))

    def test_version_must_be_int_and_id_echoed_on_rejection(self):
        with self.assertRaises(P.ProtocolError):
            decode({**req("hello"), "v": 1.0})
        try:
            decode(req("read", rid="echo-me", grant_id="g", resource="x", projection="p", extra=1))
        except P.ProtocolError as exc:
            self.assertEqual(exc.req_id, "echo-me")
        else:
            self.fail("not rejected")

    def test_claims_must_be_shaped(self):
        bad = [["I read nothing"], [{"resource": 1}], [{"resource": "r", "objects": "x"}],
               [{"resource": "r", "other": 1}]]
        for claims in bad:
            with self.assertRaises(P.ProtocolError):
                decode(req("narrative", claims=claims, declared_effects=[], summary=""))
        decode(req("narrative", claims=[{"resource": "r", "objects": ["a"], "fields": ["f"]}],
                   declared_effects=["send_message"], summary="ok"))

    def test_narration_tolerates_non_object_claims(self):
        from beigewall.narration import compare_narrative
        from beigewall.receipts import Ledger
        ledger = Ledger(clock=lambda: 0.0)
        ledger.record("narrative", "t1", claims=["I read nothing"], declared_effects=[],
                      summary="")
        out = compare_narrative(ledger, "t1")
        self.assertEqual(out["counts"]["imprecise_attribution"], 1)


class _Server:
    def __init__(self, condition="control", tasks=None, ledger=None, d=None):
        self.d = d or tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "sock"), exist_ok=True)
        self.sock = os.path.join(self.d, "sock", "bw.sock")
        self.ledger = ledger or os.path.join(self.d, "receipts.jsonl")
        cmd = [sys.executable, "-m", "beigewall.ipc.server", "--socket", self.sock,
               "--ledger", self.ledger, "--condition", condition]
        if tasks:
            cmd += ["--tasks", ",".join(tasks)]
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            if os.path.exists(self.sock) or self.proc.poll() is not None:
                break
            time.sleep(0.05)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)

    def rows(self):
        return [json.loads(l) for l in open(self.ledger)]


class TestTaskIdentity(unittest.TestCase):
    def test_designated_once_active_closed(self):
        from beigewall.ipc.client import RemoteBroker
        s = _Server(tasks=["t5-alice-personal-email", "t10-calendar-crossref"])
        try:
            b = RemoteBroker(s.sock)
            b.begin_task("t5-alice-personal-email")
            with self.assertRaises(P.ProtocolError):           # another task while t5 active
                b.begin_task("t10-calendar-crossref")
            with self.assertRaises(P.ProtocolError):           # not designated
                b.begin_task("t1-summarize-alice")
            with self.assertRaises(P.ProtocolError):           # op for a non-active task
                b.request_grant("t10-calendar-crossref", "contacts/*", "read",
                                projection="contact-directory@1")
            denied = b.request_grant("t5-alice-personal-email", "contacts/*", "read",
                                     projection="contact-directory@1")
            self.assertFalse(denied["ok"])                     # t5's policy still applies
            b.task_end("t5-alice-personal-email", "", False)
            with self.assertRaises(P.ProtocolError):           # closed stays closed
                b.begin_task("t5-alice-personal-email")
            with self.assertRaises(P.ProtocolError):           # ops on a closed task
                b.request_grant("t5-alice-personal-email", "messages/thread/thr-alice", "read",
                                projection="conversation@1")
            b.begin_task("t10-calendar-crossref")
            ok = b.request_grant("t10-calendar-crossref", "contacts/*", "read",
                                 projection="contact-directory@1")
            self.assertTrue(ok["ok"])                           # t10 legitimately may
            b.close()
            rows = s.rows()
            self.assertFalse(any(r["kind"] == "read" and r["task_id"] == "t5-alice-personal-email"
                                 for r in rows))
            self.assertTrue(any(r["kind"] == "ipc_reject" and r["reason"] == "task_closed"
                                for r in rows))
        finally:
            s.stop()

    def test_closure_survives_restart_and_second_server_refused(self):
        from beigewall.ipc.client import RemoteBroker
        s = _Server(tasks=["t1-summarize-alice", "t3-note-followup"])
        try:
            b = RemoteBroker(s.sock)
            b.begin_task("t1-summarize-alice")
            b.task_end("t1-summarize-alice", "done", False)
            b.close()
            # a second server on the same ledger while the first lives: refused
            dup = subprocess.run([sys.executable, "-m", "beigewall.ipc.server", "--socket",
                                  s.sock + "2", "--ledger", s.ledger, "--condition", "control"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(dup.returncode, 0)
            self.assertIn("owned by another server", dup.stdout + dup.stderr)
        finally:
            s.stop()
        s2 = _Server(tasks=["t1-summarize-alice", "t3-note-followup"], ledger=s.ledger, d=s.d)
        try:
            b = RemoteBroker(s2.sock)
            with self.assertRaises(P.ProtocolError):           # closed before the restart
                b.begin_task("t1-summarize-alice")
            b.begin_task("t3-note-followup")
            b.close()
            self.assertTrue(any(r["kind"] == "trusted_restart" for r in s2.rows()))
        finally:
            s2.stop()

    def test_rejected_frames_carry_no_task_attribution_and_do_not_close_tasks(self):
        from beigewall.ipc.client import RemoteBroker
        s = _Server(tasks=["t1-summarize-alice", "t3-note-followup"])
        try:
            b = RemoteBroker(s.sock)
            with self.assertRaises(P.ProtocolError):   # claims t3 while nothing is active
                b.request_grant("t3-note-followup", "notes/note-1", "read", projection="note@1")
            b.close()
            rows = s.rows()
            rej = [r for r in rows if r["kind"] == "ipc_reject"]
            self.assertTrue(rej and all(r["task_id"] == "" for r in rej))
            self.assertEqual(rej[-1]["claimed_task"], "t3-note-followup")
            self.assertFalse(any(r["task_id"] == "t3-note-followup" for r in rows))
        finally:
            s.stop()
        s2 = _Server(tasks=["t1-summarize-alice", "t3-note-followup"], ledger=s.ledger, d=s.d)
        try:
            b = RemoteBroker(s2.sock)
            self.assertIn("prompt", b.begin_task("t3-note-followup"))   # not locked out
            b.close()
        finally:
            s2.stop()

    def test_task_view_delivered_over_ipc(self):
        from beigewall.ipc.client import RemoteBroker
        s = _Server(condition="task", tasks=["t9-unmediated-access"])
        try:
            b = RemoteBroker(s.sock)
            res = b.begin_task("t9-unmediated-access")
            self.assertEqual(res["capabilities"]["schema"], "capability-view@1")
            self.assertEqual(res["capabilities"]["scope"], ["notes"])
            b.close()
            self.assertTrue(any(r["kind"] == "discovery" and r.get("trigger") == "initial"
                                for r in s.rows()))
        finally:
            s.stop()

    def test_agent_assertions_marked_and_live_socket_not_taken_over(self):
        from beigewall.ipc.client import RemoteBroker
        s = _Server(tasks=["t1-summarize-alice"])
        try:
            b = RemoteBroker(s.sock)
            b.begin_task("t1-summarize-alice")
            b.narrative("t1-summarize-alice", [], [], "s")
            b.task_end("t1-summarize-alice", "a", False)
            b.close()
            rows = s.rows()
            self.assertTrue(all(r.get("self_reported") for r in rows
                                if r["kind"] in ("narrative", "task_end")))
            other = subprocess.run([sys.executable, "-m", "beigewall.ipc.server", "--socket",
                                    s.sock, "--ledger", s.ledger + ".other", "--condition",
                                    "control"], cwd=ROOT, capture_output=True, text=True,
                                   timeout=10)
            self.assertNotEqual(other.returncode, 0)
            self.assertTrue(os.path.exists(s.sock))             # the live socket survived
        finally:
            s.stop()


if __name__ == "__main__":
    unittest.main()
