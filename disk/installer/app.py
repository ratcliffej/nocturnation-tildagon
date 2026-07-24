"""Flopagon disk installer app.

Runs on the Tildagon after the EEPROM bootstrap (Phase 3) mounts the
16 MB flash and hands off to us. Enumerates the apps/ folders on the
disk, shows a picker, and copies the chosen app(s) into /apps/
on the badge's internal filesystem.

Flow:
    picker  ->  confirm  ->  installing  ->  done

Copy progress runs on background_update() so draw() keeps rendering
one file at a time (a 51-file NocturNation install completes in ~2.5 s
at the 20 Hz tick rate).

Mount path is discovered at import time from __file__ so we work
wherever the bootstrap drops us:
    installed as /X/installer/app.py -> we read /X/apps/*/
This means Phase 3 can choose any mount path without a code change
here, and the Phase 2 smoke test can stage under /apps/ (persistent
across badge OS updates) instead of the root, which gets wiped.
"""

import os

import app
from app_components import Menu, clear_background
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus
from system.launcher.events import InstallNotificationEvent
from system.scheduler.events import (
    RequestForegroundPushEvent,
    RequestForegroundPopEvent,
)

from . import _fsutil as fsutil
from . import _manifest as manifest


def _discover_disk_mount():
    # __file__ points at .../installer/app.py; the disk root is
    # its grandparent. Falls back if __file__ is unavailable (some
    # MicroPython frozen-module builds).
    try:
        installer_dir = __file__.rsplit("/", 1)[0]
        return installer_dir.rsplit("/", 1)[0]
    except (NameError, AttributeError):
        return "/disk"


DISK_MOUNT = _discover_disk_mount()
DISK_APPS_DIR = DISK_MOUNT + "/apps"
BADGE_APPS_DIR = "/apps"

_STATE_PICKER = "picker"
_STATE_CONFIRM = "confirm"
_STATE_INSTALLING = "installing"
_STATE_DONE = "done"


class InstallerApp(app.App):
    def __init__(self, config=None):
        super().__init__()
        self.config = config
        # Owns its own Buttons subscription because the done-screen
        # exit path uses self.button_states.get() directly. The picker
        # phase gets its buttons via Menu, but Menu goes away when we
        # transition to installing, so we need our own for the tail.
        self.button_states = Buttons(self)

        self._foregrounded = False
        self._state = _STATE_PICKER

        self._catalog = _enumerate_disk_apps()
        self._install_targets = []
        self._install_queue = []
        self._install_cursor = 0
        self._install_current_name = ""
        self._install_results = []
        self._error_message = None

        self._menu = self._build_picker_menu()
        # Menu subscribes to ButtonDownEvent inside its __init__.
        # Removing that subscription from within the same handler
        # chain crashes some MicroPython eventbus builds - so cleanup
        # is deferred to the next update() tick via this flag.
        self._pending_menu_cleanup = False

    def _build_picker_menu(self):
        if not self._catalog:
            items = ["Exit"]
        elif len(self._catalog) == 1:
            entry = self._catalog[0]
            items = ["Install %s v%s" % (entry["name"], entry["version"]), "Cancel"]
        else:
            items = ["Install %s v%s" % (e["name"], e["version"]) for e in self._catalog]
            items.append("Install all")
            items.append("Cancel")
        return Menu(
            self,
            items,
            select_handler=self._on_picker_select,
            back_handler=self._on_picker_back,
        )

    def _on_picker_select(self, item, item_idx):
        # Guard against stale button events firing after we've left
        # the picker state (e.g. queued events processed during the
        # state transition).
        if self._state != _STATE_PICKER:
            return
        if not self._catalog:
            self._exit()
            return
        if len(self._catalog) == 1:
            if item_idx == 0:
                self._begin_install([self._catalog[0]])
            else:
                self._exit()
            return
        # Multi-app menu: N app rows + Install all + Cancel
        if item_idx < len(self._catalog):
            self._begin_install([self._catalog[item_idx]])
        elif item_idx == len(self._catalog):
            self._begin_install(list(self._catalog))
        else:
            self._exit()

    def _on_picker_back(self):
        if self._state != _STATE_PICKER:
            return
        self._exit()

    def _begin_install(self, targets):
        # Enumerate every file to be copied so background_update can
        # tick through them one at a time and draw() has a total for
        # the progress display.
        queue = []
        for entry in targets:
            src_root = entry["path"]
            dst_root = BADGE_APPS_DIR + "/" + entry["slug"]
            entry["_dst_root"] = dst_root
            for src, dst in fsutil.copytree_plan(src_root, dst_root):
                queue.append((src, dst, entry))

        self._install_targets = targets
        self._install_queue = queue
        self._install_cursor = 0
        self._install_current_name = targets[0]["name"] if targets else ""
        self._install_results = []
        self._error_message = None

        # Wipe existing installs before starting so a partial old copy
        # can't shadow the new one. Done up front (not per-tick) because
        # if this fails we want to bail before writing any new bytes.
        try:
            for entry in targets:
                fsutil.rmtree(entry["_dst_root"])
        except OSError as exc:
            self._error_message = "Wipe failed: " + str(exc)
            self._state = _STATE_DONE
            self._pending_menu_cleanup = True
            return

        # Defer the menu's eventbus.remove() to the next update() tick
        # - we're currently INSIDE the menu's own ButtonDownEvent
        # handler, and removing that handler from within its own
        # iteration crashes some MicroPython eventbus builds.
        self._pending_menu_cleanup = True
        self._state = _STATE_INSTALLING

    def background_update(self, delta):
        if self._state != _STATE_INSTALLING:
            return
        try:
            self._tick_install()
        except Exception as exc:
            # Anything unexpected during the copy loop or the finish
            # transition surfaces here rather than crashing the whole
            # app. Prints the type + message to the serial console so
            # we can diagnose later.
            print("[disk] install tick crashed: %s: %s"
                  % (type(exc).__name__, exc))
            self._error_message = "%s: %s" % (type(exc).__name__, exc)
            self._state = _STATE_DONE

    def _tick_install(self):
        if self._install_cursor >= len(self._install_queue):
            self._finish_install()
            return

        src, dst, entry = self._install_queue[self._install_cursor]
        self._install_current_name = entry["name"]
        try:
            fsutil.ensure_parent_dir(dst)
            fsutil.copyfile(src, dst)
        except OSError as exc:
            # File-level failure -> mark the app failed but keep going so
            # a batch install is atomic per-app, not per-file. A yanked
            # Flopagon surfaces here as OSError on read.
            self._mark_current_failed(entry, str(exc))
            self._skip_to_next_app()
            return

        self._install_cursor += 1

    def _mark_current_failed(self, entry, msg):
        for r in self._install_results:
            if r["slug"] == entry["slug"]:
                r["ok"] = False
                r["error"] = msg
                return
        self._install_results.append({
            "slug": entry["slug"],
            "name": entry["name"],
            "ok": False,
            "error": msg,
        })

    def _skip_to_next_app(self):
        # Advance cursor past every remaining file for the currently-
        # failing app so we start the next app cleanly.
        _, _, current = self._install_queue[self._install_cursor]
        while (self._install_cursor < len(self._install_queue)
               and self._install_queue[self._install_cursor][2] is current):
            self._install_cursor += 1

    def _finish_install(self):
        # Any app that didn't record a failure was fully copied.
        # Plain list membership test - MicroPython set builds are
        # inconsistent across firmware versions, and the result count
        # here is always tiny.
        failed_slugs = [r["slug"] for r in self._install_results]
        for entry in self._install_targets:
            if entry["slug"] in failed_slugs:
                continue
            self._install_results.append({
                "slug": entry["slug"],
                "name": entry["name"],
                "ok": True,
                "error": None,
            })
        # Tell the launcher to re-scan /apps.
        try:
            eventbus.emit(InstallNotificationEvent())
        except Exception as exc:
            print("[disk] launcher notify failed: %s" % exc)
        print("[disk] install complete: %d ok, %d failed"
              % (sum(1 for r in self._install_results if r["ok"]),
                 sum(1 for r in self._install_results if not r["ok"])))
        self._state = _STATE_DONE

    def _exit(self):
        eventbus.emit(RequestForegroundPopEvent(self))

    def deinit(self):
        # Called by HexpansionManagerApp when the Flopagon is pulled.
        # Mid-install: the copy loop's next tick will hit OSError on
        # read and gracefully record the failure. Nothing to release
        # here (SPI + mount are owned by the Phase 3 bootstrap).
        if self._menu is not None:
            try:
                self._menu._cleanup()
            except Exception:
                pass

    def draw(self, ctx):
        clear_background(ctx)
        if self._state == _STATE_PICKER:
            self._menu.draw(ctx)
            if not self._catalog:
                _draw_message(ctx, "No apps on this", "disk")
        elif self._state == _STATE_INSTALLING:
            self._draw_progress(ctx)
        elif self._state == _STATE_DONE:
            self._draw_done(ctx)

    def update(self, delta):
        if not self._foregrounded:
            eventbus.emit(RequestForegroundPushEvent(self))
            self._foregrounded = True
        # Deferred menu cleanup: safe here because we're no longer
        # inside the menu's own event handler.
        if self._pending_menu_cleanup and self._menu is not None:
            try:
                self._menu._cleanup()
            except Exception as exc:
                print("[disk] menu cleanup failed: %s" % exc)
            self._menu = None
            self._pending_menu_cleanup = False
        if self._state == _STATE_PICKER and self._menu is not None:
            self._menu.update(delta)
        elif self._state == _STATE_DONE:
            if self.button_states.get(BUTTON_TYPES["CONFIRM"]) \
                    or self.button_states.get(BUTTON_TYPES["CANCEL"]):
                self.button_states.clear()
                self._exit()
        return True

    def _draw_progress(self, ctx):
        total = len(self._install_queue)
        done = self._install_cursor
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 22
        ctx.move_to(0, -30).text("Installing")
        ctx.font_size = 16
        ctx.move_to(0, 0).text(self._install_current_name or "")
        ctx.font_size = 14
        ctx.move_to(0, 30).text("%d / %d files" % (done, total))
        # Slim horizontal progress bar
        if total > 0:
            bar_w = 160
            bar_h = 6
            filled = int(bar_w * done / total)
            ctx.rgb(0.25, 0.25, 0.25).rectangle(-bar_w // 2, 55, bar_w, bar_h).fill()
            ctx.rgb(0.2, 0.8, 0.4).rectangle(-bar_w // 2, 55, filled, bar_h).fill()

    def _draw_done(self, ctx):
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 20
        ctx.move_to(0, -80).text("Installed")
        y = -50
        for r in self._install_results:
            if r["ok"]:
                ctx.rgb(0.2, 0.8, 0.4)
                label = "OK  " + r["name"]
            else:
                ctx.rgb(0.9, 0.3, 0.3)
                label = "FAIL " + r["name"]
            ctx.font_size = 16
            ctx.move_to(0, y).text(label)
            y += 22
        if self._error_message is not None:
            ctx.rgb(0.9, 0.3, 0.3)
            ctx.font_size = 14
            ctx.move_to(0, y).text(self._error_message)
        ctx.rgb(0.75, 0.75, 0.75)
        ctx.font_size = 12
        ctx.move_to(0, 85).text("Reboot to see new apps")
        ctx.move_to(0, 100).text("C / F: exit")


def _enumerate_disk_apps():
    """Scan /disk/apps/*/disk.json into a menu-ready list."""
    entries = []
    try:
        names = os.listdir(DISK_APPS_DIR)
    except OSError:
        return entries
    names.sort()
    for name in names:
        folder = DISK_APPS_DIR + "/" + name
        if not fsutil.path_isdir(folder):
            continue
        m = manifest.read(folder + "/" + manifest.MANIFEST_NAME)
        e = manifest.display_entry(m, fallback_slug=name)
        e["path"] = folder
        entries.append(e)
    return entries


def _draw_message(ctx, line1, line2):
    ctx.rgb(1, 1, 1)
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE
    ctx.font_size = 18
    ctx.move_to(0, -10).text(line1)
    ctx.move_to(0, 15).text(line2)


__app_export__ = InstallerApp
