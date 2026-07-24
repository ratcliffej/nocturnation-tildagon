"""NocturNation disk autoboot bootstrap.

Runs from the Flopagon's 2 KB EEPROM when the badge inserts a
NocturNation disk. Only responsibility: init the 16 MB flash,
mount it at MOUNT, hand off to the flash-resident installer
(disk/installer/).

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
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus
from system.scheduler.events import RequestForegroundPushEvent, RequestStartAppEvent


MOUNT = "/disk"


class Bootstrap(app.App):
    def __init__(self, config=None):
        super().__init__()
        # button_states owned by us for the error-state exit path -
        # if we get stuck showing a mount/flash-init error, F lets the
        # operator escape back to the launcher without physically
        # yanking the Flopagon.
        self.button_states = Buttons(self)
        self._foregrounded = False
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

        # Defensive umount - the badge OS may fire two insertion
        # events in quick succession (contact bounce / rescan) with
        # no matching removal, leaving a stale mount from a previous
        # Bootstrap instance. Same pattern the badge OS itself uses
        # for the EEPROM auto-mount.
        try:
            vfs.umount(MOUNT)
        except OSError:
            pass
        try:
            vfs.mount(flash, MOUNT)
            self._mounted = True
        except OSError as exc:
            e = exc.args[0] if exc.args else 0
            self._error = "Mount %d" % e
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
            return

        # Register the installer with the scheduler so its own
        # RequestForegroundPushEvent is honoured. Without this, the
        # scheduler drops the push with
        # "Foreground request ignored for app that's not running" and
        # the installer never becomes visible. The scheduler also
        # doesn't tick background_task for un-registered apps, so the
        # copy loop wouldn't run either.
        eventbus.emit(RequestStartAppEvent(self._installer))

    def draw(self, ctx):
        # Only fires when we're foregrounded. In the success case that
        # never happens - the installer becomes foreground app and
        # draws itself. Only reached in an error state.
        if self._error is None:
            return
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(1, 0.4, 0.4)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 18
        ctx.move_to(0, -10).text("Disk error")
        ctx.font_size = 14
        ctx.move_to(0, 15).text(self._error or "unknown")

    def update(self, delta):
        # Only take foreground in the error state - success case has
        # the installer taking it via its own push, which would race
        # our push and briefly flash the error rendering.
        if self._error is None:
            return True
        if not self._foregrounded:
            eventbus.emit(RequestForegroundPushEvent(self))
            self._foregrounded = True
        # F escape: without this, mount/flash failures could only be
        # cleared by physically yanking the Flopagon.
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.minimise()
        return True

    def deinit(self):
        # Installer was registered separately with the scheduler
        # (RequestStartAppEvent above), so it must be explicitly
        # terminated on Flopagon removal - otherwise its update() +
        # background_task keep firing against a torn-down /disk
        # mount. terminate() reuses the base App class's already-
        # imported RequestStopAppEvent, saving us the import bytes.
        if self._installer is not None:
            try:
                self._installer.terminate()
            except Exception:
                pass
            if hasattr(self._installer, "deinit"):
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
