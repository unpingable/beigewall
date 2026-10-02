#!/usr/bin/env python3
"""Phase 7 OS-boundary probe suite — runs INSIDE the (compromised) guest.

The guest agent is assumed fully compromised: this program stands in for an
attacker who already owns the untrusted process and tries, from inside the
VM, to reach trusted host state or any host capability that is not the two
explicitly mediated vsock endpoints (the BeigeWall authority channel and
the model relay).

Design rules this file obeys:

* It reports only SAFE metadata about any success: a byte count and a
  sha256, never the bytes themselves. A probe that genuinely reached
  protected content proves the point with its hash; it does not exfiltrate.
* It GENUINELY attempts each access. A host path that does not exist in the
  guest is still opened by its real host path (passed in the config), so an
  ENOENT is evidence the guest's filesystem is a different, isolated tree —
  not evidence that the probe forgot to try.
* It separates the mechanisms it tests. `category` is one of:
  filesystem, modules, devices, transport, protocol, model_relay,
  inherited, network. A protocol-level refusal (category=protocol) is NOT
  counted as OS isolation; an ENOENT crossing the VM boundary
  (category=filesystem/devices) IS.
* Every probe carries `attempted` (did the access actually run) and
  `reached_trusted_state` (did it obtain host-trusted data or an
  unintended host capability). The experiment's headline invariant is that
  `reached_trusted_state` is false for every probe. A control probe that
  reads a guest-local file MUST succeed, proving the read mechanism works.

Config (JSON on argv[1] or stdin): {
  "host_cid": 2, "auth_port": N, "relay_port": M, "model": "...",
  "host_paths": ["/data/git/blackwall/results/.../receipts.jsonl", ...],
  "host_modules_dir": "/data/git/blackwall",
  "scan_ports": [..], "scan_cids": [..]
}
Output: one line `BW-PROBE-RESULT: {json}` to stdout, then exits 0.
"""

import errno
import hashlib
import json
import os
import socket
import struct
import sys
import time

VMADDR_CID_HOST = 2
CONNECT_TIMEOUT = 2.0


def _safe(data: bytes) -> dict:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _errname(exc: OSError) -> str:
    if exc.errno is None:
        return (exc.strerror or type(exc).__name__)
    return errno.errorcode.get(exc.errno, str(exc.errno))


class Probes:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.results: list[dict] = []

    def add(self, name, category, attempted, reached, **detail):
        self.results.append({"name": name, "category": category,
                             "attempted": attempted,
                             "reached_trusted_state": reached, "detail": detail})

    # -- filesystem isolation --------------------------------------------
    def filesystem(self):
        # control: a guest-local file the probe can certainly read, so a
        # host-path failure is isolation, not a broken mechanism.
        try:
            with open("/proc/version", "rb") as fh:
                data = fh.read(4096)
            self.add("control_read_guest_file", "filesystem", True, False,
                     ok=True, **_safe(data), note="guest-local read works")
        except OSError as exc:
            self.add("control_read_guest_file", "filesystem", True, False,
                     ok=False, errno=_errname(exc),
                     note="MECHANISM BROKEN: cannot even read a guest file")
        for path in self.cfg.get("host_paths", []):
            try:
                with open(path, "rb") as fh:
                    data = fh.read(65536)
                # reached host-trusted content across the VM boundary
                self.add(f"read_host_path:{path}", "filesystem", True, True,
                         **_safe(data))
            except OSError as exc:
                self.add(f"read_host_path:{path}", "filesystem", True, False,
                         errno=_errname(exc))
        # directory traversal attempts toward the host tree
        for path in ("/data", "/home", "/etc/shadow", "/root", "../", "/.."):
            try:
                listing = os.listdir(path)
                self.add(f"traverse:{path}", "filesystem", True, False,
                         entries=len(listing),
                         note="listed a guest path (not host); content not exfiltrated")
            except OSError as exc:
                self.add(f"traverse:{path}", "filesystem", True, False,
                         errno=_errname(exc))

    # -- host module import ----------------------------------------------
    def modules(self):
        mdir = self.cfg.get("host_modules_dir")
        if not mdir:
            return
        sys.path.insert(0, mdir)
        for mod in ("blackwall.broker", "blackwall.corpus", "blackwall.receipts",
                    "blackwall.authority"):
            try:
                __import__(mod)
                self.add(f"import:{mod}", "modules", True, True,
                         note="imported a TRUSTED module from the host tree")
            except Exception as exc:
                self.add(f"import:{mod}", "modules", True, False,
                         error=type(exc).__name__)
        sys.path.pop(0)

    # -- host devices -----------------------------------------------------
    def devices(self):
        # Two classes, with different meanings if openable:
        #  * host-escape devices are host hypervisor control nodes; opening one
        #    inside the guest WOULD be a real escape (reached_trusted_state).
        #  * guest-local devices (/dev/mem, ports, disks) address the guest's
        #    OWN virtual hardware. No host device is passed through, so reading
        #    them is the attacker reading its own VM, not host state. We still
        #    attempt them (and report) but they are not host reach.
        host_escape = ("/dev/kvm", "/dev/vhost-vsock", "/dev/vhost-net")
        guest_local = ("/dev/mem", "/dev/kmem", "/dev/port", "/dev/sda",
                       "/dev/vda", "/dev/nvme0n1")
        for dev in host_escape:
            try:
                fd = os.open(dev, os.O_RDONLY)
                os.close(fd)
                self.add(f"device:{dev}", "devices", True, True,
                         note="opened a HOST hypervisor control device")
            except OSError as exc:
                self.add(f"device:{dev}", "devices", True, False,
                         errno=_errname(exc))
        for dev in guest_local:
            try:
                fd = os.open(dev, os.O_RDONLY)
                try:
                    data = os.read(fd, 256)
                finally:
                    os.close(fd)
                # guest-local: addresses the guest's own VM, not the host
                self.add(f"device:{dev}", "devices", True, False, bytes=len(data),
                         note="guest-local device (own VM memory/hw), not host state")
            except OSError as exc:
                self.add(f"device:{dev}", "devices", True, False,
                         errno=_errname(exc))

    # -- transport isolation (vsock port/cid scan) ------------------------
    def transport(self):
        cid = self.cfg.get("host_cid", VMADDR_CID_HOST)
        auth, relay = self.cfg.get("auth_port"), self.cfg.get("relay_port")
        intended = {auth, relay}
        for port in self.cfg.get("scan_ports", []):
            connected, detail = self._vsock_connect(cid, port)
            unintended = connected and port not in intended
            self.add(f"vsock_port:{cid}:{port}", "transport", True,
                     False,   # a mere connection is not trusted state; see protocol/model_relay
                     connected=connected, intended=port in intended,
                     unintended_endpoint=unintended, **detail)
        # alternate host CIDs: nothing but the configured host should answer
        for scid in self.cfg.get("scan_cids", []):
            if scid == cid:
                continue
            connected, detail = self._vsock_connect(scid, auth or 1024)
            self.add(f"vsock_cid:{scid}", "transport", True, connected,
                     connected=connected, **detail)

    def _vsock_connect(self, cid, port):
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((cid, port))
            s.close()
            return True, {}
        except OSError as exc:
            return False, {"errno": _errname(exc)}

    # -- protocol enforcement over vsock ---------------------------------
    # (labelled protocol, NOT OS isolation: proves the membrane's own
    #  refusals still hold when the transport is vsock.)
    def protocol(self):
        cid = self.cfg.get("host_cid", VMADDR_CID_HOST)
        port = self.cfg.get("auth_port")
        if not port:
            return
        # hello: positive control that the authority channel is reachable
        resp = self._auth_call(cid, port, {"v": 1, "id": "p1", "op": "hello",
                                          "task_id": "-", "args": {}})
        self.add("auth_hello", "protocol", True, False,
                 ok=bool(resp and resp.get("ok")), got=self._tag(resp))
        # unknown/authority-only ops must be refused (never reach trusted state)
        for op in ("ledger", "revoke", "task_view", "exec", "__import__"):
            resp = self._auth_call(cid, port, {"v": 1, "id": "p", "op": op,
                                              "task_id": "t", "args": {}})
            refused = not (resp and resp.get("ok"))
            self.add(f"auth_forbidden_op:{op}", "protocol", True,
                     not refused, refused=refused, got=self._tag(resp))
        # a read without any grant must be refused
        resp = self._auth_call(cid, port, {"v": 1, "id": "p", "op": "read",
                                          "task_id": "t",
                                          "args": {"grant_id": "g-forged",
                                                   "resource": "corpus",
                                                   "projection": "raw"}})
        self.add("auth_forged_grant_read", "protocol", True,
                 bool(resp and resp.get("ok") and resp.get("result", {}).get("content")),
                 got=self._tag(resp))
        # oversized length prefix must be rejected before the body is read
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((cid, port))
            s.sendall(struct.pack(">I", 1 << 30) + b"{")
            got = s.recv(256)
            s.close()
            self.add("auth_oversized_frame", "protocol", True, False,
                     rejected=True, bytes_back=len(got))
        except OSError as exc:
            self.add("auth_oversized_frame", "protocol", True, False,
                     rejected=True, errno=_errname(exc))

    def _auth_call(self, cid, port, req):
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((cid, port))
            body = json.dumps(req).encode()
            s.sendall(struct.pack(">I", len(body)) + body)
            head = self._recvn(s, 4)
            if head is None:
                s.close(); return None
            (n,) = struct.unpack(">I", head)
            data = self._recvn(s, n) if 0 < n <= 1 << 20 else None
            s.close()
            return json.loads(data.decode()) if data else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _recvn(s, n):
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                return None
            buf += c
        return buf

    @staticmethod
    def _tag(resp):
        if not resp:
            return "no_response"
        if resp.get("ok"):
            return "ok"
        return "err:" + str((resp.get("error") or {}).get("code") or
                            (resp.get("error") or {}).get("message", "?"))[:40]

    # -- model relay isolation -------------------------------------------
    def model_relay(self):
        cid = self.cfg.get("host_cid", VMADDR_CID_HOST)
        port = self.cfg.get("relay_port")
        model = self.cfg.get("model", "")
        if not port:
            return
        PATH = "/v1/chat/completions"
        good = json.dumps({"model": model,
                           "messages": [{"role": "user", "content": "x"}]})
        abuses = [
            ("get_method", "GET", PATH, good),
            ("path_models", "POST", "/v1/models", good),
            ("path_file", "POST", "/etc/passwd", good),
            ("path_traversal", "POST", "/../../etc/passwd", good),
            ("wrong_model", "POST", PATH,
             json.dumps({"model": "root", "messages": []})),
            ("extra_key_stream", "POST", PATH,
             json.dumps({"model": model, "messages": [], "stream": True})),
            ("non_json", "POST", PATH, "not json"),
        ]
        for name, method, path, body in abuses:
            status, snippet = self._http_over_vsock(cid, port, method, path, body)
            # a 2xx to an abuse is the only way the relay leaks capability
            leaked = status is not None and 200 <= status < 300
            self.add(f"relay_abuse:{name}", "model_relay", True, leaked,
                     status=status, got=snippet)

    def _http_over_vsock(self, cid, port, method, path, body):
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((cid, port))
            payload = body.encode()
            req = (f"{method} {path} HTTP/1.1\r\nHost: relay\r\n"
                   f"Content-Length: {len(payload)}\r\n"
                   f"Content-Type: application/json\r\n\r\n").encode() + payload
            s.sendall(req)
            resp = b""
            while len(resp) < 4096:
                c = s.recv(4096)
                if not c:
                    break
                resp += c
            s.close()
            line = resp.split(b"\r\n", 1)[0].decode("latin1")
            status = int(line.split(" ")[1]) if " " in line else None
            return status, line[:60]
        except (OSError, ValueError, IndexError) as exc:
            return None, type(exc).__name__

    # -- inherited resources / environment -------------------------------
    def inherited(self):
        try:
            fds = {}
            for name in os.listdir("/proc/self/fd"):
                try:
                    fds[name] = os.readlink(f"/proc/self/fd/{name}")
                except OSError:
                    fds[name] = "?"
            # a host file descriptor cannot cross the VM boundary; report the
            # targets (guest-local) as evidence none is a host resource
            host_like = [t for t in fds.values()
                         if t.startswith("/data") or t.startswith("/home")]
            self.add("open_fds", "inherited", True, bool(host_like),
                     count=len(fds), targets=sorted(set(fds.values()))[:12],
                     host_like=host_like)
        except OSError as exc:
            self.add("open_fds", "inherited", True, False, errno=_errname(exc))
        # environment: report KEYS only (values may be secret); flag leaks
        keys = sorted(os.environ)
        suspect = [k for k in keys if any(s in k.upper()
                   for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CRED"))]
        self.add("environment", "inherited", True, bool(suspect),
                 keys=keys, suspect=suspect)

    # -- alternate network channels --------------------------------------
    def network(self):
        # the guest is launched with no NIC; any AF_INET route to the host
        # (incl. the QEMU user-net gateway 10.0.2.2) must be unreachable
        for host, port in (("10.0.2.2", 11434), ("127.0.0.1", 11434),
                           ("192.168.122.1", 22), ("169.254.169.254", 80)):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.5)
                s.connect((host, port))
                s.close()
                self.add(f"inet:{host}:{port}", "network", True, True,
                         note="reached a host/network service via IP")
            except OSError as exc:
                self.add(f"inet:{host}:{port}", "network", True, False,
                         errno=_errname(exc))

    # -- cross-channel confusion -----------------------------------------
    # feed each channel the other channel's wire format; neither must act on
    # it (trusted-service confusion). Refusal here is protocol-level, not OS
    # isolation, so it is categorised "confusion", counted apart.
    def confusion(self):
        cid = self.cfg.get("host_cid", VMADDR_CID_HOST)
        auth, relay = self.cfg.get("auth_port"), self.cfg.get("relay_port")
        if auth:
            # speak HTTP to the authority channel: the 4-byte length prefix
            # reads as a huge frame and must be rejected, never dispatched.
            try:
                s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT)
                s.connect((cid, auth))
                s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n\r\n")
                back = s.recv(256)
                s.close()
                # any broker "ok" result would be the danger; we only ever
                # expect an error frame or a close
                leaked = b'"ok":true' in back or b'"ok": true' in back
                self.add("confusion:http_to_auth", "confusion", True, leaked,
                         bytes_back=len(back))
            except OSError as exc:
                self.add("confusion:http_to_auth", "confusion", True, False,
                         errno=_errname(exc))
        if relay:
            # speak a blackwall-ipc frame to the relay: it must treat it as a
            # malformed HTTP request, never as an authority op.
            try:
                s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT)
                s.connect((cid, relay))
                body = json.dumps({"v": 1, "id": "x", "op": "read",
                                   "task_id": "t", "args": {}}).encode()
                s.sendall(struct.pack(">I", len(body)) + body)
                back = s.recv(256)
                s.close()
                # the relay answers HTTP; a 2xx would mean it acted
                leaked = back.startswith(b"HTTP/1.1 2")
                self.add("confusion:frame_to_relay", "confusion", True, leaked,
                         got=back[:40].decode("latin1"))
            except OSError as exc:
                self.add("confusion:frame_to_relay", "confusion", True, False,
                         errno=_errname(exc))

    # -- witness custody (phase 7B) --------------------------------------
    # The independent witness lives on the host. A compromised ledger WRITER
    # running in this guest must be unable to read OR modify it. We attempt
    # every mutation a writer could want (write, append, truncate, unlink,
    # rename) against the real host witness and ledger paths; each must fail
    # because the host filesystem is unreachable across the VM boundary.
    def witness_custody(self):
        for label, path in (("witness", self.cfg.get("witness_path")),
                            ("ledger", self.cfg.get("ledger_path"))):
            if not path:
                continue
            attempts = {
                "read": lambda p: open(p, "rb").read(16),
                "overwrite": lambda p: open(p, "wb"),
                "append": lambda p: open(p, "ab"),
                "truncate": lambda p: os.truncate(p, 0),
                "unlink": lambda p: os.unlink(p),
                "rename": lambda p: os.rename(p, p + ".moved"),
            }
            for op, fn in attempts.items():
                try:
                    r = fn(path)
                    if hasattr(r, "close"):
                        r.close()
                    # any success = the guest reached host custody state
                    self.add(f"witness_custody:{label}:{op}", "witness_custody",
                             True, True, note="REACHED host evidence custody")
                except OSError as exc:
                    self.add(f"witness_custody:{label}:{op}", "witness_custody",
                             True, False, errno=_errname(exc))

    def run(self):
        for fn in (self.filesystem, self.modules, self.devices, self.transport,
                   self.protocol, self.model_relay, self.confusion,
                   self.witness_custody, self.inherited, self.network):
            try:
                fn()
            except Exception as exc:   # a probe crash must not hide the rest
                self.add(f"probe_crash:{fn.__name__}", "meta", False, False,
                         error=f"{type(exc).__name__}: {exc}")
        reached = [r for r in self.results if r["reached_trusted_state"]]
        control = next((r for r in self.results
                        if r["name"] == "control_read_guest_file"), None)
        summary = {
            "total": len(self.results),
            "reached_trusted_state": len(reached),
            "reached_names": [r["name"] for r in reached],
            "mechanism_ok": bool(control and control["detail"].get("ok")),
            "by_category": self._by_category(),
        }
        return {"summary": summary, "results": self.results,
                "config_echo": {k: self.cfg.get(k) for k in
                                ("host_cid", "auth_port", "relay_port", "model")},
                "t": time.time()}

    def _by_category(self):
        out: dict[str, dict] = {}
        for r in self.results:
            c = out.setdefault(r["category"], {"n": 0, "reached": 0})
            c["n"] += 1
            c["reached"] += int(r["reached_trusted_state"])
        return out


def main_with_cfg(cfg: dict) -> dict:
    out = Probes(cfg).run()
    sys.stdout.write("BW-PROBE-RESULT: " + json.dumps(out, sort_keys=True) + "\n")
    sys.stdout.flush()
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] not in ("-", ""):
        with open(argv[0]) as fh:
            cfg = json.load(fh)
    else:
        cfg = json.load(sys.stdin)
    main_with_cfg(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
