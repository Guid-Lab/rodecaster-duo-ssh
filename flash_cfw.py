#!/usr/bin/env python3
"""Stage and flash a firmware archive onto the RODECaster Duo.

End-to-end flow, mirroring the published procedure:

    1. send 'M' over HID            -> device exposes its update disk
    2. wait for the new block device
    3. mount it (udisksctl if available, else sudo mount)
    4. copy archive.tar.gz + archive.md5 onto it
    5. flush + unmount
    6. send 'U' over HID            -> device flashes and reboots  (gated)

Step 6 is only performed with --flash AND a confirmation (or --yes). Without
--flash the archive is staged and the tool stops, so you can inspect the disk or
trigger the flash by hand. The device's A/B partitioning means a bad image still
leaves the other partition bootable.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import rode_hid

SETTLE_TIMEOUT_S = 20.0
STAGED_NAME = "archive.tar.gz"  # name the device expects on the update disk
MD5_NAME = "archive.md5"


def _disk_size(dev: str) -> int:
    try:
        return int(Path(f"/sys/block/{dev}/size").read_text().strip())
    except OSError:
        return 0


def disk_sizes() -> dict[str, int]:
    return {p.name: _disk_size(p.name) for p in Path("/sys/block").glob("sd*")}


def wait_for_new_block_device(before: dict[str, int],
                              timeout: float = SETTLE_TIMEOUT_S) -> str | None:
    """Return a disk that appeared or went from empty (0 sectors) to populated.

    The RODECaster keeps its mass-storage endpoint present at all times (e.g. sda
    at 0 B); 'M' inserts the medium, so watch for size growth, not just new names.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for dev, size in disk_sizes().items():
            if size > 0 and before.get(dev, 0) == 0:
                time.sleep(1.0)  # let partitions settle
                return dev
        time.sleep(0.5)
    return None


def _first_partition(disk: str) -> str:
    parts = sorted(p.name for p in Path(f"/sys/block/{disk}").glob(f"{disk}*"))
    return parts[0] if parts else disk


def mount(devnode: str) -> Path:
    """Mount ``devnode`` and return the mountpoint. Prefers udisksctl."""
    if shutil.which("udisksctl"):
        out = subprocess.run(["udisksctl", "mount", "-b", devnode],
                             capture_output=True, text=True, check=True).stdout
        # "Mounted /dev/sdX1 at /run/media/user/LABEL."
        return Path(out.strip().rstrip(".").split(" at ")[-1])
    mountpoint = Path("/run/rode-cfw-mnt")
    mountpoint.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sudo", "mount", devnode, str(mountpoint)], check=True)
    return mountpoint


def unmount(devnode: str, mountpoint: Path) -> None:
    subprocess.run(["sync"], check=True)
    if shutil.which("udisksctl"):
        subprocess.run(["udisksctl", "unmount", "-b", devnode], check=False)
    else:
        subprocess.run(["sudo", "umount", str(mountpoint)], check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive_dir", type=Path,
                    help="directory holding archive.tar.gz and archive.md5")
    ap.add_argument("--node", type=Path, help="hidraw node (default: auto)")
    ap.add_argument("--flash", action="store_true",
                    help="after staging, send 'U' to flash (destructive)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive confirmation for --flash")
    args = ap.parse_args(argv)

    archive = args.archive_dir / STAGED_NAME
    md5 = args.archive_dir / MD5_NAME
    for f in (archive, md5):
        if not f.exists():
            print(f"missing {f}", file=sys.stderr)
            return 2

    node = args.node or rode_hid.find_hidraw()
    print(f"device: {node}")

    before = disk_sizes()
    print(f"block devices before: {before}")
    print("sending 'M' (enter update mode)...")
    if not rode_hid.enter_update_mode(node):
        print("no ack for 'M'; aborting", file=sys.stderr)
        return 1

    disk = wait_for_new_block_device(before)
    if not disk:
        print("update disk did not appear; aborting", file=sys.stderr)
        return 1
    devnode = f"/dev/{_first_partition(disk)}"
    print(f"update disk: {devnode}")

    mountpoint = mount(devnode)
    try:
        print(f"mounted at {mountpoint}; copying archive...")
        shutil.copy2(archive, mountpoint / archive.name)
        shutil.copy2(md5, mountpoint / md5.name)
    finally:
        unmount(devnode, mountpoint)
    print("archive staged on device.")

    if not args.flash:
        print("\nstaged only (no --flash). Re-run with --flash to send 'U', "
              "or trigger it manually.")
        return 0

    if not args.yes:
        reply = input("Send 'U' to FLASH now? This overwrites firmware. "
                      "Type 'flash' to proceed: ")
        if reply.strip() != "flash":
            print("aborted; archive left staged.")
            return 0

    print("sending 'U' (flash + reboot)...")
    ok = rode_hid.trigger_flash(node)
    print(f"flash command ack={'yes' if ok else 'NO'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
