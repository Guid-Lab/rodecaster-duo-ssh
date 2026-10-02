#!/usr/bin/env python3
"""Build a modified RODECaster Duo firmware archive with SSH access enabled.

Takes the stock firmware tarball and produces a ready-to-flash archive plus its
.md5 sidecar, applying any combination of:

  * an added SSH public key in root's authorized_keys
  * PasswordAuthentication enabled in every sshd_config found in the rootfs
  * a root password set in /etc/shadow (SHA-256 crypt)

The stock firmware is a plain gzipped tar with no signature check, so a repacked
archive is accepted by the device as-is (flash it with flash_cfw.py).

Run under fakeroot (or as root) so ownership and /etc/shadow permissions survive
the round-trip:

    fakeroot python build_cfw.py stock/archive.tar.gz -o out \\
        --authorized-key ~/.ssh/rode_cfw.pub --enable-password-auth

OPEN POINT: the exact byte layout the device expects in archive.md5 is not yet
confirmed. --md5-format defaults to the common `md5sum` layout; verify it against
the stock archive.md5 once you have one, and override if it differs.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

STAGED_NAME = "archive.tar.gz"  # name the device expects on the update disk
MD5_NAME = "archive.md5"
DEFAULT_SALT = "rodessh00"  # salt used in the published makecfw.sh


def _warn_privileges() -> None:
    if os.geteuid() != 0 and "FAKEROOTKEY" not in os.environ:
        print("warning: not root and not under fakeroot; file ownership and "
              "shadow permissions may be wrong in the rebuilt archive. "
              "Re-run as: fakeroot python build_cfw.py ...", file=sys.stderr)


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        # fully_trusted: this is a firmware rootfs we repack faithfully; the
        # data filter (Python 3.12+) rejects the absolute symlinks busybox uses.
        tar.extractall(dest, numeric_owner=True, filter="fully_trusted")


def _root_home(rootfs: Path) -> Path:
    """Resolve root's home dir from /etc/passwd (Yocto uses /home/root)."""
    passwd = rootfs / "etc" / "passwd"
    try:
        for line in passwd.read_text().splitlines():
            fields = line.split(":")
            if len(fields) >= 6 and fields[0] == "root" and fields[5]:
                return rootfs / fields[5].lstrip("/")
    except OSError:
        pass
    for candidate in ("home/root", "root"):
        if (rootfs / candidate).exists():
            return rootfs / candidate
    return rootfs / "home" / "root"


def add_authorized_key(rootfs: Path, pubkey_files: list[Path]) -> None:
    home = _root_home(rootfs)
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    authorized = ssh_dir / "authorized_keys"
    keys = [p.read_text().strip() for p in pubkey_files]
    existing = authorized.read_text() if authorized.exists() else ""
    with authorized.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        for key in keys:
            fh.write(key + "\n")
    os.chmod(ssh_dir, 0o700)
    os.chmod(authorized, 0o600)
    for path in (home, ssh_dir, authorized):
        try:
            os.chown(path, 0, 0)
        except PermissionError:
            pass  # will be a no-op outside root/fakeroot; warned already
    print(f"authorized_keys: added {len(keys)} key(s) at {authorized}")


def enable_password_auth(rootfs: Path) -> None:
    # The Duo rootfs ships both sshd_config and sshd_config_readonly; patch both.
    found = [p for p in rootfs.rglob("sshd_config*") if p.is_file()]
    if not found:
        print("warning: no sshd_config found in rootfs", file=sys.stderr)
    for cfg in found:
        lines = cfg.read_text().splitlines()
        seen = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower().startswith(("passwordauthentication",
                                            "#passwordauthentication")):
                lines[i] = "PasswordAuthentication yes"
                seen = True
        if not seen:
            lines.append("PasswordAuthentication yes")
        cfg.write_text("\n".join(lines) + "\n")
        print(f"sshd_config: enabled PasswordAuthentication in {cfg}")


def _crypt_sha256(password: str, salt: str) -> str:
    """SHA-256 crypt hash ($5$...), via stdlib crypt, mkpasswd, or openssl."""
    try:
        import crypt  # removed in Python 3.13; fall through if absent
        return crypt.crypt(password, f"$5${salt}$")
    except Exception:
        pass
    if shutil.which("mkpasswd"):
        return subprocess.run(
            ["mkpasswd", "-m", "sha-256", "-S", salt, password],
            capture_output=True, text=True, check=True).stdout.strip()
    if shutil.which("openssl"):
        return subprocess.run(
            ["openssl", "passwd", "-5", "-salt", salt, password],
            capture_output=True, text=True, check=True).stdout.strip()
    raise RuntimeError("no hashing backend (need python crypt, mkpasswd or openssl)")


def set_root_password(rootfs: Path, password: str, salt: str) -> None:
    shadow = rootfs / "etc" / "shadow"
    digest = _crypt_sha256(password, salt)
    lines = shadow.read_text().splitlines()
    for i, line in enumerate(lines):
        fields = line.split(":")
        if fields and fields[0] == "root":
            fields[1] = digest
            lines[i] = ":".join(fields)
            break
    else:
        raise RuntimeError("no root entry in /etc/shadow")
    shadow.write_text("\n".join(lines) + "\n")
    print(f"shadow: set root password hash in {shadow}")


def repack(rootfs: Path, out_archive: Path) -> None:
    # Preserve the stock layout: members are './'-prefixed from the rootfs root.
    with tarfile.open(out_archive, "w:gz") as tar:
        tar.add(rootfs, arcname=".", recursive=True)
    print(f"repacked -> {out_archive}")


def write_md5(archive: Path, md5_path: Path, fmt: str) -> None:
    digest = hashlib.md5(archive.read_bytes()).hexdigest()
    md5_path.write_text(fmt.format(hash=digest, name=STAGED_NAME) + "\n")
    print(f"md5: {digest} -> {md5_path}")


def _read_password(args) -> str | None:
    if args.root_password_file:
        return Path(args.root_password_file).read_text().strip()
    return args.root_password


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path, help="stock firmware archive.tar.gz")
    ap.add_argument("-o", "--out-dir", type=Path, required=True,
                    help="output directory for archive.tar.gz + archive.md5")
    ap.add_argument("--authorized-key", type=Path, action="append", default=[],
                    metavar="PUBKEY", help="SSH public key file (repeatable)")
    ap.add_argument("--enable-password-auth", action="store_true")
    ap.add_argument("--root-password", help="plaintext root password to set")
    ap.add_argument("--root-password-file",
                    help="file holding the root password (e.g. a pass extract)")
    ap.add_argument("--salt", default=DEFAULT_SALT)
    ap.add_argument("--md5-format", default="{hash}",
                    help="archive.md5 content; {hash} and {name} expand. "
                         "Stock os.md5 is a bare hex digest, which is the default.")
    ap.add_argument("--keep-workdir", action="store_true")
    args = ap.parse_args(argv)

    if not (args.authorized_key or args.enable_password_auth
            or args.root_password or args.root_password_file):
        ap.error("nothing to do: pass --authorized-key, --enable-password-auth "
                 "and/or --root-password[-file]")

    _warn_privileges()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="rode-cfw-"))
    rootfs = workdir / "mod"
    try:
        print(f"extracting {args.archive} -> {rootfs}")
        extract(args.archive, rootfs)

        if args.authorized_key:
            add_authorized_key(rootfs, args.authorized_key)
        if args.enable_password_auth:
            enable_password_auth(rootfs)
        password = _read_password(args)
        if password:
            set_root_password(rootfs, password, args.salt)

        out_archive = args.out_dir / STAGED_NAME
        repack(rootfs, out_archive)
        write_md5(out_archive, args.out_dir / MD5_NAME, args.md5_format)
        print(f"\nready to flash: {out_archive}")
        return 0
    finally:
        if args.keep_workdir:
            print(f"workdir kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
