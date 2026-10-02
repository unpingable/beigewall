"""Phase 7 transport unit tests: the model relay's request discipline and
the vsock argument handling on the trusted server and client. These do not
need a VM or a vsock device; the end-to-end vsock path is exercised by the
VM experiment (run_vm_experiment.py). Everything here is pure logic or runs
over a socketpair / a monkeypatched upstream."""

import json
import socket
import threading
import unittest

from blackwall import model_relay as R
from blackwall.ipc import server as S


def _good_body(model="m", extra=None):
    obj = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if extra:
        obj.update(extra)
    return json.dumps(obj).encode()


def _request(method="POST", target=R.PATH, body=None, headers=None, raw_head=None):
    body = _good_body() if body is None else body
    if raw_head is not None:
        return raw_head + b"\r\n\r\n" + body
    hdr = [f"{method} {target} HTTP/1.1", "Host: x",
           f"Content-Length: {len(body)}"]
    for k, v in (headers or {}).items():
        hdr.append(f"{k}: {v}")
    return ("\r\n".join(hdr) + "\r\n\r\n").encode() + body


def _feed(raw):
    """Return a connected socket whose peer has already sent `raw`."""
    a, b = socket.socketpair()
    b.sendall(raw)
    b.shutdown(socket.SHUT_WR)
    return a, b


class TestValidate(unittest.TestCase):
    def test_accepts_canonical_request(self):
        out = R._validate("POST", R.PATH, _good_body("m"), "m")
        self.assertEqual(json.loads(out)["model"], "m")

    def test_accepts_tools_key(self):
        body = _good_body("m", {"tools": [{"type": "function"}]})
        out = R._validate("POST", R.PATH, body, "m")
        self.assertIn("tools", json.loads(out))

    def test_rejects_other_method(self):
        for m in ("GET", "PUT", "DELETE", "HEAD", "OPTIONS"):
            with self.assertRaises(R.Rejected) as c:
                R._validate(m, R.PATH, _good_body(), "m")
            self.assertEqual(c.exception.status, 405)

    def test_rejects_other_path(self):
        for p in ("/v1/models", "/", "/v1/chat", "/v1/chat/completions/x",
                  "/../etc/passwd", "/v1/embeddings"):
            with self.assertRaises(R.Rejected) as c:
                R._validate("POST", p, _good_body(), "m")
            self.assertEqual(c.exception.status, 404)

    def test_rejects_wrong_model(self):
        with self.assertRaises(R.Rejected) as c:
            R._validate("POST", R.PATH, _good_body("evil"), "m")
        self.assertEqual(c.exception.status, 403)

    def test_rejects_unknown_keys(self):
        body = _good_body("m", {"stream": True})
        with self.assertRaises(R.Rejected) as c:
            R._validate("POST", R.PATH, body, "m")
        self.assertEqual(c.exception.status, 400)

    def test_rejects_missing_messages(self):
        body = json.dumps({"model": "m"}).encode()
        with self.assertRaises(R.Rejected):
            R._validate("POST", R.PATH, body, "m")

    def test_rejects_non_object_and_bad_json(self):
        for body in (b"[]", b"\"x\"", b"123", b"not json", b"{"):
            with self.assertRaises(R.Rejected):
                R._validate("POST", R.PATH, body, "m")

    def test_rejects_wrong_types(self):
        for extra in ({"messages": "x"},):
            body = json.dumps({"model": "m", **extra}).encode()
            with self.assertRaises(R.Rejected):
                R._validate("POST", R.PATH, body, "m")
        body = json.dumps({"model": "m", "messages": [], "tools": "x"}).encode()
        with self.assertRaises(R.Rejected):
            R._validate("POST", R.PATH, body, "m")

    def test_reserializes_dropping_any_smuggled_whitespace(self):
        # a body with odd spacing comes out canonical; nothing but the
        # three allowed keys survives
        body = b'{"model":"m","messages":[],  "tools":[] }'
        out = R._validate("POST", R.PATH, body, "m")
        self.assertEqual(set(json.loads(out)), {"model", "messages", "tools"})


class TestReadRequest(unittest.TestCase):
    def _read(self, raw):
        a, b = _feed(raw)
        try:
            return R._read_request(a)
        finally:
            a.close(); b.close()

    def test_reads_simple(self):
        method, target, headers, body = self._read(_request())
        self.assertEqual((method, target), ("POST", R.PATH))
        self.assertEqual(json.loads(body)["model"], "m")

    def test_headers_too_large(self):
        big = b"X-Pad: " + b"a" * (R.MAX_HEADER + 10)
        with self.assertRaises(R.Rejected) as c:
            self._read(_request(headers={"X-Pad": "a" * (R.MAX_HEADER + 10)}))
        self.assertEqual(c.exception.status, 431)

    def test_chunked_refused(self):
        raw = (b"POST " + R.PATH.encode() + b" HTTP/1.1\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
        with self.assertRaises(R.Rejected) as c:
            self._read(raw)
        self.assertEqual(c.exception.reason, "transfer_encoding_refused")

    def test_content_length_required(self):
        raw = b"POST " + R.PATH.encode() + b" HTTP/1.1\r\nHost: x\r\n\r\n{}"
        with self.assertRaises(R.Rejected) as c:
            self._read(raw)
        self.assertEqual(c.exception.status, 411)

    def test_body_too_large(self):
        raw = (b"POST " + R.PATH.encode() + b" HTTP/1.1\r\n"
               b"Content-Length: " + str(R.MAX_BODY + 1).encode() + b"\r\n\r\n")
        with self.assertRaises(R.Rejected) as c:
            self._read(raw)
        self.assertEqual(c.exception.status, 413)

    def test_pipelined_second_request_refused(self):
        # trailing bytes after the declared body = a smuggled second request
        raw = _request() + b"POST /v1/chat/completions HTTP/1.1\r\n\r\n"
        with self.assertRaises(R.Rejected) as c:
            self._read(raw)
        self.assertEqual(c.exception.reason, "trailing_bytes")

    def test_duplicate_header_refused(self):
        raw = (b"POST " + R.PATH.encode() + b" HTTP/1.1\r\n"
               b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}")
        with self.assertRaises(R.Rejected) as c:
            self._read(raw)
        self.assertEqual(c.exception.reason, "duplicate_header")

    def test_bad_request_line(self):
        for line in (b"POST\r\n\r\n", b"POST /x HTTP/2\r\n\r\n",
                     b"GET /x\r\n\r\n"):
            with self.assertRaises(R.Rejected):
                self._read(line)


class _FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRelayHandle(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _relay(self, model="m", upstream="http://127.0.0.1:1/"):
        import tempfile, os
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return R.Relay(port=9, peer_cid=42, upstream=upstream, model=model,
                       log_path=path)

    def _run(self, relay, raw, cid=42, upstream_body=b'{"ok":1}', status=200):
        a, b = _feed(raw)

        def fake_urlopen(req, timeout=0):
            self.calls.append({"url": req.full_url, "data": req.data,
                               "headers": dict(req.header_items())})
            return _FakeResp(upstream_body, status)

        orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = fake_urlopen
        try:
            relay.handle(a, cid)
        finally:
            R.urllib.request.urlopen = orig
        resp = b.recv(65536)
        a.close(); b.close()
        return resp

    def test_forwards_only_configured_model_and_rebuilds_request(self):
        relay = self._relay(model="m", upstream="http://up:1234/")
        resp = self._run(relay, _request(body=_good_body("m")))
        self.assertIn(b"200", resp.split(b"\r\n", 1)[0])
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["url"], "http://up:1234" + R.PATH)
        # the relay built the upstream body itself: only the 2 allowed keys
        self.assertEqual(set(json.loads(call["data"])), {"model", "messages"})
        # no client-supplied header reached upstream
        self.assertNotIn("Host", call["headers"])

    def test_wrong_peer_cid_rejected_before_upstream(self):
        relay = self._relay()
        resp = self._run(relay, _request(), cid=999)
        self.assertIn(b"403", resp.split(b"\r\n", 1)[0])
        self.assertEqual(self.calls, [])

    def test_wrong_model_never_reaches_upstream(self):
        relay = self._relay(model="m")
        resp = self._run(relay, _request(body=_good_body("other")))
        self.assertIn(b"403", resp.split(b"\r\n", 1)[0])
        self.assertEqual(self.calls, [])

    def test_oversized_upstream_response_refused(self):
        relay = self._relay()
        resp = self._run(relay, _request(),
                         upstream_body=b"x" * (R.MAX_RESPONSE + 1))
        self.assertIn(b"502", resp.split(b"\r\n", 1)[0])


class TestServerArgs(unittest.TestCase):
    def _main(self, argv):
        try:
            return S.main(argv)
        except SystemExit as e:
            return e.code

    def test_requires_exactly_one_transport(self):
        # neither
        self.assertEqual(self._main(["--ledger", "/tmp/x"]), 2)
        # both
        self.assertEqual(self._main(
            ["--ledger", "/tmp/x", "--socket", "/tmp/s",
             "--vsock-port", "5", "--vsock-peer-cid", "42"]), 2)

    def test_vsock_needs_valid_peer_cid(self):
        self.assertEqual(self._main(
            ["--ledger", "/tmp/x", "--vsock-port", "5"]), 2)
        for bad in ("0", "1", "2"):   # 0..2 are reserved, not a guest
            self.assertEqual(self._main(
                ["--ledger", "/tmp/x", "--vsock-port", "5",
                 "--vsock-peer-cid", bad]), 2)


class TestClientVsockParse(unittest.TestCase):
    def test_vsock_path_connects_via_af_vsock(self):
        from blackwall.ipc.client import RemoteBroker, TransportError
        captured = {}
        orig = socket.socket

        def fake_socket(family, type_):
            captured["family"] = family
            raise ConnectionRefusedError("no listener")  # abort fast

        socket.socket = fake_socket
        orig_wait = RemoteBroker.CONNECT_WAIT
        RemoteBroker.CONNECT_WAIT = 0.0   # don't spin the backoff loop
        try:
            with self.assertRaises(TransportError):
                RemoteBroker("vsock:42:5000", timeout=0.1)
        finally:
            socket.socket = orig
            RemoteBroker.CONNECT_WAIT = orig_wait
        self.assertEqual(captured["family"], socket.AF_VSOCK)


if __name__ == "__main__":
    unittest.main()
