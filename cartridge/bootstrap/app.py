"""NocturNation cartridge autoboot bootstrap.

Runs from the Flopagon's 2 KB EEPROM when the badge inserts a
NocturNation cartridge. Only responsibility: init the 16 MB flash,
mount it at MOUNT, hand off to the flash-resident installer
(cartridge/installer/).

SIZE BUDGET: ~2 KB total on the V1 EEPROM = 32-byte hexpansion header
+ LittleFS2 metadata + this file's compiled .mpy. Nathan's original
Flopagon app.mpy is 1528 bytes. That's the target ceiling.

Adding logic here overflows the EEPROM. Menus, multi-app selection,
directory copying, verbose error UI - all belong in the installer
(unlimited flash space). This file must stay minimal.

The two-attempt SPI init loop matches Nathan's Flopagon app: some
boards raise ValueError on the first FLASH() and succeed on retry
after a bus deinit. Removing the retry causes intermittent boot
failures.
"""

from lib.flash_spi import FLASH
from machine import SPI
import vfs
import sys
import app


MOUNT = "/cartridge"


class Bootstrap(app.App):
    def __init__(self, config=None):
        super().__init__()
        self._installer = None
        self._error = None
        self._mounted = False
        self._hspi = None

        cspin = config.pin[0]
        cspin.init(cspin.OUT, value=1)
        flash = None
        for _ in range(2):
            self._hspi = SPI(1, 10000000, sck=config.pin[1],
                             mosi=config.pin[2], miso=config.pin[3])
            try:
                flash = FLASH(self._hspi, (cspin,), cmd5=False)
                break
            except ValueError:
                self._hspi.deinit()
                self._hspi = None
        if flash is None:
            self._error = "Flash init"
            return

        try:
            vfs.mount(flash, MOUNT)
            self._mounted = True
        except OSError:
            self._error = "Mount"
            return

        if MOUNT not in sys.path:
            sys.path.append(MOUNT)

        try:
            from installer.app import InstallerApp
        except Exception:
            self._error = "No installer"
            return

        try:
            self._installer = InstallerApp(config=config)
        except Exception:
            self._error = "Installer init"

    def draw(self, ctx):
        if self._installer is not None:
            self._installer.draw(ctx)
            return
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(1, 0.4, 0.4)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 18
        ctx.move_to(0, -10).text("Cartridge error")
        ctx.font_size = 14
        ctx.move_to(0, 15).text(self._error or "unknown")

    def update(self, delta):
        if self._installer is not None:
            return self._installer.update(delta)
        return True

    def background_update(self, delta):
        if self._installer is not None:
            self._installer.background_update(delta)

    def deinit(self):
        if self._installer is not None and hasattr(self._installer, "deinit"):
            try:
                self._installer.deinit()
            except Exception:
                pass
        if self._mounted:
            try:
                vfs.umount(MOUNT)
            except OSError:
                pass
        try:
            if MOUNT in sys.path:
                sys.path.remove(MOUNT)
        except ValueError:
            pass
        if self._hspi is not None:
            try:
                self._hspi.deinit()
            except Exception:
                pass


__app_export__ = Bootstrap
