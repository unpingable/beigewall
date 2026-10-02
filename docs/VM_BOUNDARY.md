# Phase 7 — VM / OS boundary

Research question: can the BeigeWall protocol/process boundary become an
actual **OS security boundary** when the untrusted agent runs inside a VM?
Not "is a VM magically secure", but concretely:

> Can a fully compromised guest agent reach trusted host state or alternate
> host capabilities **outside the explicitly mediated BeigeWall interface**?

Phase 6 established that every *logical* guarantee survives a real process
boundary, but at **protocol strength only**: both processes ran as one OS
user, so nothing stopped the agent process from opening the ledger or socket
if it discovered the path (demonstrated, `docs/PROCESS_BOUNDARY.md`). Phase 7
replaces the same-user process boundary with a VM boundary and asks what the
OS/hypervisor now enforces that the convention did not.

## Architecture

```
HOST (trusted)                                  GUEST (untrusted, compromised)
  python3 -m beigewall.ipc.server               QEMU/KVM, initramfs-only
    --vsock-port A --vsock-peer-cid C             no disk, no NIC, no shared FS
    corpus, PolicyAuthority, Ledger             /init (busybox) -> guest_main.py
    CapabilityBroker, projections, effects        run_agent_process (UNCHANGED)
    capability catalog, receipt ledger            RemoteBroker("vsock:2:A")
        ▲                                             │ authority channel
        └───────────── vsock (CID C only) ───────────┘
  python3 -m beigewall.model_relay              model channel (transport only):
    --vsock-port R --peer-cid C                   driver -> 127.0.0.1:8080
    POST /v1/chat/completions only                 -> guest_model_forwarder
    one configured model, rebuilt upstream         -> vsock:2:R -> relay -> Ollama
        ▲                                             │
        └───────────── vsock (CID C only) ───────────┘
  Ollama (host LLM, 127.0.0.1:11434)
```

The guest is launched with **only** a `vhost-vsock-pci` device (a fixed
guest CID) and a serial console. No `-drive`, no `-nic`/user-net, no
virtiofs/9p, no device passthrough, no `-fw_cfg` host files. Guest config is
a base64 JSON blob on the kernel command line (`bw.cfg=`). The serial
console is a one-way, guest-controlled **advisory** output (like phase 6
self-reported telemetry); it is not an authority channel. The trusted
evidence is the **host** ledger, written by the host server, exactly as in
phase 6.

### The two channels are separate and minimal

* **Authority channel** — the unchanged `beigewall-ipc/1` protocol, now over
  `AF_VSOCK` instead of `AF_UNIX`. `TrustedServer.serve_vsock` binds a host
  vsock port, and **accepts only the designated guest CID** (any other peer
  CID is closed before a byte is read and receipted as `ipc_reject
  peer_cid`). Dispatch, broker, projections, effects and ledger are byte-for-
  byte the phase-6C code; only the listener differs. `RemoteBroker` accepts a
  `vsock:CID:PORT` path and is otherwise unchanged.

* **Model channel** — `beigewall/model_relay.py`, a host process that exists
  only because the guest has no network and Ollama runs on the host. It is
  **not** an authority channel. It accepts, per connection, exactly one
  HTTP/1.1 `POST /v1/chat/completions` from the designated guest CID, with a
  `Content-Length` body (no chunked, no pipelining, no oversize), a JSON
  object with exactly `{model, messages[, tools]}` and `model` equal to the
  one configured model. It **rebuilds** the upstream request itself (fixed
  URL and headers; nothing from the guest's request line or headers reaches
  upstream) and returns only the upstream status and body. It holds no
  authority, imports no trusted module, reads no file, mints no grant,
  performs no effect, and offers no arbitrary host RPC. The guest reaches it
  through a blind loopback→vsock forwarder (`vm/guest_model_forwarder.py`) so
  the **normative LLM driver is unmodified**; loopback is intra-guest only
  (no NIC exists).

The channels are never combined, and none of the forbidden conveniences are
provided: no shared host directories, virtiofs, 9p, clipboard, credential
mounts, device passthrough, guest networking or arbitrary host RPC.

## The guest

The smallest guest that can boot, run the untrusted harness, speak vsock and
run the deterministic path: an **initramfs-only** VM (no root filesystem on
disk). It contains busybox, the host CPython interpreter + stdlib, the
**untrusted BeigeWall subset only** (ipc client/agent/protocol, agent
harness + drivers, `tasks_public`, `run_agent_process`) and the phase-7
guest scripts. It imports no trusted module — the phase-6 import-graph test
(`tests/test_ipc_boundary.py::test_19`) is the primary guard and
`vm/build_guest.py` re-checks the staged tree. The vsock stack ships as three
decompressed `.ko` files loaded by `/init`. Built with `vm/build_guest.py`
(needs `fakeroot`, `cpio`, `gzip`, `zstd`; no root). Kernel: a stock Ubuntu
`linux-image-unsigned-6.8.0-146-generic` (vsock = modules; virtio-pci,
console, devtmpfs-automount built in), unrelated to the host's running
kernel.

## Mechanisms, kept distinct

Phase 7 reports five enforcement layers **separately**; a refusal in one is
not evidence for another:

1. **Protocol enforcement** — the membrane's own refusals (forged grant,
   authority-only op, oversized frame) still hold over vsock.
2. **VM/OS enforcement** — the guest is a different kernel; host memory and
   host kernel objects are unreachable by construction.
3. **Filesystem isolation** — the guest has a different root; a host path,
   even when the guest knows it exactly, resolves to nothing (ENOENT).
4. **Transport isolation** — only the two configured vsock endpoints on the
   host CID answer; every other port/CID is refused.
5. **Model-relay isolation** — the relay is a single-verb transport; no abuse
   turns it into a host-state oracle.

A protocol refusal is **not** evidence of OS isolation. A failed host-file
read is evidence of OS isolation only because the probe *genuinely attempted*
the real host path and a control read of a guest-local file succeeded in the
same run.

## Running it

```
# build the guest (once)
python3 vm/build_guest.py --kernel-modules <dir with vmw_vsock_*.ko.zst> \
    --out <build-dir>            # writes <build-dir>/initramfs.gz

# OS-boundary probe suite (own ledger)
python3 run_vm_experiment.py --probe \
    --kernel <vmlinuz> --initrd <build-dir>/initramfs.gz --out <run-dir>

# deterministic suite through the VM (own, clean ledger)
python3 run_vm_experiment.py --driver scripted --condition control \
    --kernel <vmlinuz> --initrd <build-dir>/initramfs.gz --out <run-dir>

# real model through the relay
python3 run_vm_experiment.py --driver llm --model gpt-oss:20b \
    --upstream http://127.0.0.1:11434 \
    --kernel <vmlinuz> --initrd <build-dir>/initramfs.gz --out <run-dir>
```

The probe and the deterministic agent are run as **separate** invocations:
the probe's hostile frames would otherwise land in the agent's ledger and
break the normative comparison.

## What this does and does not establish

See `FINDINGS_PHASE7.md`. In short: the VM boundary makes the phase-6
same-user gaps (storage and socket secrecy) **OS-enforced** for a guest that
is a separate kernel, and the two mediated vsock endpoints are the only host
surface the guest can reach. It does **not** claim the VM is unescapable: it
does not evaluate QEMU/KVM CVEs, side channels, or a hostile *host*, and the
model relay's isolation is argued from its one-verb construction, not from a
second OS boundary around Ollama.

## Stopping boundary

Phase 7 stops at the smallest meaningful VM boundary. It does **not** proceed
to stronger VM hardening, containers, namespaces, seccomp, real desktop
databases, or production deployment. A clean failure would have been a
successful experiment; the boundary held, and that result is recorded without
extension.
