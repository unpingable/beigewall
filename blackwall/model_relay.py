"""Model-transport relay (phase 7): guest vsock -> host LLM endpoint.

The guest has two host channels. The authority channel is the BeigeWall
mediator (`blackwall.ipc.server --vsock-port`); it alone reaches trusted
state. This relay is the model-transport channel: it carries chat
completions to the local model server and nothing else. It holds no
authority, imports no trusted module, reads no file, and must expose no
host capability beyond "complete this chat with the configured model".

Accepted, exactly: one HTTP/1.1 request per connection, from the
designated guest CID, method POST, path /v1/chat/completions, a
Content-Length body (no chunked encoding, no body over MAX_BODY), JSON
object with exactly the keys {model, messages[, tools]}, model equal to
the configured model. The relay rebuilds the upstream request itself
(fixed upstream URL, fixed headers); nothing from the guest's request line
or headers reaches upstream. The upstream response body is returned with
only status and Content-Type/Content-Length. Every connection is logged
(JSONL, outside the guest) with its decision.
"""

import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

MAX_HEADER = 16 * 1024
MAX_BODY = 4 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
PATH = "/v1/chat/completions"
ALLOWED_KEYS = {"model", "messages", "tools"}
READ_TIMEOUT = 30.0


class Rejected(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _read_request(conn: socket.socket) -> tuple[str, str, dict, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            raise Rejected(400, "incomplete_headers")
        buf += chunk
        if len(buf) > MAX_HEADER:
            raise Rejected(431, "headers_too_large")
    head, _, rest = buf.partition(b"\r\n\r\n")
    try:
        lines = head.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        raise Rejected(400, "non_ascii_headers")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        raise Rejected(400, "bad_request_line")
    method, target, _ = parts
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise Rejected(400, "bad_header")
        key = name.lower()
        if key in headers:
            raise Rejected(400, "duplicate_header")
        headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise Rejected(400, "transfer_encoding_refused")
    try:
        length = int(headers.get("content-length", ""))
    except ValueError:
        raise Rejected(411, "content_length_required")
    if length < 0 or length > MAX_BODY:
        raise Rejected(413, "body_too_large")
    body = rest
    while len(body) < length:
        chunk = conn.recv(min(65536, length - len(body)))
        if not chunk:
            raise Rejected(400, "incomplete_body")
        body += chunk
    if len(body) != length:
        raise Rejected(400, "trailing_bytes")     # no pipelined second request
    return method, target, headers, body


def _validate(method: str, target: str, body: bytes, model: str) -> bytes:
    if method != "POST":
        raise Rejected(405, "method_refused")
    if target != PATH:
        raise Rejected(404, "path_refused")
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected(400, "body_not_json")
    if not isinstance(obj, dict) or not set(obj) <= ALLOWED_KEYS or "messages" not in obj:
        raise Rejected(400, "body_keys_refused")
    if obj.get("model") != model:
        raise Rejected(403, "model_refused")
    if not isinstance(obj["messages"], list):
        raise Rejected(400, "messages_not_list")
    if "tools" in obj and not isinstance(obj["tools"], list):
        raise Rejected(400, "tools_not_list")
    # re-serialized: the upstream sees the relay's bytes, not the guest's
    return json.dumps(obj).encode("utf-8")


def _respond(conn: socket.socket, status: int, body: bytes,
             ctype: str = "application/json") -> None:
    head = (f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    try:
        conn.sendall(head.encode("ascii") + body)
    except OSError:
        pass


class Relay:
    def __init__(self, port: int, peer_cid: int, upstream: str, model: str, log_path: str):
        self.port, self.peer_cid = port, peer_cid
        self.upstream = upstream.rstrip("/") + PATH
        self.model = model
        self._log_path = log_path
        self._log_lock = threading.Lock()
        self._stop = False

    def log(self, **rec) -> None:
        rec["t"] = time.time()
        with self._log_lock, open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def handle(self, conn: socket.socket, cid: int) -> None:
        conn.settimeout(READ_TIMEOUT)
        start = time.monotonic()
        try:
            if cid != self.peer_cid:
                raise Rejected(403, "peer_cid")
            method, target, _headers, body = _read_request(conn)
            payload = _validate(method, target, body, self.model)
            req = urllib.request.Request(self.upstream, data=payload,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    out = resp.read(MAX_RESPONSE + 1)
                    status = resp.status
            except urllib.error.HTTPError as exc:
                out, status = exc.read(MAX_RESPONSE + 1), exc.code
            except (urllib.error.URLError, TimeoutError) as exc:
                raise Rejected(502, f"upstream_unreachable:{type(exc).__name__}")
            if len(out) > MAX_RESPONSE:
                raise Rejected(502, "upstream_response_too_large")
            _respond(conn, status, out)
            self.log(peer_cid=cid, decision="forwarded", status=status,
                     bytes_in=len(body), bytes_out=len(out),
                     ms=round((time.monotonic() - start) * 1000, 1))
        except Rejected as exc:
            _respond(conn, exc.status, json.dumps({"error": exc.reason}).encode())
            self.log(peer_cid=cid, decision="rejected", reason=exc.reason, status=exc.status)
        except (OSError, socket.timeout) as exc:
            self.log(peer_cid=cid, decision="transport_error", reason=type(exc).__name__)
        except Exception as exc:    # never let a hostile request kill the thread
            _respond(conn, 500, json.dumps({"error": "relay_internal"}).encode())
            self.log(peer_cid=cid, decision="internal_error", reason=type(exc).__name__)
        finally:
            conn.close()

    def serve(self) -> None:
        import signal
        srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        srv.bind((socket.VMADDR_CID_ANY, self.port))
        srv.listen(8)
        srv.settimeout(0.5)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        self.log(decision="listening", port=self.port, peer_cid=self.peer_cid,
                 model=self.model)
        try:
            while not self._stop:
                try:
                    conn, (cid, _p) = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.handle, args=(conn, cid), daemon=True).start()
        finally:
            srv.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vsock-port", type=int, required=True)
    p.add_argument("--peer-cid", type=int, required=True)
    p.add_argument("--upstream", required=True, help="e.g. http://127.0.0.1:11434")
    p.add_argument("--model", required=True)
    p.add_argument("--log", required=True)
    a = p.parse_args(argv)
    if a.peer_cid < 3:
        p.error("--peer-cid must be a guest CID (>= 3)")
    Relay(a.vsock_port, a.peer_cid, a.upstream, a.model, a.log).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
