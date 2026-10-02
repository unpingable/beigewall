#!/bin/busybox sh
# Phase 7 guest init (PID 1) for the smallest BeigeWall boundary VM.
# Mounts the pseudo-filesystems, loads vsock, brings up loopback, then hands
# off to guest_main.py which reads bw.cfg from /proc/cmdline and runs either
# the OS-boundary probe suite or the untrusted agent. No network device is
# configured; loopback exists only for the intra-guest model forwarder.

/bin/busybox --install -s /bin >/dev/null 2>&1

mount -t proc    proc    /proc
mount -t sysfs   sysfs   /sys
mount -t devtmpfs dev    /dev 2>/dev/null
mount -t tmpfs   tmpfs   /tmp

# vsock stack (modules shipped decompressed in /modules, insmod in dep order)
for m in vsock vmw_vsock_virtio_transport_common vmw_vsock_virtio_transport; do
    insmod /modules/$m.ko 2>/dev/null
done

ip link set lo up 2>/dev/null || ifconfig lo up 2>/dev/null

export PYTHONDONTWRITEBYTECODE=1
export HOME=/root
echo "BW-GUEST: booted $(uname -r); running guest_main"
/bin/python3 /opt/bw/vm/guest_main.py
rc=$?
echo "BW-GUEST: guest_main exited rc=$rc"
sync
poweroff -f
