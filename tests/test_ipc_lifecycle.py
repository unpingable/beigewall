"""Lifecycle tests for the phase 6 process boundary, with real subprocesses.

Covers start order (server first, client first), trusted restart mid-run,
connection drop, a client dying during an effect, duplicate requests after
reconnect, and the stale-response guard. Every test bounds its own waiting
(each finishes well inside 15 s). The module is skipped when the IPC server
module has not landed. Contract assumptions about the not-yet-written client
are listed in the individual helpers; raw frames use beigewall.ipc.protocol.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists(os.path.join(ROOT, "beigewall", "ipc", "server.py")):
    raise unittest.SkipTest("beigewall/ipc/server.py does not exist yet: "
                            "IPC lifecycle tests need the trusted server")

T1 = "t1-summarize-alice"
T3 = "t3-note-followup"
ALICE = "messages/thread/thr-alice"


def _ipc_tele(rb) -> dict:
    """RemoteBroker.telemetry() is flat today; tolerate an {"ipc": ...} wrap."""
    tele = rb.telemetry()
    return tele.get("ipc", tele)


def _prompt(task_id: str) -> str:
    from beigewall.tasks import task_suite
    return next(t.prompt for t in task_suite() if t.task_id == task_id)


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bwl-")
        os.chmod(self.tmp, 0o700)
        self.sock_dir = os.path.join(self.tmp, "sock")
        os.mkdir(self.sock_dir, 0o700)
        self.sock = os.path.join(self.sock_dir, "s")
        self.ledger = os.path.join(self.tmp, "receipts.jsonl")
        self._procs: list[subprocess.Popen] = []
        self._brokers = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for b in self._brokers:
            try:
                b.close()
            except Exception:
                pass
        for p in self._procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- process helpers ------------------------------------------------

    def start_server(self, condition="control", tag="server"):
        log = open(os.path.join(self.tmp, f"{tag}.log"), "a")
        self.addCleanup(log.close)
        p = subprocess.Popen(
            [sys.executable, "-m", "beigewall.ipc.server", "--socket",
             self.sock, "--ledger", self.ledger, "--condition", condition,
             "--run-id", "lifecycle"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        self._procs.append(p)
        return p

    def wait_socket(self, server, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if server.poll() is not None:
                self.fail(f"server exited early: {server.returncode}")
            if os.path.exists(self.sock):
                try:
                    s = socket.socket(socket.AF_UNIX)
                    s.settimeout(1)
                    s.connect(self.sock)
                    s.close()
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        self.fail("server socket never became connectable")

    def start_agent(self, tasks, condition="control"):
        p = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "run_agent_process.py"),
             "--socket", self.sock, "--condition", condition,
             "--driver", "scripted", "--tasks", ",".join(tasks)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._procs.append(p)
        return p

    def finish(self, proc, timeout=12.0):
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail(f"process hung: {err.decode()[-400:]}")
        return proc.returncode, out.decode(), err.decode()

    def stop_server(self, server, timeout=8.0):
        server.send_signal(signal.SIGTERM)
        try:
            return server.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail("server ignored SIGTERM")

    def rows(self):
        try:
            with open(self.ledger) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []

    def wait_for_row(self, pred, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if any(pred(r) for r in self.rows()):
                return True
            time.sleep(0.05)
        return False

    # --- raw protocol helpers (no dependency on the client library) -----

    def raw_connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self.sock)
        self.addCleanup(s.close)
        return s

    _n = 0

    def raw_send(self, s, op, task_id, args, req_id=None):
        from beigewall.ipc.protocol import encode_frame
        type(self)._n += 1
        req_id = req_id or f"raw-{type(self)._n}"
        s.sendall(encode_frame({"v": 1, "id": req_id, "op": op,
                                "task_id": task_id, "args": args}))
        return req_id

    def raw_call(self, s, op, task_id, args, req_id=None):
        from beigewall.ipc.protocol import read_frame
        self.raw_send(s, op, task_id, args, req_id)
        body = read_frame(s)
        self.assertIsNotNone(body, "connection closed without a response")
        return json.loads(body)

    def remote(self):
        from beigewall.ipc.client import RemoteBroker
        rb = RemoteBroker(self.sock)
        self._brokers.append(rb)
        return rb

    def evaluate(self, task_id):
        from beigewall.evaluator import evaluate_task
        return evaluate_task(self.rows(), task_id, _prompt(task_id))

    def check_ledger_continuity(self):
        rows = self.rows()
        ids = [r["receipt_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "receipt id collision")
        seqs = [r["seq"] for r in rows]
        self.assertEqual(seqs, sorted(set(seqs)), "seq not strictly increasing")
        ts = [r["ts"] for r in rows]
        self.assertTrue(all(b >= a for a, b in zip(ts, ts[1:])),
                        "logical clock regressed")
        return rows


class TestStartOrder(_Env):
    def test_a_server_before_client(self):
        from beigewall.tasks import run_suite
        server = self.start_server()
        self.wait_socket(server)
        agent = self.start_agent([T1, T3])
        rc, out, err = self.finish(agent)
        self.assertEqual(rc, 0, err[-400:])
        self.assertEqual(self.stop_server(server), 0)
        ends = {r["task_id"] for r in self.rows() if r["kind"] == "task_end"}
        self.assertEqual(ends, {T1, T3})
        # canonical-equivalent outcomes, computed in-process for comparison
        from beigewall.evaluator import evaluate_task
        from beigewall.tasks import task_suite
        _, _, led = run_suite()
        canon_rows = [r.to_dict() for r in led.all()]
        for tid in (T1, T3):
            want = evaluate_task(canon_rows, tid, _prompt(tid))
            got = self.evaluate(tid)
            self.assertEqual(got["outcome"], want["outcome"], tid)
            self.assertEqual(got["admissibility"]["verdict"],
                             want["admissibility"]["verdict"], tid)
        self.assertEqual(self.evaluate(T1)["admissibility"]["verdict"],
                         "INADMISSIBLE")
        self.assertEqual(self.evaluate(T3)["outcome"], "SUCCESS")
        self.assertTrue(task_suite())

    def test_b_client_before_server(self):
        agent = self.start_agent([T1])
        time.sleep(2.0)
        self.assertIsNone(agent.poll(), "agent gave up before the server came")
        server = self.start_server()
        rc, out, err = self.finish(agent)
        self.assertEqual(rc, 0, err[-400:])
        self.stop_server(server)
        self.assertTrue(any(r["kind"] == "task_end" and r["task_id"] == T1
                            for r in self.rows()))


class TestTrustedRestart(_Env):
    def test_c_restart_mid_run(self):
        server = self.start_server()
        self.wait_socket(server)
        agent = self.start_agent([T1])
        rc, _, err = self.finish(agent)
        self.assertEqual(rc, 0, err[-400:])
        self.assertTrue(self.wait_for_row(
            lambda r: r["kind"] == "task_end" and r["task_id"] == T1))
        before = len(self.rows())
        self.stop_server(server)

        server2 = self.start_server(tag="server2")
        self.wait_socket(server2)
        agent2 = self.start_agent([T3])
        rc, _, err = self.finish(agent2)
        self.assertEqual(rc, 0, err[-400:])
        self.stop_server(server2)

        rows = self.check_ledger_continuity()
        self.assertGreater(len(rows), before)
        self.assertTrue(any(r["kind"] == "trusted_restart" for r in rows))
        ends = {r["task_id"] for r in rows if r["kind"] == "task_end"}
        self.assertEqual(ends, {T1, T3})
        from beigewall.lifecycle_metrics import disagreements
        self.assertEqual(disagreements(rows), [])


class TestTransport(_Env):
    def test_d_connection_drop_and_no_authority_after_restart(self):
        server = self.start_server()
        self.wait_socket(server)
        rb = self.remote()
        rb.begin_task(T1)
        grant = rb.request_grant(T1, ALICE, "read", projection="conversation@1")
        gid = grant["grant_id"]
        server.kill()
        server.wait()

        outcome = {}

        def attempt():
            try:
                rb.read(T1, gid, ALICE, "conversation@1")
                outcome["result"] = "returned"
            except BaseException as exc:      # transport error of any class
                outcome["exc"] = exc

        t0 = time.time()
        th = threading.Thread(target=attempt, daemon=True)
        th.start()
        th.join(5.5)
        self.assertFalse(th.is_alive(), "read hung after the server died")
        self.assertLess(time.time() - t0, 5.5)
        self.assertIn("exc", outcome, "read succeeded against a dead server")
        from beigewall.ipc.client import RefusalError, TransportError
        self.assertNotIsInstance(outcome["exc"], RefusalError)
        self.assertIsInstance(outcome["exc"], TransportError)
        self.assertEqual(_ipc_tele(rb)["transport_failures"], 1)

        server2 = self.start_server(tag="server2")
        self.wait_socket(server2)
        s = self.raw_connect()
        # a task interrupted by a trusted restart is closed (its grants are
        # gone; re-running it would merge two executions): re-beginning it
        # is refused, and the old grant is unknown to the next task
        closed = self.raw_call(s, "begin_task", T1, {})
        self.assertFalse(closed["ok"])
        self.assertEqual(closed["error"]["message"], "task_closed")
        self.assertTrue(self.raw_call(s, "begin_task", T3, {})["ok"])
        resp = self.raw_call(s, "read", T3,
                             {"grant_id": gid, "resource": ALICE,
                              "projection": "conversation@1"})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown grant", resp["error"]["message"])
        self.assertTrue(any(r["kind"] == "trusted_restart"
                            for r in self.rows()))

    def _setup_effect(self):
        server = self.start_server()
        self.wait_socket(server)
        rb = self.remote()
        info = rb.begin_task(T1)
        rg = rb.request_grant(T1, ALICE, "read", projection="conversation@1")
        sg = rb.request_grant(T1, ALICE, "send_message")
        read = rb.read(T1, rg["grant_id"], ALICE, "conversation@1")
        args = {"grant_id": sg["grant_id"], "effect_name": "send_message",
                "params": {"thread_id": "thr-alice", "body": "hello"},
                "derived_from": [
                    {"type": "projection",
                     "ref": read["projection_meta"]["content_hash"]},
                    {"type": "user_input", "ref": info["prompt_hash"]}]}
        # the server serves one connection at a time: release this one
        rb.close()
        return server, rb, args

    def test_e_client_dies_during_effect(self):
        server, rb, args = self._setup_effect()
        s = self.raw_connect()
        self.raw_send(s, "effect", T1, args)
        s.close()                       # never reads the response
        self.assertTrue(self.wait_for_row(lambda r: r["kind"] == "effect"),
                        "effect frame was not processed")
        s2 = self.raw_connect()
        resp = self.raw_call(s2, "effect", T1, args)
        self.assertFalse(resp["ok"])
        self.assertIn("effect grant consumed", resp["error"]["message"])
        effects = [r for r in self.rows() if r["kind"] == "effect"]
        self.assertEqual(len(effects), 1)

    def test_f_duplicate_request_after_reconnect(self):
        server = self.start_server()
        self.wait_socket(server)
        args = {"resource": ALICE, "operation": "read",
                "projection": "conversation@1"}
        s1 = self.raw_connect()
        self.assertTrue(self.raw_call(s1, "begin_task", T1, {})["ok"])
        r1 = self.raw_call(s1, "request_grant", T1, args)
        s1.close()
        s2 = self.raw_connect()
        r2 = self.raw_call(s2, "request_grant", T1, args)
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertNotEqual(r1["result"]["grant_id"], r2["result"]["grant_id"])
        rows = self.rows()
        self.assertEqual(sum(1 for r in rows if r["kind"] == "grant_request"
                             and r["task_id"] == T1), 2)
        minted = {r["grant_id"] for r in rows if r["kind"] == "grant_mint"}
        self.assertEqual(minted, {r1["result"]["grant_id"],
                                  r2["result"]["grant_id"]})

    def test_g_stale_response_is_not_matched(self):
        from beigewall.ipc.client import RemoteBroker
        self.assertTrue(
            hasattr(RemoteBroker, "_match_response"),
            "RemoteBroker._match_response(pending_id, response) is the "
            "stale-response guard this test depends on; it is missing")
        # the guard is stateless on the instance: bypass the connecting ctor
        rb = RemoteBroker.__new__(RemoteBroker)
        good = {"v": 1, "id": "req-7", "ok": True, "result": {}}
        stale = {"v": 1, "id": "req-6", "ok": True, "result": {}}
        self.assertIs(rb._match_response("req-7", good), True)
        self.assertIs(rb._match_response("req-7", stale), False)
        self.assertIs(rb._match_response("req-7", {"v": 1, "ok": True}), False)


if __name__ == "__main__":
    unittest.main()
