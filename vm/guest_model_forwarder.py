#!/usr/bin/env python3
"""Guest-side model forwarder (phase 7).

The untrusted LLM driver speaks ordinary HTTP with urllib, which cannot dial
vsock. This forwarder is the smallest possible shim that lets the UNMODIFIED
driver reach the host model relay: it listens on guest loopback
(127.0.0.1:PORT) and blind-pipes each accepted TCP connection to the host
model relay over vsock (host CID 2, relay port). It parses nothing, holds no
authority, and performs no validation — all request discipline lives in the
host relay. Loopback is intra-guest only; the guest is launched with no NIC,
so this adds no network path to the host.

  python3 guest_model_forwarder.py --listen-port 8080 \
      --host-cid 2 --relay-port 9100
"""

import argparse
import socket
import sys
import threading


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(tcp, host_cid, relay_port):
    try:
        vs = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        vs.connect((host_cid, relay_port))
    except OSError:
        tcp.close()
        return
    a = threading.Thread(target=_pipe, args=(tcp, vs), daemon=True)
    b = threading.Thread(target=_pipe, args=(vs, tcp), daemon=True)
    a.start(); b.start()
    a.join(); b.join()
    for s in (tcp, vs):
        try:
            s.close()
        except OSError:
            pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--listen-port", type=int, default=8080)
    p.add_argument("--host-cid", type=int, default=2)
    p.add_argument("--relay-port", type=int, required=True)
    a = p.parse_args(argv)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.listen_port))
    srv.listen(16)
    sys.stderr.write(f"[forwarder] 127.0.0.1:{a.listen_port} -> "
                     f"vsock:{a.host_cid}:{a.relay_port}\n")
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, a.host_cid, a.relay_port),
                         daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
