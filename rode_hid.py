#!/usr/bin/env python3
"""HID transport for the RODECaster Duo firmware-update interface.

The RODECaster Duo exposes a HID control interface (USB vendor 0x19f7).
Firmware updates are driven over it with two one-byte commands, each sent as
HID output report ID 1 and acknowledged by the device with an input report
ID 2 carrying 'A' (0x41):

    'M' (0x4d)  enter update mode -> the device presents its update partition
                as a USB mass-storage disk
    'U' (0x55)  flash the archive previously copied onto that disk, then reboot

Protocol reverse-engineered and published by nicewrld:
    https://hhh.hn/rodecaster-duo-fw/

Sending 'M' interrupts normal audio operation; sending 'U' overwrites the
inactive firmware partition. Neither is sent unless a caller explicitly asks.
The device keeps two firmware partitions (A/B): a bad flash of one still boots
from the other, which is the recovery path if a build is wrong.
"""
from __future__ import annotations

import argparse
import os
import select
import sys
from pathlib import Path

VENDOR_ID = 0x19F7

REPORT_ID = 0x01
ACK_REPORT_ID = 0x02
ACK_BYTE = ord("A")  # 0x41

CMD_ENTER_UPDATE = ord("M")  # 0x4d, exposes mass-storage disk
CMD_FLASH = ord("U")         # 0x55, flashes and reboots

DEFAULT_REPORT_LEN = 64
ACK_TIMEOUT_S = 5.0


def find_hidraw(vendor_id: int = VENDOR_ID) -> Path:
    """Return the /dev/hidraw node for the first device with ``vendor_id``.

    Matched vendor-wide (not by product id) because the RODECaster changes its
    product id when multitrack mode is toggled.
    """
    matches: list[Path] = []
    for dev in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        try:
            text = (dev / "device" / "uevent").read_text()
        except OSError:
            continue
        for line in text.splitlines():
            # e.g. HID_ID=0003:000019F7:00000095
            if line.startswith("HID_ID="):
                parts = line.split(":")
                if len(parts) == 3 and int(parts[1], 16) == vendor_id:
                    matches.append(Path("/dev") / dev.name)
    if not matches:
        raise FileNotFoundError(
            f"no hidraw node for vendor {vendor_id:#06x}; is the RODECaster "
            "connected over USB?"
        )
    if len(matches) > 1:
        print(f"warning: multiple HID matches {matches}; using {matches[0]}",
              file=sys.stderr)
    return matches[0]


def send_command(node: Path, command: int, *,
                 report_len: int = DEFAULT_REPORT_LEN,
                 expect_ack: bool = True,
                 timeout: float = ACK_TIMEOUT_S) -> bool:
    """Send a one-byte ``command`` as output report ID 1; await the 'A' ack.

    Returns True when the device acknowledged (or when ``expect_ack`` is False).
    """
    report = bytes([REPORT_ID, command]) + bytes(report_len - 2)
    fd = os.open(node, os.O_RDWR)
    try:
        written = os.write(fd, report)
        if written != len(report):
            raise OSError(f"short write: {written}/{len(report)} bytes")
        if not expect_ack:
            return True
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return False
        resp = os.read(fd, report_len)
        return len(resp) >= 2 and resp[1] == ACK_BYTE
    finally:
        os.close(fd)


def enter_update_mode(node: Path, **kw) -> bool:
    """Send 'M': expose the update partition as a mass-storage disk."""
    return send_command(node, CMD_ENTER_UPDATE, **kw)


def trigger_flash(node: Path, **kw) -> bool:
    """Send 'U': flash the staged archive and reboot. Irreversible per boot."""
    return send_command(node, CMD_FLASH, **kw)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["probe", "enter-update", "flash-trigger"],
                    help="probe: locate the device only. "
                         "enter-update: send 'M'. flash-trigger: send 'U'.")
    ap.add_argument("--node", type=Path,
                    help="hidraw node to use (default: auto-detect by vendor)")
    ap.add_argument("--report-len", type=int, default=DEFAULT_REPORT_LEN)
    ap.add_argument("--yes", action="store_true",
                    help="required to actually send 'U' (flash-trigger)")
    args = ap.parse_args(argv)

    node = args.node or find_hidraw()
    print(f"device: {node}")
    if args.action == "probe":
        return 0
    if args.action == "flash-trigger" and not args.yes:
        print("refusing to send 'U' without --yes (this flashes firmware)",
              file=sys.stderr)
        return 2

    cmd = CMD_ENTER_UPDATE if args.action == "enter-update" else CMD_FLASH
    ok = send_command(node, cmd, report_len=args.report_len)
    print(f"sent {chr(cmd)!r}; ack={'yes' if ok else 'NO'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
