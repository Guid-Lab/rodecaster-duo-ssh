# rodecaster-duo-ssh

Proof-of-concept tooling to enable SSH (root) on a RØDECaster Duo by building a
modified firmware image and flashing it over USB. Once flashed, the unit can be
reached over the network, which is otherwise not possible from Linux (there is no
RØDE Central for Linux).

This is a POC. It modifies device firmware, which voids your warranty and can
conflict with future official updates. Use at your own risk.

Credit for the original research (firmware format, HID flash protocol) goes to
nicewrld: https://hhh.hn/rodecaster-duo-fw/

## How it works

- The firmware is a plain gzipped tar (`os.tar.gz`) with an `os.md5` sidecar and
  **no signature check**, so a repacked image is accepted as-is.
- Flashing is driven over the HID interface (USB vendor `0x19f7`) with two
  one-byte commands, each sent as output report 1 and acked with `A` (`0x41`):
  - `M` (`0x4d`) – enter update mode; the unit exposes its update partition as a
    USB mass-storage disk.
  - `U` (`0x55`) – flash the archive copied onto that disk, then reboot.
- The device has two firmware partitions (A/B): a bad flash of one still boots
  from the other.

The only change this makes is adding your SSH public key to root's
`authorized_keys`. It does not enable password login or touch `sshd_config`
unless you ask it to.

## Requirements

- Linux, Python 3.8+, `fakeroot`, and `udisksctl` (or `sudo mount`).
- The RØDECaster Duo connected over USB.
- Access to the HID device without root. Install the udev rule:

  ```sh
  sudo cp 70-rode-hidraw.rules /etc/udev/rules.d/
  sudo udevadm control --reload && sudo udevadm trigger
  ```

## Usage

### 1. Generate an SSH key

Nothing is bundled. Generate your own keypair:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/rodecaster_duo -C rodecaster-duo
```

Keep the private key safe. Only the public key goes into the image.

### 2. Download the stock firmware

The firmware URL comes from RØDE's per-device manifest. For the Duo:

```sh
curl -s https://update.rode.com/rcp2-mini-manifest.json
```

Pick the version you want from that manifest, then download the OS image and its
checksum (replace `VERSION`):

```sh
mkdir -p stock
curl -o stock/os.tar.gz https://update.rode.com/rcp2-duo/VERSION/os.tar.gz
curl -o stock/os.md5    https://update.rode.com/rcp2-duo/VERSION/os.md5
[ "$(md5sum stock/os.tar.gz | cut -d' ' -f1)" = "$(cat stock/os.md5)" ] && echo OK
```

### 3. Build the modified image

Run under `fakeroot` so file ownership and permissions are preserved:

```sh
fakeroot python build_cfw.py stock/os.tar.gz -o out \
    --authorized-key ~/.ssh/rodecaster_duo.pub
```

This writes `out/archive.tar.gz` and `out/archive.md5`.

Optional: `--enable-password-auth` and `--root-password PASS` if you also want
password login (off by default).

### 4. Flash

```sh
# stage only (copies the image, does NOT flash)
python flash_cfw.py out

# stage then flash (prompts; add --yes to skip)
python flash_cfw.py out --flash
```

After `U` the device writes the inactive partition (this takes a few minutes),
then reboots into the new firmware.

### 5. Connect

The device gets a new SSH host key after flashing, so clear any stale entry:

```sh
ssh-keygen -R <device-ip>
ssh -i ~/.ssh/rodecaster_duo root@<device-ip>
```

## Scripts

| File            | Role |
|-----------------|------|
| `rode_hid.py`   | HID transport: `probe`, `enter-update`, `flash-trigger`. |
| `build_cfw.py`  | Build the modified `archive.tar.gz` + `archive.md5`. |
| `flash_cfw.py`  | Stage the image onto the device and (optionally) flash. |

## Notes

- Tested on a RØDECaster Duo on firmware 1.7.6. The RØDECaster Pro II uses the
  `rcp2` path and `manifest.json` instead; it is untested here.
- Once SSH is on, the unit runs an OpenSSH server reachable on your network.
  Keep it on an isolated VLAN.
- To revert, build from a stock image and flash again.
