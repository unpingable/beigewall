#!/usr/bin/env python3
"""Build the smallest viable BeigeWall phase-7 guest initramfs.

The guest is initramfs-only: no disk image, no root filesystem, no network
device. It contains busybox, the host CPython interpreter + stdlib, the
UNTRUSTED BeigeWall subset (ipc client/agent/protocol, agent harness +
drivers, tasks_public, run_agent_process) and the phase-7 guest scripts
(guest_main, model forwarder, OS-boundary probe). It imports no trusted
module (enforced by tests/test_ipc_boundary.py's import-graph test and
re-checked here). The vsock stack ships as decompressed .ko files loaded by
/init.

Usage:
  python3 vm/build_guest.py --kernel-modules <dir with vmw_vsock_*.ko.zst> \
      --out <dir>   # writes <dir>/initramfs.gz

Needs: fakeroot, cpio, gzip, zstd (for the .ko.zst modules). No root.
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# untrusted BeigeWall subset (must match the phase-6 import-graph contract)
BW_FILES = [
    "beigewall/__init__.py",
    "beigewall/ipc/__init__.py",
    "beigewall/ipc/client.py",
    "beigewall/ipc/agent.py",
    "beigewall/ipc/protocol.py",
    "beigewall/ipc/scripted_tasks.json",
    "beigewall/agent/__init__.py",
    "beigewall/agent/drivers.py",
    "beigewall/agent/harness.py",
    "beigewall/tasks_public.py",
    "run_agent_process.py",
    "vm/guest_main.py",
    "vm/guest_model_forwarder.py",
    "vm/probe_os_boundary.py",
]
TRUSTED_FORBIDDEN = {"broker", "corpus", "authority", "receipts",
                     "projections", "effects", "tasks", "facts", "claims",
                     "evaluator", "contracts", "disclosure"}
VSOCK_MODULES = ["vsock", "vmw_vsock_virtio_transport_common",
                 "vmw_vsock_virtio_transport"]
# stdlib dirs not needed in the guest (shrink the image)
STDLIB_SKIP = {"test", "idlelib", "tkinter", "turtledemo", "lib2to3",
               "ensurepip", "pydoc_data", "__pycache__", "config-3.12-x86_64-linux-gnu"}


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def resolved_libs(binaries):
    libs = set()
    for b in binaries:
        try:
            out = subprocess.run(["ldd", b], capture_output=True, text=True).stdout
        except OSError:
            continue
        for line in out.splitlines():
            line = line.strip()
            if "=>" in line:
                path = line.split("=>", 1)[1].strip().split(" ")[0]
            elif line.startswith("/"):
                path = line.split(" ")[0]
            else:
                continue
            # keep the path the loader will search (e.g. /lib/x86_64-linux-gnu/
            # libz.so.1), not its realpath under /usr/lib; copy follows the
            # symlink to get real content.
            if path and os.path.exists(path):
                libs.add(path)
    return libs


def copy_into(stage, src, dst_rel):
    dst = os.path.join(stage, dst_rel.lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=True)
    return dst


def build_stage(stage, modules_dir):
    for d in ("bin", "sbin", "proc", "sys", "dev", "tmp", "root",
              "usr/bin", "usr/lib", "modules", "opt/bw"):
        os.makedirs(os.path.join(stage, d), exist_ok=True)

    # busybox
    copy_into(stage, "/usr/bin/busybox", "bin/busybox")

    # python interpreter at the host's own path, stdlib alongside it
    py = os.path.realpath("/usr/bin/python3.12")
    copy_into(stage, py, "usr/bin/python3.12")
    os.symlink("python3.12", os.path.join(stage, "usr/bin/python3"))
    os.symlink("/usr/bin/python3.12", os.path.join(stage, "bin/python3"))

    stdlib_src = "/usr/lib/python3.12"
    stdlib_dst = os.path.join(stage, "usr/lib/python3.12")
    shutil.copytree(stdlib_src, stdlib_dst,
                    ignore=shutil.ignore_patterns(*STDLIB_SKIP), symlinks=True)

    # shared libraries for the interpreter and every C extension
    dynload = os.path.join(stdlib_dst, "lib-dynload")
    exts = [os.path.join(dynload, f) for f in os.listdir(dynload)] \
        if os.path.isdir(dynload) else []
    for lib in resolved_libs([py, "/usr/bin/busybox", *exts]):
        rel = lib if not lib.startswith("/") else lib[1:]
        copy_into(stage, lib, rel)
    # the dynamic loader
    for ld in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-x86-64.so.2"):
        if os.path.exists(ld):
            copy_into(stage, os.path.realpath(ld), ld.lstrip("/"))

    # untrusted BeigeWall subset
    for rel in BW_FILES:
        copy_into(stage, os.path.join(REPO, rel), os.path.join("opt/bw", rel))

    # vsock modules, decompressed
    for m in VSOCK_MODULES:
        zst = os.path.join(modules_dir, f"{m}.ko.zst")
        ko = os.path.join(stage, "modules", f"{m}.ko")
        if os.path.exists(zst):
            with open(ko, "wb") as out:
                run(["zstd", "-d", "-c", zst], stdout=out)
        else:
            plain = os.path.join(modules_dir, f"{m}.ko")
            if not os.path.exists(plain):
                raise SystemExit(f"missing vsock module {m} in {modules_dir}")
            shutil.copy2(plain, ko)

    # init
    init_dst = os.path.join(stage, "init")
    shutil.copy2(os.path.join(REPO, "vm/guest_init.sh"), init_dst)
    os.chmod(init_dst, 0o755)


def verify_no_trusted(stage):
    """Belt-and-suspenders: the staged tree must not contain a trusted
    module source (the import-graph test is the primary guard)."""
    bwdir = os.path.join(stage, "opt/bw/beigewall")
    present = {f[:-3] for f in os.listdir(bwdir) if f.endswith(".py")}
    leaked = present & TRUSTED_FORBIDDEN
    if leaked:
        raise SystemExit(f"trusted modules leaked into the guest: {sorted(leaked)}")


def pack(stage, out):
    script = os.path.join(stage, ".pack.sh")
    with open(script, "w") as fh:
        fh.write(
            "#!/bin/sh\nset -e\n"
            "mknod -m 622 dev/console c 5 1\n"
            "mknod -m 666 dev/null c 1 3\n"
            "mknod -m 666 dev/ttyS0 c 4 64\n"
            "find . -path ./.pack.sh -prune -o -print | cpio -o -H newc --quiet "
            f"| gzip -9 > {out}\n")
    os.chmod(script, 0o755)
    run(["fakeroot", "sh", ".pack.sh"], cwd=stage)
    os.unlink(script)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kernel-modules", required=True,
                   help="dir holding vmw_vsock_*.ko.zst and vsock.ko.zst")
    p.add_argument("--out", required=True, help="output dir for initramfs.gz")
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    out = os.path.abspath(os.path.join(a.out, "initramfs.gz"))
    stage = os.path.join(a.out, "stage")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    build_stage(stage, a.kernel_modules)
    verify_no_trusted(stage)
    pack(stage, out)
    size = os.path.getsize(out)
    print(f"wrote {out} ({size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
