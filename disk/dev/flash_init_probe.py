"""disk/dev/flash_init_probe.py

Standalone diagnostic that walks through the same SPI + FLASH +
vfs.mount sequence our EEPROM bootstrap does, but with a timestamp
+ description printed before every step and a short sleep after
each print so serial output flushes before any potential crash /
brownout / reboot.

Point: isolate WHICH step triggers the "reboot on Flopagon insert"
symptom. Not shipped as part of the app - it's a manual bench tool
you invoke via mpremote.

Two failure modes we can distinguish:

  (A) Reboot happens during badge OS auto-mount of the EEPROM.
      Fires BEFORE our bootstrap even runs. Not reproducible here
      (this script doesn't touch the EEPROM). If a Flopagon insert
      reboots the badge but this script runs clean, that's the badge
      OS's HexpansionManagerApp path - bug lives there, not us.

  (B) Reboot happens during our bootstrap's SPI or FLASH init.
      Reproducible here. Last [probe] line before the reboot points
      at the offending call. Likely candidates: SPI() itself, or the
      first FLASH() read that pulls current on the flash chip.

Usage:

    # Edit PORT below to match whichever slot the Flopagon is in.
    # Then, with the Flopagon inserted:
    mpremote resume run disk/dev/flash_init_probe.py

If the probe reproduces the reboot, note the last [probe] line -
that's the diagnostic. If it runs clean end-to-end but Flopagon
insertion still reboots the badge, we're looking at failure mode
(A) and the fix lives elsewhere.

The probe MOUNTS at /probe_mount (not /disk) to avoid
colliding with a live bootstrap. It unmounts cleanly at the end
even on partial failure.
"""

import os
import sys
import time
import vfs

from lib.flash_spi import FLASH
from machine import SPI
from system.hexpansion.config import HexpansionConfig


# ---------------------------------------------------------------------
# EDIT THIS to the port your Flopagon is inserted in (1..6, clockwise
# from top-right when the badge is right-way-up).
PORT = 1
# ---------------------------------------------------------------------

MOUNT = "/probe_mount"
SPI_ATTEMPTS = 4  # more than the production bootstrap's 2 so we can
                  # tell an intermittent from a hard failure


def _stamp(label):
    # Short sleep after each print so serial has time to drain
    # before any potential brownout swallows the line.
    print("[probe] %d ms  %s" % (time.ticks_ms(), label))
    time.sleep_ms(20)


def main():
    _stamp("start (port %d)" % PORT)

    try:
        config = HexpansionConfig(PORT)
    except Exception as exc:
        _stamp("HexpansionConfig(%d) FAILED: %s" % (PORT, exc))
        return
    _stamp("HexpansionConfig ok")

    try:
        cspin = config.pin[0]
    except Exception as exc:
        _stamp("config.pin[0] FAILED: %s" % exc)
        return
    _stamp("cspin fetched (config.pin[0])")

    try:
        cspin.init(cspin.OUT, value=1)
    except Exception as exc:
        _stamp("cspin.init FAILED: %s" % exc)
        return
    _stamp("cspin.init OUT/high ok")

    hspi = None
    flash = None
    for attempt in range(1, SPI_ATTEMPTS + 1):
        _stamp("SPI attempt %d begin" % attempt)
        try:
            hspi = SPI(1, 10000000,
                       sck=config.pin[1],
                       mosi=config.pin[2],
                       miso=config.pin[3])
        except Exception as exc:
            _stamp("SPI %d FAILED: %s" % (attempt, exc))
            continue
        _stamp("SPI %d ok (10 MHz, pins 1..3)" % attempt)

        _stamp("FLASH attempt %d begin" % attempt)
        try:
            flash = FLASH(hspi, (cspin,), cmd5=False)
        except ValueError as exc:
            _stamp("FLASH %d ValueError: %s" % (attempt, exc))
            try:
                hspi.deinit()
                _stamp("hspi.deinit ok after failed FLASH")
            except Exception as exc2:
                _stamp("hspi.deinit FAILED: %s" % exc2)
            hspi = None
            continue
        except Exception as exc:
            _stamp("FLASH %d UNEXPECTED %s: %s"
                   % (attempt, type(exc).__name__, exc))
            hspi = None
            break
        _stamp("FLASH %d ok" % attempt)
        break

    if flash is None:
        _stamp("ERROR: flash init failed after %d attempts" % SPI_ATTEMPTS)
        return

    _stamp("vfs.mount(flash, %s) begin" % MOUNT)
    try:
        vfs.mount(flash, MOUNT)
    except Exception as exc:
        _stamp("vfs.mount FAILED: %s" % exc)
        try:
            hspi.deinit()
        except Exception:
            pass
        return
    _stamp("vfs.mount ok")

    _stamp("os.listdir(%s) begin" % MOUNT)
    try:
        entries = os.listdir(MOUNT)
        _stamp("listdir ok: %d entries" % len(entries))
        for e in entries:
            print("       %s" % e)
    except Exception as exc:
        _stamp("listdir FAILED: %s" % exc)

    _stamp("vfs.umount(%s) begin" % MOUNT)
    try:
        vfs.umount(MOUNT)
        _stamp("umount ok")
    except Exception as exc:
        _stamp("umount FAILED: %s" % exc)

    _stamp("hspi.deinit begin")
    try:
        hspi.deinit()
        _stamp("hspi.deinit ok")
    except Exception as exc:
        _stamp("hspi.deinit FAILED: %s" % exc)

    _stamp("done - full sequence completed without reboot")


main()
