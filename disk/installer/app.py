"""Flopagon disk manager app.

Runs on the Tildagon after the EEPROM bootstrap mounts the 16 MB
flash and hands off to us. Four operations from one hub menu:

    Install app        - copy from disk to badge
    Backup app         - copy from badge to disk (+ manifest write)
    Delete from disk   - rmtree a disk-side app
    Delete from badge  - rmtree a badge-side app

Job progress runs on background_update() so draw() keeps rendering
one file at a time. Mount path is discovered from __file__ so the
Phase 3 bootstrap can pick any path (this file happens to sit at
/disk/installer/app.py in production).
"""

import os
import time

import app
from app_components import Menu, clear_background
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus
from system.launcher.events import InstallNotificationEvent
from system.scheduler.events import (
    RequestForegroundPushEvent,
    RequestForegroundPopEvent,
)

from . import _badge_apps
from . import _fsutil as fsutil
from . import _jobs
from . import _manifest as manifest


def _discover_disk_mount():
    # __file__ -> .../installer/app.py; disk root is the grandparent.
    try:
        installer_dir = __file__.rsplit("/", 1)[0]
        return installer_dir.rsplit("/", 1)[0]
    except (NameError, AttributeError):
        return "/disk"


DISK_MOUNT = _discover_disk_mount()
DISK_APPS_DIR = DISK_MOUNT + "/apps"
BADGE_APPS_DIR = "/apps"

_STATE_HUB = "hub"
_STATE_PICKER = "picker"
_STATE_CONFIRM = "confirm"
_STATE_WORKING = "working"
_STATE_DONE = "done"

# Operation kinds carried through picker + confirm + working states.
_KIND_INSTALL = "install"          # disk -> badge
_KIND_BACKUP = "backup"            # badge -> disk (+ manifest)
_KIND_DEL_DISK = "del_disk"        # rmtree on disk
_KIND_DEL_BADGE = "del_badge"      # rmtree on badge

_HUB_INSTALL = "Install app"
_HUB_BACKUP = "Backup app"
_HUB_DEL_DISK = "Delete from disk"
_HUB_DEL_BADGE = "Delete from badge"
_HUB_EXIT = "Exit"

_HUB_ITEMS = (_HUB_INSTALL, _HUB_BACKUP, _HUB_DEL_DISK, _HUB_DEL_BADGE,
              _HUB_EXIT)

_PICKER_BACK = "Back"


def _verb(kind):
    return {
        _KIND_INSTALL:   "Install",
        _KIND_BACKUP:    "Backup",
        _KIND_DEL_DISK:  "Delete",
        _KIND_DEL_BADGE: "Delete",
    }[kind]


def _source_apps(kind):
    """Which side lists apps for this operation."""
    if kind in (_KIND_INSTALL, _KIND_DEL_DISK):
        return _enumerate_disk_apps()
    return _badge_apps.list_installed(BADGE_APPS_DIR)


def _enumerate_disk_apps():
    """Scan <mount>/apps/*/disk.json into a menu-ready list."""
    entries = []
    try:
        names = sorted(os.listdir(DISK_APPS_DIR))
    except OSError:
        return entries
    for name in names:
        folder = DISK_APPS_DIR + "/" + name
        if not fsutil.path_isdir(folder):
            continue
        m = manifest.read(folder + "/" + manifest.MANIFEST_NAME)
        e = manifest.display_entry(m, fallback_slug=name)
        e["path"] = folder
        entries.append(e)
    return entries


class DiskManagerApp(app.App):
    def __init__(self, config=None):
        super().__init__()
        self.config = config
        self.button_states = Buttons(self)

        self._foregrounded = False
        self._menu = None
        # Deferred cleanup: Menu removes its ButtonDownEvent subscription
        # inside _cleanup(), and doing that from within a menu callback
        # (which is what happens on select) crashes some MicroPython
        # eventbus builds. Set a flag; update() drops the menu on the
        # next tick, safely outside the callback.
        self._pending_menu_cleanup = False

        # Operation state: what kind + what target + job progress.
        self._kind = None
        self._target = None      # dict: {slug, name, version, path}
        self._job = None
        self._result_message = None
        self._result_ok = False

        self._state = _STATE_HUB
        self._menu = self._build_hub_menu()

    # ---------------------------------------------------------------------
    # Menu builders
    # ---------------------------------------------------------------------

    def _build_hub_menu(self):
        return Menu(self, list(_HUB_ITEMS),
                    select_handler=self._on_hub_select,
                    back_handler=self._on_hub_back)

    def _build_picker_menu(self, apps):
        if not apps:
            items = [_PICKER_BACK]
        else:
            items = ["%s v%s" % (a["name"], a["version"]) for a in apps]
            items.append(_PICKER_BACK)
        return Menu(self, items,
                    select_handler=self._on_picker_select,
                    back_handler=self._on_picker_back)

    # ---------------------------------------------------------------------
    # Hub state
    # ---------------------------------------------------------------------

    def _on_hub_select(self, item, item_idx):
        if self._state != _STATE_HUB:
            return
        if item == _HUB_EXIT:
            self._exit_app()
            return
        kind = {
            _HUB_INSTALL:   _KIND_INSTALL,
            _HUB_BACKUP:    _KIND_BACKUP,
            _HUB_DEL_DISK:  _KIND_DEL_DISK,
            _HUB_DEL_BADGE: _KIND_DEL_BADGE,
        }.get(item)
        if kind is None:
            return
        self._enter_picker(kind)

    def _on_hub_back(self):
        if self._state == _STATE_HUB:
            self._exit_app()

    def _return_to_hub(self):
        # Called from any state's exit path. Menu cleanup deferred so
        # we don't rip out the subscription that might still be firing
        # this callback.
        self._pending_menu_cleanup = True
        self._state = _STATE_HUB
        self._kind = None
        self._target = None
        self._job = None
        self._result_message = None
        self._result_ok = False
        self.button_states.clear()

    # ---------------------------------------------------------------------
    # Picker state
    # ---------------------------------------------------------------------

    def _enter_picker(self, kind):
        self._kind = kind
        self._picker_apps = _source_apps(kind)
        self._pending_menu_cleanup = True
        self._state = _STATE_PICKER

    def _on_picker_select(self, item, item_idx):
        if self._state != _STATE_PICKER:
            return
        if item == _PICKER_BACK or item_idx >= len(self._picker_apps):
            self._return_to_hub()
            return
        self._target = self._picker_apps[item_idx]
        self._pending_menu_cleanup = True
        self._state = _STATE_CONFIRM

    def _on_picker_back(self):
        if self._state == _STATE_PICKER:
            self._return_to_hub()

    # ---------------------------------------------------------------------
    # Confirm + working states
    # ---------------------------------------------------------------------

    def _begin_job(self):
        # Set up the right job for the current kind + target, then
        # transition to WORKING.
        kind = self._kind
        target = self._target
        if kind == _KIND_INSTALL:
            self._job = _jobs.CopyJob(
                src_dir=target["path"],
                dst_dir=BADGE_APPS_DIR + "/" + target["slug"],
            )
        elif kind == _KIND_BACKUP:
            now_ms = time.ticks_ms() if hasattr(time, "ticks_ms") else 0
            self._job = _jobs.CopyJob(
                src_dir=target["path"],
                dst_dir=DISK_APPS_DIR + "/" + target["slug"],
                manifest_data=manifest.build(
                    name=target["name"],
                    slug=target["slug"],
                    version=target["version"],
                    file_count=0,   # will be visibly wrong in the manifest;
                                    # accepted for now, installer doesn't use it
                    copied_at=now_ms,
                ),
            )
        elif kind == _KIND_DEL_DISK:
            self._job = _jobs.DeleteJob(
                path=DISK_APPS_DIR + "/" + target["slug"],
            )
        elif kind == _KIND_DEL_BADGE:
            self._job = _jobs.DeleteJob(
                path=BADGE_APPS_DIR + "/" + target["slug"],
            )
        self._state = _STATE_WORKING

    def background_update(self, delta):
        if self._state != _STATE_WORKING or self._job is None:
            return
        try:
            self._job.tick()
        except Exception as exc:
            print("[disk] job tick crashed: %s: %s"
                  % (type(exc).__name__, exc))
            self._job.error = "%s: %s" % (type(exc).__name__, exc)
            self._job.done = True
        if self._job.done:
            self._finish_job()

    def _finish_job(self):
        job = self._job
        if job.error:
            self._result_ok = False
            self._result_message = job.error
        else:
            self._result_ok = True
            self._result_message = self._success_message()
        # Install / delete on the badge changes the launcher's app list;
        # ping it so the operator can see the change without a reboot.
        if self._kind in (_KIND_INSTALL, _KIND_DEL_BADGE) and self._result_ok:
            try:
                eventbus.emit(InstallNotificationEvent())
            except Exception as exc:
                print("[disk] launcher notify failed: %s" % exc)
        print("[disk] %s %s: %s"
              % (self._kind, self._target.get("slug"),
                 "OK" if self._result_ok else "FAIL"))
        self._state = _STATE_DONE

    def _success_message(self):
        target = self._target
        kind = self._kind
        if kind == _KIND_INSTALL:
            return "Installed %s" % target["name"]
        if kind == _KIND_BACKUP:
            return "Backed up %s" % target["name"]
        if kind == _KIND_DEL_DISK:
            return "Removed from disk"
        return "Removed from badge"

    # ---------------------------------------------------------------------
    # App-level lifecycle
    # ---------------------------------------------------------------------

    def _exit_app(self):
        eventbus.emit(RequestForegroundPopEvent(self))

    def deinit(self):
        # Fired by HexpansionManagerApp on Flopagon removal. Nothing
        # to release here (SPI + mount live on the bootstrap). Just
        # clean up the menu subscription so a subsequent instance
        # gets a clean event bus.
        if self._menu is not None:
            try:
                self._menu._cleanup()
            except Exception:
                pass

    # ---------------------------------------------------------------------
    # Scheduler surface
    # ---------------------------------------------------------------------

    def update(self, delta):
        if not self._foregrounded:
            eventbus.emit(RequestForegroundPushEvent(self))
            self._foregrounded = True

        if self._pending_menu_cleanup:
            self._flush_pending_state()

        if self._menu is not None:
            self._menu.update(delta)

        # Post-state input: WORKING has no menu, DONE has no menu.
        if self._state == _STATE_CONFIRM:
            self._handle_confirm_input()
        elif self._state == _STATE_DONE:
            self._handle_done_input()
        return True

    def _flush_pending_state(self):
        # Runs on the next update() tick after a state transition
        # from within a menu callback. Rebuilds the menu appropriate
        # for the new state.
        self._pending_menu_cleanup = False
        if self._state == _STATE_HUB:
            self._replace_menu(self._build_hub_menu())
        elif self._state == _STATE_PICKER:
            self._replace_menu(self._build_picker_menu(self._picker_apps))
        elif self._state in (_STATE_CONFIRM, _STATE_WORKING, _STATE_DONE):
            # These states don't use a Menu widget.
            self._replace_menu(None)

    def _replace_menu(self, new_menu):
        if self._menu is not None:
            try:
                self._menu._cleanup()
            except Exception:
                pass
        self._menu = new_menu

    def _handle_confirm_input(self):
        if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.button_states.clear()
            self._begin_job()
        elif self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._pending_menu_cleanup = True
            self._state = _STATE_PICKER   # back to same picker

    def _handle_done_input(self):
        if (self.button_states.get(BUTTON_TYPES["CONFIRM"])
                or self.button_states.get(BUTTON_TYPES["CANCEL"])):
            self.button_states.clear()
            self._return_to_hub()

    def draw(self, ctx):
        clear_background(ctx)
        if self._state in (_STATE_HUB, _STATE_PICKER) and self._menu is not None:
            self._menu.draw(ctx)
            if self._state == _STATE_PICKER and not self._picker_apps:
                _draw_lines(ctx, "No apps to", _kind_source_label(self._kind))
        elif self._state == _STATE_CONFIRM:
            self._draw_confirm(ctx)
        elif self._state == _STATE_WORKING:
            self._draw_progress(ctx)
        elif self._state == _STATE_DONE:
            self._draw_done(ctx)

    # ---------------------------------------------------------------------
    # Draw helpers
    # ---------------------------------------------------------------------

    def _draw_confirm(self, ctx):
        kind = self._kind
        target = self._target
        destructive = kind in (_KIND_DEL_DISK, _KIND_DEL_BADGE)
        _center_setup(ctx)
        ctx.rgb(1, 1, 1)
        ctx.font_size = 18
        ctx.move_to(0, -60).text("%s %s?" % (_verb(kind), target["name"]))
        ctx.font_size = 14
        ctx.move_to(0, -35).text("v%s" % target["version"])
        ctx.font_size = 12
        ctx.rgb(0.7, 0.7, 0.7)
        ctx.move_to(0, 20).text(_kind_from_to(kind))
        if destructive:
            ctx.rgb(0.9, 0.35, 0.35)
            ctx.font_size = 14
            ctx.move_to(0, 45).text("This deletes files")
        ctx.rgb(0.7, 0.7, 0.7)
        ctx.font_size = 12
        ctx.move_to(0, 75).text("C: confirm")
        ctx.move_to(0, 92).text("F: cancel")

    def _draw_progress(self, ctx):
        job = self._job
        _center_setup(ctx)
        ctx.rgb(1, 1, 1)
        ctx.font_size = 22
        label = "Deleting" if isinstance(job, _jobs.DeleteJob) else "Copying"
        ctx.move_to(0, -30).text(label)
        ctx.font_size = 14
        ctx.move_to(0, 0).text(self._target["name"] if self._target else "")
        if isinstance(job, _jobs.CopyJob) and job.total > 0:
            ctx.font_size = 14
            ctx.move_to(0, 25).text("%d / %d files" % (job.cursor, job.total))
            bar_w = 160
            bar_h = 6
            filled = int(bar_w * job.cursor / job.total)
            ctx.rgb(0.25, 0.25, 0.25).rectangle(-bar_w // 2, 50, bar_w, bar_h).fill()
            ctx.rgb(0.2, 0.8, 0.4).rectangle(-bar_w // 2, 50, filled, bar_h).fill()

    def _draw_done(self, ctx):
        _center_setup(ctx)
        if self._result_ok:
            ctx.rgb(0.2, 0.8, 0.4)
            ctx.font_size = 20
            ctx.move_to(0, -30).text("Done")
        else:
            ctx.rgb(0.9, 0.35, 0.35)
            ctx.font_size = 18
            ctx.move_to(0, -30).text("Failed")
        ctx.rgb(1, 1, 1)
        ctx.font_size = 14
        ctx.move_to(0, 5).text(self._result_message or "")
        ctx.rgb(0.7, 0.7, 0.7)
        ctx.font_size = 12
        ctx.move_to(0, 80).text("C / F: back")


def _kind_from_to(kind):
    return {
        _KIND_INSTALL:   "disk -> badge",
        _KIND_BACKUP:    "badge -> disk",
        _KIND_DEL_DISK:  "on disk",
        _KIND_DEL_BADGE: "on badge",
    }[kind]


def _kind_source_label(kind):
    return "backup" if kind in (_KIND_BACKUP, _KIND_DEL_BADGE) else "install"


def _center_setup(ctx):
    ctx.text_align = ctx.CENTER
    ctx.text_baseline = ctx.MIDDLE


def _draw_lines(ctx, line1, line2):
    _center_setup(ctx)
    ctx.rgb(1, 1, 1)
    ctx.font_size = 18
    ctx.move_to(0, -10).text(line1)
    ctx.move_to(0, 15).text(line2)


__app_export__ = DiskManagerApp
