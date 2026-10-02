"""Phase 6 process-boundary probes (docs/PROCESS_BOUNDARY.md).

A real `blackwall.ipc.server` subprocess is driven over raw AF_UNIX sockets
by a client assumed compromised. Every probe asserts the exact server reply
and the trusted ledger evidence: the client gains no trusted authority.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_SRC = os.path.join(ROOT, "blackwall", "ipc", "server.py")
AGENT_SRC = os.path.join(ROOT, "run_agent_process.py")
MAX_FRAME = 262144
ALICE = "messages/thread/thr-alice"
GROUP = "messages/thread/thr-group"
def _t8_prompt_hash():
    # computed test-side only to fabricate a *real other-task* hash
    from blackwall.receipts import canonical_hash
    from blackwall.tasks import task_suite
    t8 = next(t for t in task_suite() if t.task_id == "t8-send-reply")
    return canonical_hash({"prompt": t8.prompt})


T8_PROMPT_HASH = _t8_prompt_hash()
BROKER_KINDS = {"grant_request", "grant_mint", "grant_deny", "read", "effect",
                "refusal", "discovery"}


class Conn:
    """Raw framed client: nothing from the real RemoteBroker is used."""

    def __init__(self, path, timeout=5.0):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(path)

    def send_raw(self, data: bytes):
        self.s.sendall(data)

    def send_frame(self, body: bytes):
        self.s.sendall(struct.pack(">I", len(body)) + body)

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.s.recv(n - len(buf))
            except (ConnectionResetError, BrokenPipeError):
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def recv_frame(self):
        head = self._exact(4)
        if head is None:
            return None
        (n,) = struct.unpack(">I", head)
        assert n <= MAX_FRAME, f"server sent oversized frame {n}"
        return self._exact(n)

    def recv_obj(self):
        body = self.recv_frame()
        return None if body is None else json.loads(body)

    def request(self, req: dict) -> dict:
        self.send_frame(json.dumps(req).encode())
        resp = self.recv_obj()
        assert resp is not None, f"connection closed on {req!r}"
        return resp

    def closed(self, within=2.0) -> bool:
        """True if the server closed (EOF/reset) within `within` seconds."""
        self.s.settimeout(within)
        try:
            return self.s.recv(1) == b""
        except (ConnectionResetError, BrokenPipeError):
            return True
        except socket.timeout:
            return False

    def close(self):
        self.s.close()


def req(op, task_id="t1-summarize-alice", rid="r", **args):
    return {"v": 1, "id": rid, "op": op, "task_id": task_id, "args": args}


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SERVER_SRC):
            raise unittest.SkipTest("blackwall/ipc/server.py not present yet")
        cls.tmp = tempfile.mkdtemp(prefix="bw-ipc-")
        os.chmod(cls.tmp, 0o700)
        cls.sock = os.path.join(cls.tmp, "s.sock")
        cls.ledger = os.path.join(cls.tmp, "receipts.jsonl")
        cls.log = open(os.path.join(cls.tmp, "server.log"), "wb")
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "blackwall.ipc.server", "--socket", cls.sock,
             "--ledger", cls.ledger, "--condition", "control",
             "--run-id", "probe"], cwd=ROOT, stdout=cls.log,
            stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while not os.path.exists(cls.sock):
            if cls.proc.poll() is not None or time.monotonic() > deadline:
                cls._stop()
                raise RuntimeError("ipc server did not create its socket")
            time.sleep(0.05)
        # the server owns task identity: each task is begun at most once,
        # one at a time, so every probe uses tasks no other probe touches
        cls.prompt_hash, cls.begun, cls.active = {}, {}, None

    @classmethod
    def _stop(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    # --- helpers -------------------------------------------------------
    def conn(self):
        """The server serves connections serially, so a new probe connection
        first closes the test's previous one."""
        prev = getattr(self, "_cur", None)
        if prev is not None:
            prev.close()
        c = self._cur = Conn(self.sock)
        self.addCleanup(c.close)
        return c

    def activate(self, c, task):
        """End the active task (if another), then begin `task` once."""
        cls = type(self)
        if cls.active == task:
            return cls.begun[task]
        if cls.active is not None:
            r = c.request(req("task_end", cls.active, rid="te", answer="",
                              truncated=False))
            self.assertTrue(r["ok"], r)
            cls.active = None
        r = c.request(req("begin_task", task, rid="bt"))
        self.assertTrue(r["ok"], r)
        cls.active, cls.begun[task] = task, r
        cls.prompt_hash[task] = r["result"]["prompt_hash"]
        return r

    def ledger_rows(self):
        rows = []
        with open(self.ledger, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass        # a line still being appended
        return rows

    def rows_since(self, n, settle=0.0):
        if settle:
            time.sleep(settle)
        return self.ledger_rows()[n:]

    def wait_reject(self, n, reason=None, within=2.0):
        """Poll for a new ipc_reject row after index n; return it."""
        deadline = time.monotonic() + within
        while True:
            rej = [r for r in self.ledger_rows()[n:] if r["kind"] == "ipc_reject"]
            if rej and (reason is None or any(r.get("reason") == reason for r in rej)):
                return rej
            if time.monotonic() > deadline:
                self.fail(f"no ipc_reject {reason or ''} receipted after row {n}")
            time.sleep(0.05)

    def grant(self, c, task, resource=ALICE, operation="read",
              projection="conversation@1", **extra):
        args = {"resource": resource, "operation": operation, **extra}
        if projection is not None:
            args["projection"] = projection
        r = c.request(req("request_grant", task, rid="g", **args))
        self.assertTrue(r["ok"], r)
        return r["result"]

    def read(self, c, task, gid, resource=ALICE, projection="conversation@1"):
        return c.request(req("read", task, rid="rd", grant_id=gid,
                             resource=resource, projection=projection))

    def assertRefusal(self, resp, text):
        self.assertFalse(resp["ok"], resp)
        self.assertEqual(resp["error"]["code"], "refusal", resp)
        self.assertIn(text, resp["error"]["message"])
        rid = resp["error"]["receipt_id"]
        row = next(r for r in self.ledger_rows() if r["receipt_id"] == rid)
        self.assertEqual(row["kind"], "refusal")
        self.assertIn(text, row["reason"])
        return row

    def assertProtocol(self, resp, n, reason=None):
        self.assertIsNotNone(resp, "expected a protocol error response")
        self.assertFalse(resp["ok"], resp)
        self.assertEqual(resp["v"], 1)
        self.assertEqual(resp["error"]["code"], "protocol", resp)
        self.assertNotIn("result", resp)
        return self.wait_reject(n, reason)

    def reads_by(self, gid):
        return [r for r in self.ledger_rows()
                if r["kind"] == "read" and r["grant_id"] == gid]


class TestIpcBoundaryProbes(ServerCase):
    def test_01_forged_grant_id(self):
        c = self.conn()
        self.activate(c, "t12-scope-mismatched-effect")
        other = self.grant(c, "t12-scope-mismatched-effect")["grant_id"]
        self.activate(c, "t1-summarize-alice")
        for gid in ("g-999", other):
            r = self.read(c, "t1-summarize-alice", gid)
            row = self.assertRefusal(r, "unknown grant")
            self.assertEqual(row["task_id"], "t1-summarize-alice")
            self.assertEqual(self.reads_by(gid), [])

    def test_02_forged_receipt_and_cross_task_hash(self):
        c = self.conn()
        self.activate(c, "t10-calendar-crossref")
        ga = self.grant(c, "t10-calendar-crossref")["grant_id"]
        ra = self.read(c, "t10-calendar-crossref", ga)
        self.assertTrue(ra["ok"] and ra["result"]["ok"], ra)
        real = ra["result"]["projection_meta"]["content_hash"]
        task = "t11-fabricated-provenance"
        self.activate(c, task)
        gs = self.grant(c, task, operation="send_message", projection=None)["grant_id"]
        for ref in ("ab" * 32, real):
            r = c.request(req("effect", task, rid="e", grant_id=gs,
                              effect_name="send_message",
                              params={"thread_id": "thr-alice", "body": "x"},
                              derived_from=[{"type": "projection", "ref": ref},
                                            {"type": "user_input",
                                             "ref": self.prompt_hash[task]}]))
            self.assertRefusal(r, "never disclosed")
        self.assertFalse([x for x in self.ledger_rows()
                          if x["kind"] == "effect" and x["task_id"] == task])

    def test_03_fabricated_provenance_and_server_owned_prompt_hash(self):
        c = self.conn()
        task = "t4-schedule-priya"
        self.activate(c, task)
        gs = self.grant(c, task, operation="send_message", projection=None)["grant_id"]
        for ref in ("0" * 64, T8_PROMPT_HASH):
            r = c.request(req("effect", task, rid="e", grant_id=gs,
                              effect_name="send_message",
                              params={"thread_id": "thr-alice", "body": "x"},
                              derived_from=[{"type": "user_input", "ref": ref}]))
            self.assertRefusal(r, "does not match the task prompt hash")
        n = len(self.ledger_rows())
        r = self.conn().request(req("begin_task", task, rid="bt",
                                    prompt_hash="0" * 64))
        self.assertProtocol(r, n, "unknown_arg")
        self.assertFalse([x for x in self.ledger_rows()
                          if x["kind"] == "effect" and x["task_id"] == task])

    def test_04_altered_projection_id(self):
        c = self.conn()
        task = "t5-alice-personal-email"
        self.activate(c, task)
        gid = self.grant(c, task)["grant_id"]
        self.assertRefusal(self.read(c, task, gid, projection="conversation@2"),
                           "projection mismatch")
        self.assertRefusal(self.read(c, task, gid,
                                     projection="conversation@1; import os"),
                           "projection mismatch")
        self.assertEqual(self.reads_by(gid), [])
        self.assertTrue(self.conn().request(req("hello", rid="h"))["ok"])
        self.assertIsNone(self.proc.poll())

    def test_05_altered_resource(self):
        c = self.conn()
        self.activate(c, "t9-unmediated-access")
        gid = self.grant(c, "t9-unmediated-access")["grant_id"]
        self.assertRefusal(self.read(c, "t9-unmediated-access", gid, resource=GROUP),
                           "outside scope")
        self.assertEqual(self.reads_by(gid), [])

    def test_06_altered_operation(self):
        c = self.conn()
        task = "t3-note-followup"
        self.activate(c, task)
        gid = self.grant(c, task)["grant_id"]
        r = c.request(req("effect", task, rid="e", grant_id=gid,
                          effect_name="send_message",
                          params={"thread_id": "thr-alice", "body": "x"},
                          derived_from=[{"type": "user_input",
                                         "ref": self.prompt_hash[task]}]))
        self.assertRefusal(r, "operation mismatch")

    def test_07_altered_scope_at_request_time(self):
        c = self.conn()
        task = "t2-group-thread-resolution"
        self.activate(c, task)
        for res in ("messages/*", "*", "../../etc/passwd"):
            r = c.request(req("request_grant", task, rid="g", resource=res,
                              operation="read", projection="conversation@1"))
            self.assertTrue(r["ok"], r)          # denial is a normal result
            self.assertFalse(r["result"]["ok"])
            self.assertNotIn("grant_id", r["result"])
            self.assertIsInstance(r["result"]["reason"], str)
        rows = [x for x in self.ledger_rows() if x["task_id"] == task]
        self.assertFalse([x for x in rows if x["kind"] in ("grant_mint", "read")])
        self.assertEqual(sum(x["kind"] == "grant_deny" for x in rows), 3)

    def test_08_replayed_read_is_budgeted(self):
        c = self.conn()
        task = "t7-version-pin"
        self.activate(c, task)
        gid = self.grant(c, task, budget={"reads": 8, "objects": 200})["grant_id"]
        frame = json.dumps(req("read", task, rid="same", grant_id=gid,
                               resource=ALICE, projection="conversation@1")).encode()
        hashes = set()
        for _ in range(8):
            c.send_frame(frame)
            r = c.recv_obj()
            self.assertTrue(r["ok"] and r["result"]["ok"], r)
            self.assertEqual(r["id"], "same")
            hashes.add(r["result"]["projection_meta"]["content_hash"])
        c.send_frame(frame)
        self.assertRefusal(c.recv_obj(), "read budget exhausted")
        self.assertEqual(len(hashes), 1)
        self.assertEqual(len(self.reads_by(gid)), 8)

    def test_09_replayed_effect_refused(self):
        c = self.conn()
        task = "t8-send-reply"
        self.activate(c, task)
        gid = self.grant(c, task)["grant_id"]
        h = self.read(c, task, gid)["result"]["projection_meta"]["content_hash"]
        gs = self.grant(c, task, operation="send_message", projection=None)["grant_id"]
        frame = json.dumps(req("effect", task, rid="e1", grant_id=gs,
                               effect_name="send_message",
                               params={"thread_id": "thr-alice", "body": "ok"},
                               derived_from=[{"type": "projection", "ref": h},
                                             {"type": "user_input",
                                              "ref": self.prompt_hash[task]}])).encode()
        c.send_frame(frame)
        first = c.recv_obj()
        self.assertTrue(first["ok"] and first["result"]["ok"], first)
        c.send_frame(frame)
        self.assertRefusal(c.recv_obj(), "effect grant consumed")
        eff = [x for x in self.ledger_rows()
               if x["kind"] == "effect" and x["task_id"] == task]
        self.assertEqual(len(eff), 1)
        self.assertEqual(eff[0]["receipt_id"], first["result"]["receipt_id"])

    def test_10_malformed_json_frames(self):
        for body in (b"{", b"[]", b"null", b"\xff\xfe", b'[{"v":1}]', b'"hello"'):
            with self.subTest(body=body):
                n = len(self.ledger_rows())
                c = self.conn()
                c.send_frame(body)
                self.assertProtocol(c.recv_obj(), n)
                self.assertTrue(c.closed())
                self.assertFalse(BROKER_KINDS & {x["kind"] for x in self.rows_since(n)})

    def test_11_oversized_frame_rejected_before_body(self):
        for length in (MAX_FRAME + 1, 2 ** 31):
            with self.subTest(length=length):
                n = len(self.ledger_rows())
                c = self.conn()
                c.s.settimeout(2.0)
                c.send_raw(struct.pack(">I", length) + b"abc")
                t0 = time.monotonic()
                try:
                    resp = c.recv_obj()
                except socket.timeout:
                    self.fail("server waited for the oversized body")
                self.assertLess(time.monotonic() - t0, 2.0)
                if resp is not None:
                    self.assertEqual(resp["error"]["code"], "protocol")
                    self.assertTrue(c.closed())
                self.wait_reject(n, "frame_too_large")

    def test_12_unexpected_fields(self):
        base = req("hello", rid="u")
        cases = {"extra_top": {**base, "x": 1},
                 "extra_arg": {**base, "args": {"x": 1}},
                 "args_not_object": {**base, "args": []},
                 "missing_v": {k: v for k, v in base.items() if k != "v"},
                 "v2": {**base, "v": 2}, "v_true": {**base, "v": True}}
        for name, obj in cases.items():
            with self.subTest(name):
                n = len(self.ledger_rows())
                r = self.conn().request(obj)
                self.assertProtocol(r, n)
                self.assertFalse(BROKER_KINDS & {x["kind"] for x in self.rows_since(n)})

    def test_13_duplicate_fields(self):
        for body in (b'{"v":1,"id":"d","op":"read","task_id":"t1-summarize-alice",'
                     b'"args":{},"op":"hello"}',
                     b'{"v":1,"id":"d","op":"hello","task_id":"t1-summarize-alice",'
                     b'"args":{"a":1,"a":2}}'):
            with self.subTest(body=body):
                n = len(self.ledger_rows())
                c = self.conn()
                c.send_frame(body)
                rej = self.assertProtocol(c.recv_obj(), n)
                self.assertTrue(any(x.get("reason") == "duplicate_key" for x in rej))

    def test_14_connection_interruption(self):
        n = len(self.ledger_rows())
        c = Conn(self.sock)
        c.send_raw(struct.pack(">I", 100) + b'{"v":1,"id"')
        c.close()
        new = self.rows_since(n, settle=1.0)
        self.assertLessEqual({x["kind"] for x in new}, {"ipc_connection", "ipc_reject"})
        self.assertIsNone(self.proc.poll())
        self.assertTrue(self.conn().request(req("hello", rid="after"))["ok"])

    def test_15_fragmented_frame_reassembled(self):
        c = self.conn()
        body = json.dumps(req("hello", rid="frag")).encode()
        c.send_raw(struct.pack(">I", len(body))[:2])
        time.sleep(0.3)
        c.send_raw(struct.pack(">I", len(body))[2:] + body[:7])
        time.sleep(1.0)
        c.send_raw(body[7:])
        r = c.recv_obj()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["id"], "frag")
        self.assertEqual(r["result"]["protocol"], "blackwall-ipc/1")

    def test_16_response_ids_echo_in_order(self):
        c = self.conn()
        c.send_raw(b"".join(struct.pack(">I", len(b)) + b for b in
                            (json.dumps(req("hello", rid=i)).encode() for i in "ab")))
        ra, rb = c.recv_obj(), c.recv_obj()
        self.assertEqual((ra["id"], rb["id"]), ("a", "b"))
        self.assertNotEqual(ra["id"], "b")
        c.close()
        c1, c2 = Conn(self.sock), Conn(self.sock)
        self.addCleanup(c2.close)
        c1.send_frame(json.dumps(req("hello", rid="x1")).encode())
        c2.send_frame(json.dumps(req("hello", rid="x2")).encode())
        self.assertEqual(c1.recv_obj()["id"], "x1")
        c1.close()                      # serial server: c2 is served next
        self.assertEqual(c2.recv_obj()["id"], "x2")

    def test_17_unavailable_ops(self):
        for op in ("revoke", "task_view", "ledger", "eval", "exec", "__import__",
                   "read_file", "discover", ""):
            with self.subTest(op=op):
                n = len(self.ledger_rows())
                if op == "discover" and type(self).active is None:
                    self.skipTest("discover needs an active task (run the class)")
                # discover is a known op: send it as the active task so the
                # rejection is the control-condition trap, not task identity
                tid = type(self).active if op == "discover" else "t1-summarize-alice"
                r = self.conn().request(req(op, tid, rid="o", grant_id="g-1", reason="x")
                                        if op == "revoke" else req(op, tid, rid="o"))
                rej = self.assertProtocol(r, n)
                if op:  # "" fails the non-empty op check first (bad_op)
                    self.assertTrue(any(x.get("reason") == "unknown_op" for x in rej))
                kinds = {x["kind"] for x in self.rows_since(n)}
                self.assertFalse(kinds & (BROKER_KINDS | {"grant_revoke"}))

    def test_18_trusted_file_access_limitation(self):
        """DOCUMENTED LIMITATION, not a boundary property: this same-user
        process can read the ledger because it was told the path. Separately
        asserted boundary property: no response discloses a filesystem path."""
        c = self.conn()
        task = "t6-budget-exhaustion"
        resps = [c.request(req("hello", task, rid="h")), self.activate(c, task)]
        g = c.request(req("request_grant", task, rid="g", resource=ALICE,
                          operation="read", projection="conversation@1"))
        resps += [g, self.read(c, task, g["result"]["grant_id"]),
                  self.read(c, task, "g-999")]

        def strings(o):
            if isinstance(o, str):
                yield o
            elif isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    yield from strings(v)
            elif isinstance(o, list):
                for v in o:
                    yield from strings(v)
        for s in (s for r in resps for s in strings(r)):
            self.assertFalse(s.startswith("/"), s)
            self.assertNotIn("receipts.jsonl", s)
            self.assertNotIn(self.tmp, s)
        with open(self.ledger, encoding="utf-8") as fh:
            self.assertTrue(fh.readline())

    def test_20_unintended_interface(self):
        n = len(self.ledger_rows())
        c = self.conn()
        c.send_raw(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        resp = c.recv_obj()
        if resp is not None:
            self.assertEqual(resp["error"]["code"], "protocol")
        self.assertTrue(c.closed())
        self.wait_reject(n)
        pid = self.proc.pid
        inodes = set()
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if tgt.startswith("socket:["):
                inodes.add(tgt[8:-1])
        self.assertTrue(inodes, "server holds no sockets?")
        for proto in ("tcp", "tcp6", "udp", "udp6"):
            path = f"/proc/{pid}/net/{proto}"
            if not os.path.exists(path):
                continue
            with open(path) as fh:
                inet = {line.split()[9] for line in list(fh)[1:] if line.strip()}
            self.assertFalse(inodes & inet, f"server holds an {proto} socket")

    def test_zz_invariants(self):
        rows = self.ledger_rows()
        minted = {(r["task_id"], r["grant_id"]) for r in rows if r["kind"] == "grant_mint"}
        for r in rows:
            if r["kind"] == "read":
                self.assertIn((r["task_id"], r["grant_id"]), minted, r)
        self.assertFalse([r for r in rows if r["kind"] == "read"
                          and r["task_id"] not in {t for t, _ in minted}])
        eff = [r for r in rows if r["kind"] == "effect"]
        self.assertEqual([r["task_id"] for r in eff], ["t8-send-reply"])
        rejects = [r for r in rows if r["kind"] == "ipc_reject"]
        self.assertTrue(rejects)
        for r in rejects:
            self.assertIsInstance(r.get("reason"), str, r)
            self.assertTrue(r["reason"], r)
        self.assertFalse([r for r in rows if r["kind"] == "grant_revoke"])
        self.assertIsNone(self.proc.poll())


class TestTaskIdentity(ServerCase):
    """Separate server: these probes need tasks the main class consumes."""

    def test_21_task_relabelling_refused(self):
        c = self.conn()
        t5, t10, t12 = ("t5-alice-personal-email", "t10-calendar-crossref",
                        "t12-scope-mismatched-effect")
        self.activate(c, t5)
        r = c.request(req("request_grant", t5, rid="g", resource="contacts/*",
                          operation="read", projection="contact-directory@1"))
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["result"]["ok"])
        self.assertIn("personal contact data", r["result"]["reason"])
        n = len(self.ledger_rows())
        self.assertProtocol(c.request(req("begin_task", t10, rid="b")), n,
                            "task_already_active")
        self.activate(c, t10)           # ends t5, begins t10
        for tid, reason in ((t5, "task_closed"), (t12, "task_not_active")):
            n = len(self.ledger_rows())
            r = c.request(req("read", tid, rid="x", grant_id="g-1",
                              resource=ALICE, projection="conversation@1"))
            self.assertProtocol(r, n, reason)
            self.assertFalse(BROKER_KINDS & {x["kind"] for x in self.rows_since(n)})
        rows = self.ledger_rows()
        self.assertFalse([x for x in rows if x["kind"] == "read"])
        # the reject row is labelled with the *claimed* task id; nothing else
        # rejected frames carry no task attribution: nothing is filed under
        # t12, and the claimed id is recorded separately
        self.assertEqual({x["kind"] for x in rows if x["task_id"] == t12}, set())
        self.assertTrue(any(x["kind"] == "ipc_reject" and x.get("claimed_task") == t12
                            for x in rows))

    def test_22_task_cannot_be_rebegun(self):
        c = self.conn()
        task = "t1-summarize-alice"
        self.activate(c, task)
        r = c.request(req("task_end", task, rid="te", answer="", truncated=False))
        self.assertTrue(r["ok"], r)
        type(self).active = None
        n = len(self.ledger_rows())
        self.assertProtocol(c.request(req("begin_task", task, rid="b")), n,
                            "task_closed")
        self.assertEqual(sum(x["kind"] == "task_end" and x["task_id"] == task
                             for x in self.ledger_rows()), 1)


class TestAgentImportGraph(unittest.TestCase):
    def test_19_agent_process_imports_no_trusted_module(self):
        if not os.path.exists(AGENT_SRC):
            self.skipTest("run_agent_process.py not present yet")
        out = subprocess.run(
            [sys.executable, "-c", "import run_agent_process, sys; print("
             "'\\n'.join(sorted(m for m in sys.modules if m.startswith('blackwall'))))"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        loaded = set(out.stdout.split())
        trusted = {f"blackwall.{m}" for m in ("broker", "corpus", "authority",
                                              "projections", "effects", "receipts")}
        self.assertFalse(loaded & trusted, sorted(loaded & trusted))


if __name__ == "__main__":
    unittest.main()
