"""disk/dev/provision_flopagon.py

One-shot Flopagon provisioning. Does what Nathan's setup_flopagon +
our eeprom_reformat + populate_flopagon do, all in one mpremote
session:

    1. Detect EEPROM I2C addr, write Flopagon V1 header
    2. VfsLfs2.mkfs the EEPROM partition (wipes any old state)
    3. Copy our bootstrap app.mpy onto the EEPROM
    4. Init SPI, mount the 16 MB flash directly (no bootstrap run yet)
    5. Wipe /installer + /apps on the flash
    6. Copy every disk/installer/*.py + disk/dev/testapp/*
    7. Umount everything cleanly

After completion:

    mpremote reset
    # physical remove + reinsert Flopagon
    # -> bootstrap runs, mounts /disk, launches installer hub

Requires:
- Flopagon inserted in PORT (edit below)
- Write-protect jumper shorted (permanent on Jason's dev boards)
- Bootstrap .mpy freshly compiled at disk/bootstrap/app.mpy

Usage:
    mpremote resume mount . run disk/dev/provision_flopagon.py

Or the shell wrapper handles the compile + reset too:
    ./disk/dev/provision_flopagon.sh
"""

import os
import vfs
from machine import I2C, SPI

from lib.flash_spi import FLASH
from system.hexpansion.config import HexpansionConfig
from system.hexpansion.header import HexpansionHeader, write_header
from system.hexpansion.util import (
    detect_eeprom_addr,
    get_hexpansion_block_devices,
    read_hexpansion_header,
)


# ---------------------------------------------------------------------
# EDIT THIS to your Flopagon port (1..6, clockwise from top-right).
PORT = 1
# ---------------------------------------------------------------------

BOOTSTRAP_SRC = "/remote/disk/bootstrap/app.mpy"
INSTALLER_SRC = "/remote/disk/installer"
TESTAPP_SRC = "/remote/disk/dev/testapp"

EEPROM_MOUNT = "/eeprom_setup"
FLASH_MOUNT = "/disk_setup"

# Nathan's Flopagon V1 header verbatim - stock-Flopagon compatible.
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


def _log(msg):
    print("[provision] %s" % msg)


def _copy_file(src, dst):
    with open(src, "rb") as fr:
        with open(dst, "wb") as fw:
            while True:
                chunk = fr.read(4096)
                if not chunk:
                    break
                fw.write(chunk)


def _mkdir_p(p):
    try:
        os.mkdir(p)
    except OSError:
        pass


def _rmtree(p):
    try:
        for e in os.ilistdir(p):
            path = p + "/" + e[0]
            if e[1] & 0x4000:
                _rmtree(path)
            else:
                os.remove(path)
        os.rmdir(p)
    except OSError:
        pass


def _copy_dir_files(src_dir, dst_dir, filter_ext=None):
    """Copy immediate (non-recursive) file children of src_dir into
    dst_dir. Skips subdirs. Optional extension filter (e.g. '.py')."""
    _mkdir_p(dst_dir)
    for e in sorted(os.ilistdir(src_dir)):
        name, kind = e[0], e[1]
        if kind & 0x4000:
            continue
        if filter_ext is not None and not name.endswith(filter_ext):
            continue
        _copy_file(src_dir + "/" + name, dst_dir + "/" + name)
        _log("  %s" % name)


# ---------------------------------------------------------------------

def provision_eeprom(port):
    _log("EEPROM: init I2C on port %d" % port)
    i2c = I2C(port)
    addr, addr_len = detect_eeprom_addr(i2c)
    if addr is None:
        raise RuntimeError("no EEPROM detected on port %d" % port)
    _log("EEPROM: addr=%s addr_len=%d" % (hex(addr), addr_len))

    _log("EEPROM: writing header")
    write_header(port, _HEADER, addr=addr, addr_len=addr_len,
                 page_size=_HEADER.eeprom_page_size)
    header = read_hexpansion_header(i2c, addr,
                                    set_read_addr=True,
                                    addr_len=addr_len)
    if header is None:
        raise RuntimeError("failed to read back header")
    _log("EEPROM: header ok (%s)" % header.friendly_name)

    eep, partition = get_hexpansion_block_devices(i2c, header, addr,
                                                   addr_len=addr_len)
    _log("EEPROM: mkfs LFS2 (wipes filesystem)")
    vfs.VfsLfs2.mkfs(partition)

    try:
        vfs.umount(EEPROM_MOUNT)
    except OSError:
        pass
    vfs.mount(partition, EEPROM_MOUNT)
    _log("EEPROM: mounted at %s" % EEPROM_MOUNT)

    _log("EEPROM: copying %s" % BOOTSTRAP_SRC)
    _copy_file(BOOTSTRAP_SRC, EEPROM_MOUNT + "/app.mpy")
    _log("EEPROM: wrote app.mpy (%d bytes)"
         % os.stat(EEPROM_MOUNT + "/app.mpy")[6])

    vfs.umount(EEPROM_MOUNT)
    _log("EEPROM: umount ok")


def provision_flash(port):
    _log("FLASH: init SPI on port %d" % port)
    config = HexpansionConfig(port)
    cspin = config.pin[0]
    cspin.init(cspin.OUT, value=1)

    hspi = None
    flash = None
    for attempt in range(1, 3):
        _log("FLASH: SPI attempt %d" % attempt)
        hspi = SPI(1, 10000000,
                   sck=config.pin[1],
                   mosi=config.pin[2],
                   miso=config.pin[3])
        try:
            flash = FLASH(hspi, (cspin,), cmd5=False)
            break
        except ValueError as exc:
            _log("FLASH: SPI attempt %d failed: %s" % (attempt, exc))
            hspi.deinit()

    if flash is None:
        raise RuntimeError("could not init FLASH after 2 attempts")
    _log("FLASH: FLASH init ok")

    try:
        vfs.umount(FLASH_MOUNT)
    except OSError:
        pass
    vfs.mount(flash, FLASH_MOUNT)
    _log("FLASH: mounted at %s" % FLASH_MOUNT)

    _log("FLASH: wiping /installer and /apps")
    _rmtree(FLASH_MOUNT + "/installer")
    _rmtree(FLASH_MOUNT + "/apps")

    _log("FLASH: copying installer sources")
    _copy_dir_files(INSTALLER_SRC,
                    FLASH_MOUNT + "/installer",
                    filter_ext=".py")

    _log("FLASH: copying testapp")
    _mkdir_p(FLASH_MOUNT + "/apps")
    _copy_dir_files(TESTAPP_SRC, FLASH_MOUNT + "/apps/testapp")

    vfs.umount(FLASH_MOUNT)
    _log("FLASH: umount ok")

    hspi.deinit()


def main():
    _log("port %d" % PORT)
    provision_eeprom(PORT)
    provision_flash(PORT)
    _log("done - mpremote reset + reinsert Flopagon to activate")


main()
