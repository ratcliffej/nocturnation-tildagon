"""disk/dev/eeprom_reformat.py

Nuclear recovery for a Flopagon whose 2 KB EEPROM LFS2 has gone
sideways (repeated ENOSPC / ENAMETOOLONG on `mpremote cp` even
after the file has been rm'd, mount visible but writes fail).

Mirrors Nathan's `prepare_eeprom.py::setup_flopagon` but writes
our bootstrap `app.mpy` instead of his. In one shot:

    1. Detect EEPROM I2C address
    2. Write a fresh Flopagon V1 header (32-byte, matches Nathan's)
    3. VfsLfs2.mkfs the partition (wipes everything)
    4. Mount at /eeprom_recovery
    5. Copy /remote/disk/bootstrap/app.mpy onto it
    6. Umount

Requires the write-protect jumper to be shorted (on Jason's dev
board this is permanent). If the write is refused silently, check
the jumper.

Usage (from the repo root, with the Flopagon inserted):

    mpremote mount . run disk/dev/eeprom_reformat.py

Edit `PORT` below to match your Flopagon slot.
"""

import vfs
from machine import I2C

from system.hexpansion.header import HexpansionHeader, write_header
from system.hexpansion.util import (
    detect_eeprom_addr,
    get_hexpansion_block_devices,
    read_hexpansion_header,
)


# ---------------------------------------------------------------------
# EDIT THIS to the port your Flopagon is inserted in (1..6, clockwise
# from top-right when the badge is right-way-up).
PORT = 1
# ---------------------------------------------------------------------

# Path to the compiled bootstrap on the mounted host filesystem
# (mpremote maps repo root -> /remote).
BOOTSTRAP_SRC = "/remote/disk/bootstrap/app.mpy"

# Nathan's Flopagon V1 header - taken verbatim from
# https://github.com/hairymnstr/Flopagon prepare_eeprom.py so this
# board looks identical to a stock Flopagon after we're done. Do NOT
# change unless you're targeting a different hexpansion.
_HEADER = HexpansionHeader(
    manifest_version="2024",
    fs_offset=32,
    eeprom_page_size=16,
    eeprom_total_size=1024 * (16 // 8),
    vid=0xCAFE,
    pid=0xD15C,
    unique_id=0,
    friendly_name="Flopagon",
)

RECOVERY_MOUNT = "/eeprom_recovery"


def _log(msg):
    print("[reformat] %s" % msg)


def main():
    _log("port %d" % PORT)

    i2c = I2C(PORT)
    addr, addr_len = detect_eeprom_addr(i2c)
    if addr is None:
        _log("ERROR: no EEPROM detected on this port")
        return
    _log("EEPROM at %s (addr_len=%d)" % (hex(addr), addr_len))

    _log("writing fresh Flopagon V1 header")
    write_header(PORT, _HEADER, addr=addr, addr_len=addr_len,
                 page_size=_HEADER.eeprom_page_size)

    header = read_hexpansion_header(i2c, addr,
                                    set_read_addr=True,
                                    addr_len=addr_len)
    if header is None:
        _log("ERROR: failed to read back header")
        return
    _log("header reads back OK: %s" % header.friendly_name)

    _log("get_hexpansion_block_devices")
    eep, partition = get_hexpansion_block_devices(i2c, header, addr,
                                                   addr_len=addr_len)

    _log("VfsLfs2.mkfs(partition) - wipes filesystem")
    vfs.VfsLfs2.mkfs(partition)

    # Umount any stale mount at our recovery path.
    try:
        vfs.umount(RECOVERY_MOUNT)
    except OSError:
        pass

    _log("mount fresh LFS2 at %s" % RECOVERY_MOUNT)
    vfs.mount(partition, RECOVERY_MOUNT)

    _log("copying %s -> %s/app.mpy" % (BOOTSTRAP_SRC, RECOVERY_MOUNT))
    try:
        with open(BOOTSTRAP_SRC, "rb") as fr:
            with open(RECOVERY_MOUNT + "/app.mpy", "wb") as fw:
                total = 0
                while True:
                    chunk = fr.read(64)
                    if not chunk:
                        break
                    fw.write(chunk)
                    total += len(chunk)
        _log("wrote %d bytes" % total)
    except Exception as exc:
        _log("ERROR: copy failed: %s" % exc)
        try:
            vfs.umount(RECOVERY_MOUNT)
        except OSError:
            pass
        return

    try:
        vfs.umount(RECOVERY_MOUNT)
        _log("umount ok")
    except OSError as exc:
        _log("umount FAILED: %s" % exc)

    _log("done - hard-reset the badge (mpremote reset) then reinsert Flopagon")


main()
