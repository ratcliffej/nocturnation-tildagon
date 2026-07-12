"""NocturNation Tildagon receiver app.

Block 1 (shipped): minimal Tildagon OS app draws the brand-mark and
exits cleanly on CANCEL.

Block 2 (shipped): async background_task drives ESP-NOW receive with
channel auto-scan, deduplication, and hop-count enforcement per the
protocol manual.

Block 3 (shipped): each accepted LIGHT_PULSE is dispatched to the
PerimeterRenderer which arms per-LED envelopes; the update() tick
advances envelopes and pushes the resulting (r, g, b) per LED via
tildagonos.leds[i].

Block 4 (shipped): the LCD pulse renderer arms a full-screen colour
wash on each accepted dispatch; draw() paints it as the background
beneath the UI text. Calm Mode disables LCD pulsing entirely.

Block 5 (shipped): persistent settings (Calm Mode, group, channel) +
in-app menu via app_components.Menu. CONFIRM opens the menu; CANCEL
backs out. Class + group filter on inbound LIGHT_PULSE per protocol
manual section 4.2 and Epic 5 Q1 / Q2.

Block 6: NO SIGNAL indication after a 3 s frame gap (protocol
manual section 6.2); backgrounded operation per architecture spec
section 7.3 (perimeter LEDs continue, LCD reverts to foreground
app). Local DROP / BREAKDOWN synthetic fires from MUSIC_EVENT were
removed in the spec v0.29 protocol trim.

Reference: https://tildagon.badge.emfcamp.org/tildagon-apps/development/
Block plan: nocturnation-m5 docs/epics/epic-05-tildagon.md
"""

# The Tildagon launcher loads this module as ``apps.<dir>.app`` and does
# NOT add the app's own directory to sys.path, so this app's modules use
# relative imports (``from .nocturnation.X import Y`` below). The Show
# library is imported *absolutely* - by ``discover_shows()``
# (``__import__("shows")``) and by each Show's ``from nocturnation.X import
# Y`` - so the identical Show source runs unchanged under host pytest
# (where ``nocturnation`` / ``shows`` are importable via pyproject).
#
# Derive this app's own directory from the launcher module name and add it
# to sys.path so those absolute imports resolve on the badge too, whatever
# the install directory is called: ``/apps/nocturnation`` when deployed via
# deploy.sh, or ``/apps/nocturnation_tildagon`` when installed from the EMF
# app store (the store derives the dir from the repo name). app.py and its
# ``nocturnation`` / ``shows`` packages sit at the repo root so the store
# tarball has ``app.py`` at the top, as the installer requires.
import sys as _sys
try:
    _pkg = __name__.rsplit(".", 1)[0]  # e.g. "apps.nocturnation"
    _APP_DIR = "/" + _pkg.replace(".", "/") if "." in __name__ else "/apps/nocturnation"
    if _APP_DIR not in _sys.path:
        _sys.path.append(_APP_DIR)
except Exception as _exc:  # pragma: no cover - defensive only
    print("[nocturnation] could not extend sys.path: %s" % _exc)

import app
from events.input import Buttons, BUTTON_TYPES

# Optional imports: only available on the badge runtime. On the host
# (pytest), these modules don't exist; the async background_task simply
# logs and exits without doing radio work.
try:
    import asyncio
except ImportError:
    asyncio = None
try:
    import network
except ImportError:
    network = None
try:
    import espnow
except ImportError:
    espnow = None
try:
    import time
except ImportError:
    time = None
try:
    from tildagonos import tildagonos
except ImportError:
    tildagonos = None
try:
    from system.eventbus import eventbus
    from system.patterndisplay.events import PatternDisable, PatternEnable
except ImportError:
    eventbus = None
    PatternDisable = None
    PatternEnable = None
try:
    from system.scheduler.events import (
        RequestForegroundPushEvent,
        RequestForegroundPopEvent,
        RequestStopAppEvent,
    )
except ImportError:
    RequestForegroundPushEvent = None
    RequestForegroundPopEvent = None
    RequestStopAppEvent = None
try:
    from app_components import Menu, clear_background
except ImportError:
    Menu = None
    clear_background = None
# IMU is a badge built-in (Epic 6B Director tap-to-beat). Optional so
# host imports / non-IMU badges degrade gracefully.
try:
    import imu
except ImportError:
    imu = None
# Badge WiFi manager. We stop it before ESP-NOW so the radio stops
# sweeping channels (B9 root cause). Optional import for host / sim.
try:
    import wifi as _badge_wifi
except ImportError:
    _badge_wifi = None
# Standard library on both CPython and MicroPython; used by the dynamic
# repeater FSM to seed CANDIDATE/COOLDOWN X-frame counters.
import random

# Relative imports against the internal nocturnation/ package: the
# Tildagon launcher loads this module as apps.<dir>.app and does not add
# the app's own directory to sys.path, so a bare absolute `from
# nocturnation.X import Y` would fail on the badge. Relative imports
# resolve via the parent package (apps.<dir>) and work on both runtimes.
# Host-side pytest never imports this file (only the nocturnation/
# package below), so the dot prefix is invisible to the test suite.
from .nocturnation.channel_scan import ChannelScanner
from .nocturnation.clock import ticks_diff
from .nocturnation import images as bg_images
from .nocturnation.protocol import DedupRing, MessageType
from .nocturnation.protocol.frame import (
    make_light_wash_frame, make_light_wash_end_frame,
)
from .nocturnation.receive import parse_admittable
from .nocturnation.render import (
    LcdRenderer,
    LumeTextRenderer,
    PerimeterRenderer,
    CtxDisplay,
    PERIMETER_CLASSES,
    LCD_CLASSES,
)
from .nocturnation.settings import Settings
from .nocturnation.signal_tracker import SignalTracker
from .nocturnation.tofu import TofuLock, format_lock_label
# Epic 6B Director mode.
from .nocturnation.plugins import PropertyType
from .nocturnation.shows import discover_shows, show_registry, InputAction
from .nocturnation.director import (
    DirectorController,
    DirectorHost,
    RenderDispatcher,
    ImuAdapter,
    ButtonTapSource,
    DirectorButtonMapper,
    IMU_ADAPTER_CAPS,
    RESULT_OPEN_PICKER,
    RESULT_OPEN_SETTINGS,
)
from .nocturnation.director.espnow_sender import make_sender, BROADCAST_MAC
from .nocturnation.repeater import DynamicRepeater


# App version, loaded once at import from metadata.json (the on-badge
# runtime artefact copied alongside app.py by deploy.sh). Displayed on
# the Lume searching-for-signal screen. Read dynamically rather than
# baked in as a literal so the value tracks metadata.json without a
# separate constant to keep in sync.
#
# Fallback path list because __file__ semantics differ:
#   - Host pytest: __file__ absolute, rsplit gives project dir - reads
#     Nocturnation-Tildagon/metadata.json.
#   - Badge: deploy.sh copies metadata.json to /apps/nocturnation/,
#     alongside app.py. Depending on how the launcher spawns the app,
#     __file__ may or may not include the full path - try both.
#
# Falls back to "?" if every path fails; the caller renders "v?" so the
# failure surfaces on the screen rather than reading as a healthy value.
_APP_VERSION = "?"
try:
    import json as _json
    _version_paths = ["/apps/nocturnation/metadata.json", "metadata.json"]
    try:
        _version_paths.insert(0, __file__.rsplit("/", 1)[0] + "/metadata.json")
    except (NameError, AttributeError):
        pass
    for _p in _version_paths:
        try:
            with open(_p) as _vf:
                _APP_VERSION = _json.load(_vf).get("version", "?")
                break
        except OSError:
            continue
except (ValueError, ImportError):
    pass


# Cycle order for the settings menu.
_GROUP_CYCLE = (0, 1, 2, 3)
_CHANNEL_CYCLE = ("auto", "1", "11")

# B9 bench debug: when True, the receive path logs the *actual* radio
# channel (wlan.config('channel')) vs the app's belief, WiFi
# association state, and every raw frame with its parse + TOFU verdict.
# The gated helpers (_dbg_radio / _dbg_frame) stay for future bench
# work; flip this back on to re-enable the logging.
_DEBUG = False

# Director mode transmits on the hobby channel only (Epic 5.5: the
# Tildagon must not broadcast on the channel-11 Performance band).
# Channel 11 is reserved for commercial / show Directors with random-
# per-boot Performance-range source IDs (Epic 5.5 §3.4). A swarm of
# Tildagons at EMF transmitting on ch 11 would compete with the
# orchestrator's StickC Director and drown out cue traffic.
DIRECTOR_CHANNEL = 1

# Channels a Tildagon is FORBIDDEN to broadcast Director on. The
# constant above is the only authorised channel today, but this
# explicit blocklist gives us a hard runtime gate that catches any
# accidental change to DIRECTOR_CHANNEL (future bug, fork divergence,
# settings-file injection) before the radio is brought up.
DIRECTOR_FORBIDDEN_TX_CHANNELS = frozenset((11,))

# Director's source_id. A fixed community-range id (0x00-0x3F) is fine
# for the hobby channel; the Epic 5.5 random-per-boot allocation is a
# channel-11 concern and the Tildagon never transmits there.
DIRECTOR_SOURCE_ID = 0x20

# Signal-loss fallback wash (EMF prep). Same timings as the StickC
# LumeMode constants so the fleet converges on the same idle effect
# at the same moment:
#
#   3 s   silence -> NO SIGNAL diagnostic text (SignalTracker.is_lost)
#  10 s   silence -> synthesised LIGHT_WASH (blue<->purple ping-pong)
#                    with attack=FALLBACK_ATTACK_TICKS, intensity=
#                    FALLBACK_INTENSITY, cycle_ms=FALLBACK_CYCLE_PERIOD_MS
#  40 s   silence -> synthesised LIGHT_WASH_END (release_time=
#                    FALLBACK_FADE_TICKS in 100 ms units) starts the
#                    fade-to-black. release_time is u8 (~25.5 s cap)
#                    so the "30 s fade" the operator asked for is
#                    served by the maximum the wash state machine
#                    accepts - functionally equivalent to the eye.
# Any inbound frame cancels the fallback with a short-release END so
# the Director's returning wash/pulse traffic isn't fighting the
# synthetic baseline.
FALLBACK_ENTER_MS         = 10000
FALLBACK_FADE_START_MS    = 40000
FALLBACK_CYCLE_PERIOD_MS  = 10000
FALLBACK_ATTACK_TICKS     = 30    # 100 ms units = 3 s
FALLBACK_FADE_TICKS       = 255   # 100 ms units, ~25.5 s
FALLBACK_INTENSITY        = 60    # 0..255; ~24 % brightness
FALLBACK_RECOVERY_TICKS   = 5     # 100 ms units = 500 ms
FALLBACK_COLOUR_A         = (20, 0, 80)    # dark violet
FALLBACK_COLOUR_B         = (0, 20, 80)    # dark navy


class NocturNationApp(app.App):
    """Tildagon OS app entry point.

    The badge calls __init__ once at start-up, then drives update(delta)
    and draw(ctx) at ~20 Hz. background_task() is an async coroutine
    that runs for the lifetime of the app, including while backgrounded.
    """

    def __init__(self) -> None:
        self.button_states = Buttons(self)
        self._dedup = DedupRing()
        self._frame_count = 0
        self._last_frame = None
        # Epic 15 bench follow-up: diagnostic capture for the debug-overlay
        # LCD view. _last_frame_ms is the timestamp of the most recent
        # accepted frame of any type (drives the "Last: N.Ns" gap readout
        # + the background-colour signal-health band). _last_hop_count is
        # the hop_count field of the most recent accepted frame.
        # _frame_window is a ring of recent frame timestamps so the
        # overlay can show frames-per-10s (more useful than heartbeats-
        # only since real traffic = LIGHT_PULSE + LIGHT_WASH + heartbeats).
        # _hops_seen[h] flips True the first time a frame at hop=h
        # arrives. Indexed by hop_count (0..3). Drives the overlay's
        # "(1 2 3)" live meter - presence/absence per hop level,
        # no counts. Any non-zero entry at hop>=1 is cast-iron
        # evidence the relay path reached us during this session.
        # _last_heartbeat_ms kept for completeness but no longer shown.
        self._last_frame_ms     = 0
        self._last_heartbeat_ms = 0
        self._last_hop_count    = 0
        self._frame_window      = []   # list[int_ms], pruned to last 10 s
        self._heartbeat_window  = []   # list[int_ms], pruned to last 10 s
        self._hops_seen         = [False, False, False, False]   # by hop 0..3
        # Epic 17 B1: optional observer notified for every admitted frame
        # (both first-seen and dedup-duplicate). B2 wires an FSM instance
        # here when Repeat=AUTO. Signature: on_admitted_frame(frame,
        # is_duplicate, now_ms, raw_buf). raw_buf is the untouched ESP-NOW
        # payload so the FSM can relay by mutating hop_count (byte 5) and
        # calling esp.send() without re-encoding. now_ms/raw_buf may be
        # None on the host (no time module or in tests).
        self._repeater_observer = None
        # Epic 17 B2: DynamicRepeater FSM instance, created on Lume-mode
        # entry when settings.repeat == "AUTO" and torn down on exit.
        self._fsm = None
        self._status = "starting"
        self._esp = None
        # WiFi STA handle, stored so the receive loop can read back the
        # actual radio channel for B9 debug.
        self._wlan = None
        # Last channel for which wlan.config(channel=N) succeeded. None
        # if we never managed to set the radio channel at all. Shown on
        # the LCD so the operator knows which channel to align the
        # Director Stick to when auto-scan is disabled.
        self._receive_channel = None
        # Load persisted settings before constructing renderers so the
        # initial Calm Mode state matches what the operator last chose.
        self._settings = Settings.load()
        # Build the channel scanner from the persisted Channel setting:
        # "1" / "11" pin a single channel (no scan, no mis-lock); "auto"
        # cycles the full order. Constructed after settings load so the
        # pin takes effect.
        self._scanner = self._make_scanner()
        # Perimeter LED renderer. Calm Mode default per persisted
        # settings (default True). Architecture spec section 15.
        self._renderer = PerimeterRenderer(calm_mode=self._settings.calm_mode)
        # LCD pulse renderer. Calm Mode disables the LCD wash entirely
        # per architecture spec section 15.3.
        self._lcd_renderer = LcdRenderer(calm_mode=self._settings.calm_mode)
        # Epic 13: LCD text renderer. Layers TEXT_DISPLAY content (header
        # + body) on top of the wash background. Independent of the
        # pulse/wash state machine - operator-paced display content,
        # not music-paced visuals. Lifecycle is event-driven (no calm-
        # mode gate; lyric content is the point of declaring DisplayText).
        self._lume_text_renderer = LumeTextRenderer()
        # In-app settings menu state.
        self._settings_open = False
        self._settings_menu = None
        # Epic 6B: app role. "idle" (no radio, WiFi left up so the badge
        # stays connected while the operator is in the start menu),
        # "lume" (receive), or "director" (Show framework + IMU + TX).
        # Epic 12: launch straight into Lume - the background loop sees
        # the mode change on its first iteration and acquires the radio
        # (stopping WiFi). F (CANCEL) in Lume calls _stop_to_idle, which
        # restores WiFi and opens the idle menu as the route to Director
        # / settings / help.
        self._mode = "lume"
        # Idle start-menu (Menu component); built lazily on first idle
        # tick. None while a mode is running.
        self._idle_menu = None
        # Help screen (QR code) state. _help_matrix is the cached uQR
        # matrix; built once on open so QR generation doesn't run every
        # draw frame.
        self._help_open = False
        self._help_matrix = None
        # True while we hold the radio (WiFi stopped + ESP-NOW up). The
        # background loop acquires it on entering a mode and releases it
        # (restoring WiFi) on returning to idle.
        self._radio_held = False
        # Director runtime, built lazily on first Director-mode entry
        # (_ensure_director). None in Lume mode / before first entry.
        self._controller = None
        self._director_host = None
        self._dispatcher = None
        self._imu_adapter = None
        self._button_tap = None
        self._dir_buttons = DirectorButtonMapper()
        self._display = CtxDisplay()
        # Director-mode TX heartbeat pip: the send-wrapper set up in
        # _ensure_director bumps this to time.ticks_ms() on every
        # successful esp.send(); _draw_director paints a small pip in
        # the top-right whose colour is derived from the age of this
        # timestamp. Gives the operator a visual "the radio is actually
        # broadcasting" cue without hooking up USB serial. None = no
        # TX has landed yet this session.
        self._director_last_tx_ms = None
        # Director overlay (Show picker / per-Show settings). None when
        # the active Show owns the screen.
        self._director_overlay = None
        self._dir_settings_defs = []
        self._picker_show_ids = []
        # Block 6: Director-liveness tracker. Records every accepted
        # frame; the draw loop overlays NO SIGNAL when the gap exceeds
        # 3 s per protocol manual section 6.2.
        self._signal_tracker = SignalTracker()
        # Signal-loss fallback wash (EMF prep). After kFallbackEnterMs
        # of silence we synthesise a LIGHT_WASH locally (blue<->purple
        # cycle, muted intensity) and feed it to the renderers via the
        # same on_light_wash path real frames take; after a further
        # kFallbackFadeStartMs we emit a synthetic LIGHT_WASH_END to
        # fade to black. Any inbound frame cancels the fallback with
        # a short-release END. _fallback_last_check_ms suppresses
        # repeated emission while in the same state.
        self._fallback_active = False
        self._fallback_faded  = False
        # Epic 5.5 B6: Trust-On-First-Use lock on Director source_id.
        # Locks to the first valid frame from a non-broadcast source
        # after construction or clear(); subsequent frames from other
        # source_ids are dropped silently. On channel 11, only
        # Performance-range source_ids (0x40..0xFE) are eligible to
        # be locked. Lock expires after 10 s of inactivity.
        self._tofu = TofuLock()
        # Bring the perimeter LEDs out of low-power before the first
        # tick. Harmless if tildagonos is None (host environment).
        if tildagonos is not None:
            try:
                tildagonos.set_led_power(True)
            except Exception as exc:
                print("[nocturnation] tildagonos.set_led_power failed: %s" % exc)
        # Tell the badge's system patterndisplay service to stop driving
        # the perimeter LEDs while this app is running. Without this the
        # system pattern and our renderer.tick() both write the LED ring
        # and the result flickers. We re-enable on minimise so the badge
        # idle animation resumes once the operator backs out.
        self._patterns_inhibited = False
        self._inhibit_patterns()
        # Foreground state. The app starts foreground (the launcher push
        # that brought us here happens before __init__ in some firmware
        # versions). The scheduler events below keep this in sync.
        self._is_foreground = True
        # Subscribe to the scheduler's foreground push / pop events so
        # we can pause receive and release LED control while the app is
        # not in the foreground. The Tildagon launcher caches the app
        # instance, so __init__ runs only once - we cannot rely on it
        # for entry/exit lifecycle. The eventbus broadcasts these
        # events; we filter by event.app is self to ignore transitions
        # affecting other apps.
        if (
            eventbus is not None
            and RequestForegroundPushEvent is not None
            and RequestForegroundPopEvent is not None
        ):
            eventbus.on(RequestForegroundPushEvent, self._on_foreground_push, self)
            eventbus.on(RequestForegroundPopEvent, self._on_foreground_pop, self)

    def _on_foreground_push(self, event) -> None:
        """Scheduler is bringing this app to the foreground.

        Per Block 6 spec the perimeter ring continued animating in the
        background, so the renderer state is fresh - we keep it. The
        LCD renderer's envelope was being dispatched but not drawn;
        clear it so the wash starts cleanly with the next fire rather
        than mid-envelope. Re-inhibit patterns defensively in case
        another app emitted PatternEnable while we were away.
        """
        if event.app is not self:
            return
        self._is_foreground = True
        self._inhibit_patterns()
        self._lcd_renderer.clear()
        self._lume_text_renderer.clear()
        print("[nocturnation] foreground push - LCD wash resumed")

    def _on_foreground_pop(self, event) -> None:
        """Scheduler is taking this app out of the foreground.

        Per Block 6 spec the perimeter LEDs continue animating while
        the app is backgrounded (architecture spec section 7.3). We
        keep PatternDisable in effect (so the badge's patterndisplay
        service doesn't fight us for the LED ring) and let the
        receive + render loop keep running. The LCD goes idle
        automatically - the OS routes draw() calls to the new
        foreground app.
        """
        if event.app is not self:
            return
        self._is_foreground = False
        print("[nocturnation] foreground pop - LEDs continue in background")

    def _inhibit_patterns(self) -> None:
        if eventbus is None or PatternDisable is None:
            return
        try:
            eventbus.emit(PatternDisable())
            self._patterns_inhibited = True
        except Exception as exc:
            print("[nocturnation] PatternDisable emit failed: %s" % exc)

    def _resume_patterns(self) -> None:
        if eventbus is None or PatternEnable is None or not self._patterns_inhibited:
            return
        try:
            eventbus.emit(PatternEnable())
            self._patterns_inhibited = False
        except Exception as exc:
            print("[nocturnation] PatternEnable emit failed: %s" % exc)

    def update(self, delta: float) -> None:
        # Settings menu has its own update path - delegate to it.
        if self._settings_open and self._settings_menu is not None:
            self._settings_menu.update(delta)
            return

        # Help screen owns input: any of CONFIRM / CANCEL closes it.
        if self._help_open:
            if (self.button_states.get(BUTTON_TYPES["CANCEL"])
                    or self.button_states.get(BUTTON_TYPES["CONFIRM"])):
                self.button_states.clear()
                self._close_help()
            return

        # Idle: the start menu owns input. Lazily open it on the first
        # idle tick (Menu can't be built in __init__ before the app is
        # registered with the scheduler).
        if self._mode == "idle":
            if self._idle_menu is None and Menu is not None:
                self._open_idle_menu()
            if self._idle_menu is not None:
                self._idle_menu.update(delta)
            return

        # Director mode owns its own foreground input handling.
        if self._mode == "director":
            self._update_director(delta)
            return

        # Lume mode.
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            # F = stop the Lume session and return to the idle menu
            # (which restores WiFi). Switching to another badge app
            # instead keeps the Lume running in the background (Block 6).
            self._stop_to_idle()
            return
        if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.button_states.clear()
            self._open_settings()
            return
        # Perimeter LED ticking happens in the background_task receive
        # loop (Block 6) so it runs whether we're foregrounded or not.

    def _update_director(self, delta: float) -> None:
        """Foreground input for Director mode.

        Button map (Epic 6B B6b):
          CONFIRM (C) - manual tap (button-tap fallback); edge-detected
                        by the ButtonTapSource, so we read held state
                        without clearing.
          CANCEL  (F) - exit Director mode back to Lume.
          UP / DOWN / LEFT / RIGHT - nav -> InputAction (picker /
                        settings / cycle / cycle-prev), edge-detected by
                        the DirectorButtonMapper.

        IMU tap-to-beat is polled from background_task so it keeps
        working when the badge is backgrounded.
        """
        # An open overlay (picker / per-Show settings) owns input.
        if self._director_overlay is not None:
            self._director_overlay.update(delta)
            return
        if self._controller is None:
            return

        now = time.ticks_ms() if time is not None else 0

        # CONFIRM = manual tap. Read held state; ButtonTapSource finds
        # the rising edge. Deliberately NOT cleared (clear() would wipe
        # the held state the edge detector relies on).
        confirm = bool(self.button_states.get(BUTTON_TYPES["CONFIRM"]))
        self._controller.poll_button(confirm, now)

        # CANCEL = stop Director mode, back to the idle menu.
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._stop_to_idle()
            return

        # Nav buttons -> InputActions (mapper does its own edge + hold
        # detection, so no button_states.clear() that would disturb the
        # held CONFIRM tap state). now_ms feeds the long-press timer
        # for LEFT / RIGHT (short = palette, long = section).
        actions = self._dir_buttons.poll(
            up=bool(self.button_states.get(BUTTON_TYPES["UP"])),
            down=bool(self.button_states.get(BUTTON_TYPES["DOWN"])),
            left=bool(self.button_states.get(BUTTON_TYPES["LEFT"])),
            right=bool(self.button_states.get(BUTTON_TYPES["RIGHT"])),
            now_ms=time.ticks_ms() if time is not None else 0,
        )
        for action in actions:
            result = self._controller.on_input_action(action)
            if result == RESULT_OPEN_PICKER:
                self._open_picker()
                return
            if result == RESULT_OPEN_SETTINGS:
                self._open_director_settings()
                return

    # =====================================================================
    # Idle start menu + mode start/stop (Epic 6B B9)
    # =====================================================================

    def _idle_menu_items(self):
        return ["Lume Mode", "Director Mode", "Settings", "Help", "Quit"]

    def _open_idle_menu(self) -> None:
        if Menu is None or self._idle_menu is not None:
            return
        self._idle_menu = Menu(
            self,
            self._idle_menu_items(),
            select_handler=self._idle_menu_select,
            back_handler=self._idle_menu_back,
        )

    def _close_idle_menu(self) -> None:
        if self._idle_menu is not None:
            try:
                self._idle_menu._cleanup()
            except Exception:
                pass
        self._idle_menu = None
        self.button_states.clear()

    def _idle_menu_select(self, item, idx) -> None:
        if idx == 0:
            self._start_mode("lume")
        elif idx == 1:
            self._start_mode("director")
        elif idx == 2:
            self._open_settings()   # config submenu; returns to idle on Back
        elif idx == 3:
            self._open_help()
        elif idx == 4:
            self._quit()

    def _idle_menu_back(self) -> None:
        # Back from the top idle menu minimises to the launcher. WiFi
        # stays up (we never took the radio in idle).
        self.minimise()

    def _start_mode(self, mode) -> None:
        """Leave idle and start an active mode. The background loop sees
        the mode change and acquires the radio (stopping WiFi)."""
        self._close_idle_menu()
        self._mode = mode
        self._settings.mode = mode
        try:
            self._settings.save()
        except Exception as exc:
            print("[nocturnation] settings save failed: %s" % exc)
        print("[nocturnation] starting %s mode" % mode)

    def _stop_to_idle(self) -> None:
        """Stop the active mode and return to the idle menu. The
        background loop releases the radio + restores WiFi on its next
        idle iteration."""
        self._mode = "idle"
        self._renderer.clear()
        self._lcd_renderer.clear()
        self._lume_text_renderer.clear()
        self._fallback_active = False
        self._fallback_faded  = False
        self._dark_perimeter()
        self._open_idle_menu()
        print("[nocturnation] stopped to idle")

    # =====================================================================
    # Help screen - QR code to the project URL (Epic 6B B9)
    # =====================================================================

    def _build_qr(self, url):
        """Generate the QR matrix for `url` via the vendored uQR library.
        Returns a 2D bool matrix, or None on failure (lazy import so the
        ~1300-line module only loads when Help is opened)."""
        try:
            from .uQR import QRCode
            qr = QRCode()
            qr.add_data(url)
            return qr.get_matrix()
        except Exception as exc:
            print("[nocturnation] QR build failed: %s" % exc)
            return None

    def _open_help(self) -> None:
        self._close_idle_menu()
        self._help_open = True
        self._help_matrix = self._build_qr(self._settings.help_url)

    def _close_help(self) -> None:
        self._help_open = False
        self._help_matrix = None
        self.button_states.clear()
        if self._mode == "idle":
            self._open_idle_menu()

    def _restore_wifi(self) -> None:
        """Re-enable the OS WiFi connection we took down at startup.

        wifi.connect() is non-blocking - it reactivates the STA and kicks
        off association to the configured SSID without waiting."""
        if _badge_wifi is None:
            return
        try:
            _badge_wifi.connect()
            print("[nocturnation] WiFi restored to the OS")
        except Exception as exc:
            print("[nocturnation] wifi restore failed: %s" % exc)

    def _quit(self) -> None:
        """Cleanly leave the app and hand the radio back to the OS.

        We took the radio with wifi.stop() at startup, so on quit we
        release ESP-NOW, restore WiFi, return the LED ring to the badge's
        idle animation, then ask the scheduler to stop us. RequestStopApp
        cancels our background + update tasks and drops the launcher
        cache, so a relaunch starts fresh."""
        # Release ESP-NOW before WiFi reclaims the radio.
        try:
            if self._esp is not None:
                self._esp.active(False)
        except Exception as exc:
            print("[nocturnation] esp deactivate failed: %s" % exc)
        self._restore_wifi()
        # Hand the LED ring back to the badge's pattern service.
        self._resume_patterns()
        self._dark_perimeter()
        print("[nocturnation] quitting")
        if eventbus is not None and RequestStopAppEvent is not None:
            try:
                eventbus.emit(RequestStopAppEvent(self))
                return
            except Exception as exc:
                print("[nocturnation] stop-app emit failed: %s" % exc)
        # Fallback if the scheduler event is unavailable: just minimise.
        self.minimise()

    def _open_settings(self) -> None:
        """Open the in-app settings menu (Block 5).

        Darkens the perimeter ring and clears any in-flight envelopes
        so the LEDs don't hold a stale brightness while update() is
        delegated to the menu and _render_perimeter() stops ticking.
        """
        if Menu is None:
            print("[nocturnation] Menu component unavailable; cannot open settings")
            return
        # If we came from the idle menu, close it so it doesn't draw
        # underneath; _close_settings reopens it on Back when in idle.
        self._close_idle_menu()
        self._settings_open = True
        self._renderer.clear()
        self._lcd_renderer.clear()
        self._lume_text_renderer.clear()
        self._dark_perimeter()
        self._settings_menu = Menu(
            self,
            self._settings_menu_items(),
            select_handler=self._settings_select,
            back_handler=self._settings_back,
        )

    def _dark_perimeter(self) -> None:
        """Force every perimeter LED to (0, 0, 0) and commit immediately."""
        if tildagonos is None:
            return
        try:
            leds = tildagonos.leds
            for i in range(1, 13):
                leds[i] = (0, 0, 0)
            leds.write()
        except Exception as exc:
            print("[nocturnation] dark_perimeter failed: %s" % exc)

    def _close_settings(self) -> None:
        if self._settings_menu is not None:
            try:
                self._settings_menu._cleanup()
            except Exception:
                pass
        self._settings_open = False
        self._settings_menu = None
        # Drop any pending button press. The Menu component doesn't clear
        # button_states after invoking select_handler / back_handler, so
        # without this the next update() cycle would re-read the same
        # CONFIRM press as a fresh menu-open (or CANCEL as a minimise).
        self.button_states.clear()
        # If we opened Settings from the idle menu, return to it.
        if self._mode == "idle":
            self._open_idle_menu()

    def _settings_menu_items(self):
        """Compose the menu line labels from current settings values.

        Config only - mode selection (Lume/Director) and Quit live on
        the idle menu; this submenu is reachable both from idle and
        in-session (CONFIRM while a Lume runs)."""
        return [
            "Calm Mode: %s" % ("ON" if self._settings.calm_mode else "OFF"),
            "Group: %d" % self._settings.group,
            "Channel: %s" % self._settings.channel,
            "Debug: %s" % ("ON" if self._settings.debug_mode else "OFF"),
            "Repeat: %s" % self._settings.repeat,
            "Rescan",
            "Back",
        ]

    def _rebuild_settings_menu(self) -> None:
        """Rebuild the menu after a value cycle so the labels refresh."""
        if not self._settings_open or Menu is None:
            return
        try:
            self._settings_menu._cleanup()
        except Exception:
            pass
        self._settings_menu = Menu(
            self,
            self._settings_menu_items(),
            select_handler=self._settings_select,
            back_handler=self._settings_back,
        )

    def _settings_select(self, item, idx) -> None:
        """Menu select handler. Cycles the value of the selected line."""
        if idx == 0:
            self._settings.calm_mode = not self._settings.calm_mode
            self._apply_calm_mode()
        elif idx == 1:
            cur = self._settings.group
            try:
                pos = _GROUP_CYCLE.index(cur)
            except ValueError:
                pos = -1
            self._settings.group = _GROUP_CYCLE[(pos + 1) % len(_GROUP_CYCLE)]
        elif idx == 2:
            cur = self._settings.channel
            try:
                pos = _CHANNEL_CYCLE.index(cur)
            except ValueError:
                pos = -1
            self._settings.channel = _CHANNEL_CYCLE[(pos + 1) % len(_CHANNEL_CYCLE)]
        elif idx == 3:
            # Debug overlay toggle (Epic 15 bench follow-up). When ON,
            # the Lume LCD shows last-heartbeat freshness, hop count
            # of the most recent frame, heartbeats-per-10s window, and
            # the TOFU lock label - used to objectively measure
            # repeat-mode behaviour at range.
            self._settings.debug_mode = not self._settings.debug_mode
        elif idx == 4:
            # Repeat toggle (Epic 17). Cycles AUTO ↔ OFF. In AUTO the
            # badge silently becomes an audience repeater when no peer
            # covers its vicinity; OFF disables the FSM entirely. Takes
            # effect on the next Lume-mode session entry - a mid-session
            # toggle does not tear down an active FSM here (bench-test
            # for whether that matters).
            self._settings.repeat = "OFF" if self._settings.repeat == "AUTO" else "AUTO"
        elif idx == 5:
            # Rescan (Epic 5.5 B7). Clears the TOFU lock so the next
            # valid frame on the current channel establishes a fresh
            # lock. Note: the Tildagon's radio doesn't reliably support
            # channel re-scanning post-boot (CHANGELOG / Epic 5 Q6), so
            # this is a TOFU-only reset; the channel stays the same.
            self._tofu.clear()
            print("[nocturnation] TOFU lock cleared by operator")
            self._close_settings()
            return
        elif idx == 6:
            self._close_settings()
            return
        # Persist after every change. If the save fails we keep the
        # in-memory change so the UI is consistent; the next save
        # attempt (next change) tries again.
        try:
            self._settings.save()
        except Exception as exc:
            print("[nocturnation] settings save failed: %s" % exc)
        self._rebuild_settings_menu()

    def _settings_back(self) -> None:
        """Menu back handler - CANCEL while menu is open."""
        self._close_settings()

    def _apply_calm_mode(self) -> None:
        """Push the current Calm Mode setting to both renderers."""
        on = self._settings.calm_mode
        self._renderer.set_calm_mode(on)
        self._lcd_renderer.set_calm_mode(on)
        if not on:
            # Switching into Full mode - clear the LCD wash so it
            # starts from black at the next dispatch rather than
            # holding any stale envelope.
            self._lcd_renderer.clear()

    def _start_repeater(self) -> None:
        """Epic 17 B2: instantiate the dynamic-repeater FSM if Repeat=AUTO.

        Registers the ESP-NOW broadcast peer for TX. Wires the FSM to
        _repeater_observer so every admitted frame reaches its FSM.
        No-op when Repeat=OFF or the radio isn't up.
        """
        if self._settings.repeat != "AUTO":
            self._fsm = None
            self._repeater_observer = None
            return
        if self._esp is None or espnow is None:
            return
        try:
            self._esp.add_peer(BROADCAST_MAC)
        except OSError:
            # Peer already registered from a prior session; add_peer is
            # not otherwise idempotent on this MicroPython build.
            pass
        esp_ref = self._esp

        def _relay_send(payload):
            # esp_ref captured at start; if _esp is later cleared we
            # still send here - but the FSM is torn down at _stop_repeater
            # before the radio is released, so this closure only fires
            # while _esp is live.
            esp_ref.send(BROADCAST_MAC, payload)

        def _relay_random(lo, hi):
            return random.randint(lo, hi)

        now = time.ticks_ms() if time is not None else 0
        self._fsm = DynamicRepeater(_relay_send, _relay_random, now_ms=now)
        self._repeater_observer = self._fsm.on_admitted_frame
        print("[nocturnation] dynamic repeater armed (Repeat=AUTO)")

    def _stop_repeater(self) -> None:
        """Epic 17 B2: tear down the FSM. Idempotent - safe to call in
        any state, including when the FSM was never instantiated."""
        if self._fsm is not None:
            print("[nocturnation] dynamic repeater disarmed "
                  "(relayed=%d peer-seen=%d)" %
                  (self._fsm.relayed_count, self._fsm.peer_seen_count))
        self._fsm = None
        self._repeater_observer = None

    def _render_perimeter(self) -> None:
        """Advance perimeter LED envelopes and push to hardware.

        Called every UI frame (~20 Hz). The renderer.tick() callback fans
        out to tildagonos.leds[i] = (r, g, b); we then commit with a
        single tildagonos.leds.write() rather than after each set, so the
        ring updates atomically.
        """
        if tildagonos is None or time is None:
            return
        now_ms = time.ticks_ms()
        leds = tildagonos.leds
        def set_led(i, r, g, b):
            leds[i] = (r, g, b)
        try:
            self._renderer.tick(now_ms, set_led)
            leds.write()
        except Exception as exc:
            # Don't let a hardware glitch take down the app's update loop.
            print("[nocturnation] perimeter render failed: %s" % exc)

    def draw(self, ctx) -> None:
        # Settings menu owns the entire screen when it is open.
        if self._settings_open and self._settings_menu is not None:
            if clear_background is not None:
                clear_background(ctx)
            else:
                ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
            self._settings_menu.draw(ctx)
            return

        # Help screen (QR code) owns the screen when open.
        if self._help_open:
            self._draw_help(ctx)
            return

        # Idle: the start menu owns the screen.
        if self._mode == "idle":
            self._draw_idle(ctx)
            return

        # Director mode: the active Show owns the screen (or an overlay).
        if self._mode == "director":
            self._draw_director(ctx)
            return

        # Epic 15 bench follow-up. When the operator has toggled debug
        # ON in Settings, the Lume LCD is taken over by a diagnostic
        # readout: last-heartbeat freshness, current hop count, hb/10s,
        # lock label. Bypasses the normal background-image + wash +
        # text layers because diagnostic clarity beats aesthetics
        # during a range test. Re-toggle OFF to restore the regular
        # Lume LCD behaviour.
        if self._mode == "lume" and self._settings.debug_mode:
            self._draw_debug_overlay(ctx)
            return

        # Background: layered, with DirID-keyed image (Epic 13 Phase 2A)
        # as the lowest layer when available.
        #
        # The image layer is keyed off the current TOFU lock. As the
        # badge re-locks to a different Director, ``bg_images.load_for_dir_id``
        # caches the next image; an unknown DirID falls back to a
        # bundled default logo, and missing default means "no image,
        # paint the solid colour underneath as before".
        # Epic 13 Phase 2A: DirID-keyed background image (JPG) via
        # the documented ctx.image() API. The Tildagon Ctx reference
        # at https://tildagon.badge.emfcamp.org/tildagon-apps/reference/ctx/
        # documents image(path, x, y, w, h) as the supported path
        # for displaying JPG/PNG files; Ctx caches the decoded image
        # internally by path so we can call it every frame without
        # re-decoding.
        #
        # If no image is resolvable (no DirID-specific file, no
        # default.jpg) we fall through to the pre-Epic-13 solid
        # wash colour.
        bg_path = bg_images.path_for_dir_id(self._tofu.locked_id)
        if bg_path is not None:
            ctx.image(bg_path, -120, -120, 240, 240)
        else:
            # Pulse wash if Full mode is on and there's an active
            # envelope; otherwise black (Calm Mode keeps the LCD
            # quiet so the badge stays comfortable face-distance).
            bg_r, bg_g, bg_b = self._lcd_background_rgb01()
            ctx.rgb(bg_r, bg_g, bg_b).rectangle(-120, -120, 240, 240).fill()

        # Epic 13: once we've locked to a Director, the Lume LCD is
        # a content surface (mirrors the StickC's "LCD is content in
        # Lume" role per project memory). Suppress the diagnostic
        # HUD - brand mark / channel / frame count would be noise
        # against operator-paced text content, and the gaps between
        # explicit text cues (e.g. between lyric lines, or before the
        # @ShowSongInfo card fires) shouldn't surface the HUD either.
        # The wash background is the only visual during those gaps.
        # NO SIGNAL still surfaces as a small footer because radio
        # liveness matters even when a lyric is on the screen.
        # The HUD remains shown when scanning / unlocked so the
        # operator can confirm the badge is hunting for a Director.
        if self._tofu.is_locked():
            now_ms_local = time.ticks_ms() if time is not None else 0
            if self._lume_text_renderer.has_content():
                self._display.set_ctx(ctx)
                self._lume_text_renderer.paint(self._display, now_ms_local)
            if time is not None and self._signal_tracker.is_lost(now_ms_local):
                ctx.rgb(0.6, 0.1, 0.1)
                ctx.text_align = ctx.CENTER
                ctx.text_baseline = ctx.MIDDLE
                ctx.font_size = 12
                ctx.move_to(0, 95).text("NO SIGNAL")
            return

        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        ctx.font_size = 24
        ctx.move_to(0, -50).text("NocturNation")

        ctx.font_size = 12
        ctx.move_to(0, -20).text(self._status)
        # Channel + TOFU lock status. Composed by format_lock_label so
        # the Director and Lume use the same `C:nn` / `P:nn` convention.
        ctx.move_to(0, 0).text(
            format_lock_label(
                channel=self._scanner.current_channel,
                scanner_locked=self._scanner.is_locked,
                tofu_locked_id=self._tofu.locked_id,
            )
        )
        # Frame count + FSM state deliberately not drawn on the
        # searching / unlocked Lume screen (2026-07-12): punters see
        # a clean tagline + channel/lock status. Both stay available
        # on the operator Debug Overlay (_draw_debug_overlay) where
        # LISTEN / REPEAT / CDOWN belong.

        # NO SIGNAL overlay (Block 6) takes precedence over the RGB
        # triplet line: it answers the more important "is the Director
        # alive?" question. Shown in dimmed red so it doesn't compete
        # with the brand mark at the top.
        if time is not None and self._signal_tracker.is_lost(time.ticks_ms()):
            ctx.rgb(0.6, 0.1, 0.1)
            ctx.font_size = 18
            ctx.move_to(0, 50).text("NO SIGNAL")
            ctx.rgb(1, 1, 1)
            ctx.font_size = 12
        elif (
            self._last_frame is not None
            and self._last_frame.message_type == MessageType.LIGHT_PULSE
        ):
            f = self._last_frame
            ctx.move_to(0, 45).text("rgb %02x%02x%02x" % (f.r, f.g, f.b))

        # Version line, tucked just below the brand title so it reads
        # as "NocturNation vN.N.N" in a two-line stack. Value read once
        # at module import from metadata.json (see _APP_VERSION); tracks
        # whatever the on-badge manifest declares. Placed above the
        # tagline / channel stack so the round display can't clip it -
        # earlier position at y=105 sat on the edge of the visible arc.
        ctx.font_size = 12
        ctx.move_to(0, -33).text("v%s" % _APP_VERSION)

        # Button-hint footer. Tildagon convention: C = select (CONFIRM),
        # F = back (CANCEL). The mapping is fixed by the frontboard and
        # apps don't override it; printing the hint inline so the
        # operator doesn't have to remember which physical button does
        # what.
        ctx.font_size = 10
        ctx.move_to(0, 85).text("C: settings   F: exit")

    def _draw_debug_overlay(self, ctx) -> None:
        """Epic 15 bench follow-up: diagnostic readout for repeat-mode
        + range testing.

        Layout (top to bottom, large fonts for outdoor readability):
          1. Lock label - format_lock_label returns "ch N P:nn" so
             channel is encoded here (no separate channel line).
          2. Last frame age - "Last: 0.4s" prominent; this is the
             diagnostic the operator watches as they walk away from
             the Director.
          3. Frames per 10s - total traffic rate (LIGHT_PULSE + WASH
             + heartbeats). More useful than just heartbeats since
             real shows fire 4-8 LIGHT_PULSEs/sec at peak.
          4. Hop count + live meter of further hops also reaching
             us. "Hop: N (a b ...)" where N is the most recent
             admitted frame's hop, and (a b ...) lists each hop
             level GREATER than N that has been seen in this
             session. Examples:
                Hop: 0 ()      direct only - no relay observed
                Hop: 0 (1 2)   direct latest, but relay-and-relay
                               -of-relay frames have also arrived
                Hop: 1 (2)     latest was via one repeater, double-
                               relay frames are also reaching us
             Empty parens after a known-relaying repeater has been
             firing = relay TX isn't reaching us.
          5. Total frames received this session.
          6. Footer: how to exit.

        Background tints by signal health (Last-frame-age band):

          age < 1.2 s  -> green tint (healthy)
          1.2 - 2.0 s  -> amber tint (degraded)
          age > 2.0 s  -> red tint (lost or about to be)

        Text colour adjusts per band to keep contrast:
          green/red bg -> white text
          amber bg     -> black text (yellow + white is unreadable
                          in bright daylight at arm's length)
        """
        # Compute the health band from the last-frame age.
        if time is None or self._last_frame_ms == 0:
            band = "unknown"
            age_ms = -1
        else:
            age_ms = ticks_diff(time.ticks_ms(), self._last_frame_ms)
            if age_ms < 0:
                age_ms = 0
            if age_ms < 1200:
                band = "green"
            elif age_ms < 2000:
                band = "amber"
            else:
                band = "red"

        # Backgrounds tuned for outdoor visibility; text colour matches.
        if band == "green":
            bg_r, bg_g, bg_b = 0.0, 0.5, 0.0
            fg = (1.0, 1.0, 1.0)
            dim = (0.85, 1.0, 0.85)
        elif band == "amber":
            bg_r, bg_g, bg_b = 0.9, 0.6, 0.0
            fg = (0.0, 0.0, 0.0)
            dim = (0.2, 0.15, 0.0)
        elif band == "red":
            bg_r, bg_g, bg_b = 0.6, 0.0, 0.0
            fg = (1.0, 1.0, 1.0)
            dim = (1.0, 0.85, 0.85)
        else:
            bg_r, bg_g, bg_b = 0.0, 0.0, 0.0
            fg = (0.7, 0.7, 0.7)
            dim = (0.4, 0.4, 0.4)

        ctx.rgb(bg_r, bg_g, bg_b).rectangle(-120, -120, 240, 240).fill()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        # Lock label (includes "ch N" prefix).
        ctx.rgb(*fg)
        ctx.font_size = 20
        ctx.move_to(0, -85).text(
            format_lock_label(
                channel=self._scanner.current_channel,
                scanner_locked=self._scanner.is_locked,
                tofu_locked_id=self._tofu.locked_id,
            )
        )

        # Last frame age - the prominent diagnostic. "0.4s" format
        # (1/10 s precision is what the operator can perceive while
        # walking; ms is too noisy).
        ctx.font_size = 26
        if age_ms < 0:
            ctx.move_to(0, -45).text("Last: --")
        else:
            ctx.move_to(0, -45).text("Last: %d.%ds" % (age_ms // 1000,
                                                       (age_ms % 1000) // 100))

        # Frames per 10 s.
        ctx.font_size = 22
        fr_per_10s = len(self._frame_window)
        ctx.move_to(0, -10).text("Fr/10s: %d" % fr_per_10s)

        # Hop count + live meter of further relay hops also reaching
        # us. "Hop: N (a b ...)" where (a b ...) are the hop levels
        # GREATER than N that have been observed in this session.
        # Direct + multi-hop relay simultaneously visible reads as
        # "Hop: 0 (1 2)"; a corner-side Lume hearing only relays
        # reads as "Hop: 1 (2)" or similar. Empty parens after a
        # known-relaying repeater has been firing = relay TX isn't
        # reaching us.
        ctx.font_size = 18
        if self._last_frame is None:
            ctx.move_to(0, 25).text("Hop: --")
        else:
            higher_seen = " ".join(
                str(h) for h in (1, 2, 3)
                if h > self._last_hop_count and self._hops_seen[h]
            )
            ctx.move_to(0, 25).text(
                "Hop: %d (%s)" % (self._last_hop_count, higher_seen)
            )

        # Epic 17 B3: dynamic-repeater state, prominent state label
        # + relayed / peer-seen counters underneath. Absent when
        # Repeat=OFF (FSM not instantiated). Replaces the pre-Epic-17
        # "Tot: N" row - total-frames count wasn't materially useful
        # in the field vs the FSM state which is the primary
        # diagnostic during a bench walk-out.
        if self._fsm is not None:
            ctx.rgb(*fg)
            ctx.font_size = 20
            ctx.move_to(0, 55).text(self._fsm.state_label())
            ctx.rgb(*dim)
            ctx.font_size = 14
            ctx.move_to(0, 80).text(
                "tx:%d px:%d" % (self._fsm.relayed_count,
                                  self._fsm.peer_seen_count)
            )
        else:
            # Repeat=OFF: fall back to the pre-Epic-17 Tot: readout so
            # the space isn't blank on non-repeater devices.
            ctx.rgb(*dim)
            ctx.font_size = 18
            ctx.move_to(0, 60).text("Tot: %d" % self._frame_count)

        # Footer hint.
        ctx.rgb(*fg)
        ctx.font_size = 14
        ctx.move_to(0, 95).text("C: toggle off")

    def _draw_idle(self, ctx) -> None:
        """Idle screen: the start menu (Lume / Director / Settings /
        Quit). WiFi is up here; no radio session is running."""
        if clear_background is not None:
            clear_background(ctx)
        else:
            ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        if self._idle_menu is not None:
            self._idle_menu.draw(ctx)
        else:
            ctx.rgb(1, 1, 1)
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
            ctx.font_size = 22
            ctx.move_to(0, -10).text("NocturNation")
            ctx.font_size = 12
            ctx.move_to(0, 20).text("starting...")

    def _draw_help(self, ctx) -> None:
        """Render the cached QR matrix as black modules on white, centred
        in the round LCD, with the URL caption beneath. Pattern from the
        shkspr.mobi Tildagon QR article."""
        # QR codes need a light background; white the whole screen.
        ctx.rgb(1, 1, 1).rectangle(-120, -120, 240, 240).fill()
        m = self._help_matrix
        if not m:
            ctx.rgb(0, 0, 0)
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
            ctx.font_size = 16
            ctx.move_to(0, -10).text("Help unavailable")
            ctx.font_size = 10
            ctx.move_to(0, 15).text(self._settings.help_url)
            return
        qr_size = len(m)
        # The round 240 px screen inscribes a ~170 px square (240/sqrt2),
        # and a QR's corner finder patterns must stay on-screen to scan -
        # so floor the module size to keep the whole code within 170 px
        # (the blog's "+1" can overshoot for longer URLs and clip the
        # corners under the bezel).
        pixel_size = max(1, int(170 / qr_size))
        code_px = pixel_size * qr_size
        offset = -120 + (240 - code_px) / 2
        for row in range(qr_size):
            r = m[row]
            for col in range(qr_size):
                if r[col]:
                    ctx.rgb(0, 0, 0).rectangle(
                        (col * pixel_size) + offset,
                        (row * pixel_size) + offset,
                        pixel_size, pixel_size).fill()
        # URL caption just below the code's bottom edge (the QR carries
        # its own quiet-zone margin). Sitting it right under the code
        # keeps it where the round screen is still wide enough to show
        # the full text, rather than down at the narrow bottom chord.
        ctx.rgb(0, 0, 0)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 10
        ctx.move_to(0, (offset + code_px) + 8).text(self._settings.help_url)

    def _draw_director(self, ctx) -> None:
        """Director mode draw: an open overlay owns the screen; otherwise
        the active Show paints via on_render()."""
        # Overlay (picker / per-Show settings) owns the screen.
        if self._director_overlay is not None:
            if clear_background is not None:
                clear_background(ctx)
            else:
                ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
            self._director_overlay.draw(ctx)
            return

        show = self._controller.active_show if self._controller is not None else None
        if show is None:
            ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
            ctx.rgb(1, 1, 1)
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
            ctx.font_size = 18
            ctx.move_to(0, 0).text("No shows")
            return

        # Hand the live ctx to the Show's drawing surface, then let it
        # paint. A crashing Show must not take the UI down with it - this
        # is a system boundary (third-party Show code).
        self._display.set_ctx(ctx)
        try:
            show.on_render(self._controller.active_context)
        except Exception as exc:
            ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
            ctx.rgb(0.6, 0.1, 0.1)
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
            ctx.font_size = 14
            ctx.move_to(0, 0).text("show error")
            print("[nocturnation] show on_render failed: %s" % exc)

        # Director TX heartbeat pip. Drawn last so a Show that clears
        # its own background can't wipe it. Positioned top-right just
        # inside the round-display arc. Colour by age of the last
        # successful esp.send():
        #   < 200 ms  bright green   (a frame just went out)
        #   < 2000 ms dim green      (recent TX; healthy heartbeat cadence)
        #   >= 2000 ms or never      red (TX stalled - Q6, radio fault,
        #                                 or Show emits nothing)
        # 2 s outer threshold matches the 1 Hz heartbeat cadence
        # (dispatcher.heartbeat_tick) with margin.
        if time is not None:
            now_pip_ms = time.ticks_ms()
            if self._director_last_tx_ms is None:
                pip_r, pip_g, pip_b = 0.6, 0.0, 0.0
            else:
                age = ticks_diff(now_pip_ms, self._director_last_tx_ms)
                if age < 0:
                    age = 0
                if age < 200:
                    pip_r, pip_g, pip_b = 0.0, 1.0, 0.0
                elif age < 2000:
                    pip_r, pip_g, pip_b = 0.0, 0.4, 0.0
                else:
                    pip_r, pip_g, pip_b = 0.6, 0.0, 0.0
            ctx.rgb(pip_r, pip_g, pip_b)
            ctx.rectangle(95, -110, 8, 8).fill()

    async def background_task(self) -> None:
        """ESP-NOW receive loop: auto-scan, lock, then receive forever.

        Per protocol manual section 5.3 the Lume tries channel 11 first
        (suggested show channel) then channel 1 (hobby), each for ~2 s,
        repeating until a valid frame arrives. After lock, receive runs
        on the locked channel until the app is killed.

        Fallback: if the badge's networking layer rejects channel
        changes on STA_IF (observed: RuntimeError 0xffffffff), we skip
        auto-scan and listen on whichever channel the radio is already
        on. The operator must align Director + Tildagon channels manually
        in that case.
        """
        if espnow is None or network is None or asyncio is None:
            self._status = "no radio module"
            print("[nocturnation] required modules unavailable; receive disabled")
            return

        # Session loop (Epic 6B). In idle we hold no radio and leave WiFi
        # up (the operator is in the start menu / connected). On entering
        # a mode we take the radio (wifi.stop + ESP-NOW); on returning to
        # idle we release it and restore WiFi. So WiFi is only down while
        # a Lume / Director session is actively running.
        while True:
            if self._mode == "idle":
                self._release_radio()
                await asyncio.sleep_ms(100)
                continue
            self._acquire_radio()
            if self._wlan is None:
                # Acquire failed; drop back to idle rather than spin.
                self._mode = "idle"
                await asyncio.sleep_ms(200)
                continue
            if self._mode == "director":
                await self._director_session(self._wlan)
            else:
                await self._lume_session(self._wlan)
            await asyncio.sleep_ms(20)

    def _acquire_radio(self) -> None:
        """Take the radio for ESP-NOW: stop the badge WiFi manager (so the
        ESP32 firmware stops channel-sweeping for an AP - the B9 root
        cause), bring the STA up, and activate ESP-NOW. Idempotent while
        held. Resets the scanner + TOFU so each session starts fresh."""
        if self._radio_held:
            return
        if _badge_wifi is not None:
            try:
                _badge_wifi.stop()
                print("[nocturnation] WiFi stopped to free the radio for ESP-NOW")
            except Exception as exc:
                print("[nocturnation] wifi.stop() failed: %s" % exc)
        try:
            self._wlan = network.WLAN(network.STA_IF)
            self._wlan.active(True)
            # Epic 15: switch the PHY to ESP-NOW Long Range mode.
            # Halves the bitrate (500 kbps vs 1 Mbps) in exchange for
            # 2.5-7x open-air range. Fleet-wide commitment - LR-only
            # peers cannot decode standard 802.11b/g/n peers, so every
            # Director and Lume must enable this together.
            # Integer 8 = WIFI_PROTOCOL_LR in ESP-IDF v5.x. We pass it
            # as a raw int rather than network.WLAN.PROTOCOL_LR because
            # the badge's MicroPython (5114f2c-dirty, March 2024 base)
            # has the protocol setter but not the named LR constant;
            # the setter accepts any int and forwards to esp_wifi_set_
            # protocol() which knows the value. Bench-confirmed
            # 2026-06-27: wlan.config(protocol=8) succeeds and
            # wlan.config("protocol") reads back 8.
            try:
                self._wlan.config(protocol=8)
            except Exception as exc:
                print("[nocturnation] wlan.config(protocol=8/LR) failed: %s" % exc)
            # Disable Wi-Fi modem power-save. Per the MicroPython espnow
            # docs ("ESPNow and Wifi Operation"), the STA defaults to a
            # duty-cycled PM mode that sleeps through short ESP-NOW
            # bursts (the Director sends 2x retransmits within ~2 ms;
            # was 3x pre-Epic-15), so receivers must set PM_NONE for
            # reliable receive. This raises idle current; any future
            # light-sleep work must keep the radio awake for heartbeat
            # windows or this bug returns. Older firmware may not
            # expose `pm`; swallow the error so acquisition still
            # proceeds.
            try:
                self._wlan.config(pm=network.WLAN.PM_NONE)
            except Exception as exc:
                print("[nocturnation] wlan.config(pm=PM_NONE) failed: %s" % exc)
            if self._esp is None:
                self._esp = espnow.ESPNow()
            self._esp.active(True)
        except Exception as exc:
            print("[nocturnation] radio acquire failed: %s" % exc)
            self._wlan = None
            return
        # Fresh scan / lock / signal state for this session. Resetting
        # the signal tracker means a re-entered session starts in the
        # truthful NO SIGNAL state rather than carrying a stale
        # last-frame timestamp from before the previous _release_radio.
        self._scanner = self._make_scanner()
        self._tofu.clear()
        self._signal_tracker.reset()
        self._radio_held = True
        self._dbg_radio("acquire")

    def _bounce_radio(self) -> bool:
        """Cycle STA_IF active state so the next wlan.config(channel=N)
        call becomes a fresh first-config-after-active (Q6 workaround).

        Cheaper than _release_radio + _acquire_radio because it skips the
        badge_wifi restore/stop cycle and does NOT reset the scanner /
        TOFU / signal-tracker state - those must survive across an
        auto-scan's channel rotations. Long-range PHY + PM_NONE need
        to be re-applied because active(False) throws them away; the
        ESP-NOW peer table survives the wlan cycle in practice (bench-
        verified 2026-07-12) but we bounce esp.active around it for
        safety in case a future MicroPython build tightens the coupling.

        Returns True on success. False on hard exception - the caller
        should bail out of the scan rather than spin forever on a broken
        radio.
        """
        if self._wlan is None:
            return False
        try:
            if self._esp is not None:
                try:
                    self._esp.active(False)
                except Exception:
                    pass
            self._wlan.active(False)
            self._wlan.active(True)
            try:
                self._wlan.config(protocol=8)   # ESP-NOW long range
            except Exception:
                pass
            try:
                self._wlan.config(pm=network.WLAN.PM_NONE)
            except Exception:
                pass
            if self._esp is not None:
                self._esp.active(True)
            return True
        except Exception as exc:
            print("[nocturnation] radio bounce failed: %s" % exc)
            return False

    def _release_radio(self) -> None:
        """Release ESP-NOW and hand WiFi back to the OS. Idempotent."""
        if not self._radio_held:
            return
        try:
            if self._esp is not None:
                self._esp.active(False)
        except Exception as exc:
            print("[nocturnation] esp deactivate failed: %s" % exc)
        self._restore_wifi()
        self._radio_held = False
        self._status = "idle"
        print("[nocturnation] radio released; WiFi restored")

    def _make_scanner(self):
        """Build the ChannelScanner from the persisted Channel setting.

        "1" / "11" pin a single channel - no scan cycling, so the radio
        stays put and can't mis-lock onto a neighbour that bleeds a
        stray frame in (the B9 channel-6 bug). "auto" uses the full
        11 -> 1 -> 6 scan order.
        """
        ch = self._settings.channel
        if ch == "1":
            return ChannelScanner(order=(1,))
        if ch == "11":
            return ChannelScanner(order=(11,))
        return ChannelScanner()  # "auto" -> default SCAN_ORDER (11, 1, 6)

    async def _lume_session(self, wlan) -> None:
        """Receive session. A pinned channel ("1"/"11") sets the radio
        once and locks immediately; "auto" runs the scan state machine.
        Returns when the mode leaves 'lume'."""
        pinned = self._settings.channel in ("1", "11")
        if pinned:
            ch = int(self._settings.channel)
            try:
                wlan.config(channel=ch)
            except Exception as exc:
                print("[nocturnation] pin channel %d failed: %s" % (ch, exc))
            self._receive_channel = ch
            self._scanner.lock(ch)
            self._status = "ch %d" % ch
            print("[nocturnation] channel pinned to %d (no auto-scan)" % ch)
            self._dbg_radio("pin-%d" % ch)
        elif not await self._scan_until_locked(wlan):
            # Auto-scan bailed because channel-set failed mid-scan. The
            # radio is on whichever channel was last successfully set,
            # or on the platform default if none succeeded.
            if self._receive_channel is not None:
                self._status = "ch %d (no-scan)" % self._receive_channel
                print(
                    "[nocturnation] auto-scan unavailable; listening on channel %d "
                    "(align Director Stick to this channel)" % self._receive_channel
                )
            else:
                self._status = "no-scan"
                print("[nocturnation] auto-scan unavailable; listening on default channel")

        # Epic 17 B2: instantiate the dynamic-repeater FSM if Repeat=AUTO.
        # Register the ESP-NOW broadcast peer once so the FSM's send_fn
        # can TX relay frames. add_peer is idempotent-with-OSError; we
        # swallow the error the same way the Director-side make_sender
        # does.
        self._start_repeater()

        await self._receive_loop()

        # Tear down the FSM on Lume-session exit so a subsequent Director
        # session doesn't inherit stale state and the observer hook goes
        # back to None (no-op path).
        self._stop_repeater()

    async def _director_session(self, wlan) -> None:
        """Director transmit session: build the runtime, claim the
        hobby channel, then poll the IMU + tick the active Show +
        render the perimeter until the mode changes.

        Note (bench): this is exercised end-to-end only on hardware
        (Epic 6B B9). The orchestration it drives - DirectorController,
        RenderDispatcher, ImuAdapter - is host-tested.
        """
        self._ensure_director()
        if self._controller is None or not self._controller.show_ids():
            print("[nocturnation] no Director shows available; back to idle")
            self._stop_to_idle()
            return

        # Hard guard: a Tildagon must never broadcast as Director on a
        # forbidden channel (channel 11 is the commercial / show band,
        # reserved for Performance-range Directors with random-per-boot
        # source IDs - a Tildagon swarm transmitting there at EMF would
        # compete with the orchestrator's StickC Director). Today
        # DIRECTOR_CHANNEL is hardcoded to 1 so this gate is belt-and-
        # braces; if the constant ever drifts to 11 (settings injection,
        # fork divergence, future bug), we refuse to acquire the radio
        # and return to idle. Logged so it's visible at the console.
        if DIRECTOR_CHANNEL in DIRECTOR_FORBIDDEN_TX_CHANNELS:
            print(
                "[nocturnation] director TX refused on forbidden channel %d "
                "(reserved for commercial Directors); back to idle"
                % DIRECTOR_CHANNEL
            )
            self._status = "ch %d off-limits" % DIRECTOR_CHANNEL
            self._stop_to_idle()
            return

        # Q6 workaround: the badge's STA_IF layer only honours the FIRST
        # wlan.config(channel=N) call after each active(True) - subsequent
        # channel-sets raise RuntimeError 0xffffffff. If we arrived here
        # via a prior Lume session, that first-config slot was already
        # spent on the auto-scan's channel 11, so the config(channel=1)
        # below would silently fail: the try/except catches the exception,
        # the log line prints, and the Director then broadcasts on ch 11
        # (invisible to Lumes: StickC's tofu_lock filters community-range
        # source_ids on ch 11, Tildagon's DIRECTOR_FORBIDDEN check already
        # rules out ch 11 as a design channel). Release and re-acquire the
        # radio so ch 1 becomes the fresh first-config after active(True).
        # Idempotent + fast on a Director-first flow.
        self._release_radio()
        self._acquire_radio()
        if self._wlan is None:
            print("[nocturnation] director: radio re-acquire failed; back to idle")
            self._status = "no radio"
            self._stop_to_idle()
            return
        wlan = self._wlan   # rebind: _release + _acquire rebuilt the STA_IF

        # Director transmits on the hobby channel only (Epic 5.5).
        try:
            wlan.config(channel=DIRECTOR_CHANNEL)
            self._receive_channel = DIRECTOR_CHANNEL
        except Exception as exc:
            print("[nocturnation] director wlan.config(channel=%d) failed: %s"
                  % (DIRECTOR_CHANNEL, exc))
            self._status = "ch %d config failed" % DIRECTOR_CHANNEL
            self._stop_to_idle()
            return

        # Readback verify: if the actual radio channel doesn't match
        # DIRECTOR_CHANNEL, refuse to broadcast rather than silently TX
        # on the wrong channel. Best-effort - some MicroPython builds
        # don't support config("channel") with a positional arg; a
        # readback exception is logged but doesn't block the session.
        try:
            actual_ch = wlan.config("channel")
            if actual_ch != DIRECTOR_CHANNEL:
                print(
                    "[nocturnation] director channel verify failed: wanted %d, "
                    "actual %d; back to idle" % (DIRECTOR_CHANNEL, actual_ch)
                )
                self._status = "ch %d != %d" % (actual_ch, DIRECTOR_CHANNEL)
                self._stop_to_idle()
                return
        except Exception as exc:
            print("[nocturnation] director channel readback unsupported: %s" % exc)

        # Reset the heartbeat pip so a stale timestamp from a prior
        # Director session doesn't paint green before the first TX.
        self._director_last_tx_ms = None

        self._controller.enter()
        self._bind_display()
        self._status = "director"

        poll_ms = 5
        imu_interval_ms = 20     # ~50 Hz IMU poll - matches how the
                                 # tap/motion detector was tuned (a
                                 # faster poll lets the gravity EMA
                                 # erode tap transients).
        render_interval_ms = 50  # ~20 Hz perimeter tick
        now0 = time.ticks_ms() if time is not None else 0
        last_imu = now0
        last_render = now0
        while self._mode == "director":
            now = time.ticks_ms() if time is not None else 0
            # Beacon a 1 Hz HEARTBEAT (skip-if-recent) so Lumes can
            # discover the channel and keep their TOFU lock between
            # taps. Runs even while an overlay is open - the Director
            # stays discoverable while the operator navigates menus.
            if time is not None:
                self._dispatcher.heartbeat_tick(now)
            # When an overlay (picker / per-Show settings) is open it
            # owns the screen and input; the Show pauses.
            if self._director_overlay is None:
                # IMU tap-to-beat at ~50 Hz (button tap is polled in
                # update()).
                if time is not None and ticks_diff(now, last_imu) >= imu_interval_ms:
                    self._controller.poll_inputs(now, button_pressed=None)
                    last_imu = now
                self._controller.tick(now)
                if time is not None and ticks_diff(now, last_render) >= render_interval_ms:
                    self._render_perimeter()
                    last_render = now
            await asyncio.sleep_ms(poll_ms)

        self._controller.exit()

    # =====================================================================
    # Director runtime + overlays (Epic 6B B6b)
    # =====================================================================

    def _ensure_director(self) -> None:
        """Build the Director runtime once: discover Shows, wire the
        render dispatcher (broadcast + local loopback), the IMU + button
        input adapters, and the controller. Idempotent."""
        if self._controller is not None:
            return
        try:
            discover_shows()
        except Exception as exc:
            print("[nocturnation] show discovery failed: %s" % exc)
        registry = show_registry()

        send_fn = None
        if self._esp is not None:
            try:
                raw_send_fn = make_sender(self._esp)
                # Wrap the sender so we can stamp _director_last_tx_ms
                # on every successful send. The stamp drives the LCD
                # heartbeat pip in _draw_director. Failure to send
                # doesn't advance the stamp - pip stays cold if the
                # radio's silently failing.
                def _pip_send(payload):
                    raw_send_fn(payload)
                    if time is not None:
                        self._director_last_tx_ms = time.ticks_ms()
                send_fn = _pip_send
            except Exception as exc:
                print("[nocturnation] espnow sender setup failed: %s" % exc)
        # The Director is its own first Lume: render_fx broadcasts AND
        # loops back to the local perimeter + LCD renderers.
        self._dispatcher = RenderDispatcher(
            send_fn=send_fn,
            perimeter=self._renderer,
            lcd=self._lcd_renderer,
            source_id=DIRECTOR_SOURCE_ID,
            redundancy=3,  # ESP-NOW is lossy; match the M5 master's 3x TX
        )
        clock = time.ticks_ms if time is not None else (lambda: 0)
        self._director_host = DirectorHost(
            self._dispatcher, clock=clock, imu_caps=IMU_ADAPTER_CAPS
        )

        if imu is not None:
            self._imu_adapter = ImuAdapter(acc_read_fn=imu.acc_read)
        else:
            self._imu_adapter = None
            print("[nocturnation] no IMU module; tap-to-beat via button C only")
        self._button_tap = ButtonTapSource()

        self._controller = DirectorController(
            self._director_host,
            registry,
            imu=self._imu_adapter,
            button_tap=self._button_tap,
            initial_show_id=self._settings.active_show,
            on_active_show_changed=self._persist_active_show,
        )

    def _persist_active_show(self, show_id) -> None:
        self._settings.active_show = show_id
        try:
            self._settings.save()
        except Exception as exc:
            print("[nocturnation] settings save failed: %s" % exc)

    def _bind_display(self) -> None:
        """Point the active Show's ShowContext at the shared CtxDisplay
        so on_render() can draw. Called after entry and after a Show
        change."""
        if self._controller is None:
            return
        ctx = self._controller.active_context
        if ctx is not None:
            ctx.set_display(self._display)

    # -- Show picker overlay ----------------------------------------------

    def _open_picker(self) -> None:
        if Menu is None or self._controller is None:
            return
        self._picker_show_ids = self._controller.show_ids()
        items = []
        for sid in self._picker_show_ids:
            show = self._controller.registry.find(sid)
            items.append(show.display_name() if show is not None else sid)
        items.append("Back")
        self._renderer.clear()
        self._dark_perimeter()
        self._director_overlay = Menu(
            self,
            items,
            select_handler=self._picker_select,
            back_handler=self._close_overlay,
        )

    def _picker_select(self, item, idx) -> None:
        if idx < len(self._picker_show_ids):
            self._controller.select_show(self._picker_show_ids[idx])
            self._bind_display()
        self._close_overlay()

    # -- Per-Show settings overlay ----------------------------------------

    def _open_director_settings(self) -> None:
        if Menu is None or self._controller is None:
            return
        show = self._controller.active_show
        if show is None:
            return
        self._dir_settings_defs = list(show.properties())
        if not self._dir_settings_defs:
            print("[nocturnation] active show has no settings")
            return
        self._renderer.clear()
        self._dark_perimeter()
        self._build_dir_settings_menu()

    def _build_dir_settings_menu(self) -> None:
        if Menu is None:
            return
        if self._director_overlay is not None:
            try:
                self._director_overlay._cleanup()
            except Exception:
                pass
        self._director_overlay = Menu(
            self,
            self._dir_settings_items(),
            select_handler=self._dir_settings_select,
            back_handler=self._close_dir_settings,
        )

    def _dir_settings_items(self):
        ctx = self._controller.active_context
        items = []
        for pd in self._dir_settings_defs:
            val = ctx.get_property(pd.key)
            items.append("%s: %s" % (pd.display_name, self._format_prop(pd, val)))
        items.append("Back")
        return items

    def _dir_settings_select(self, item, idx) -> None:
        if idx >= len(self._dir_settings_defs):
            self._close_dir_settings()
            return
        pd = self._dir_settings_defs[idx]
        ctx = self._controller.active_context
        cur = ctx.get_property(pd.key)
        # set_property clamps + persists + notifies the Show.
        ctx.set_property(pd.key, self._cycle_prop(pd, cur))
        self._build_dir_settings_menu()  # refresh labels

    def _close_dir_settings(self) -> None:
        # A sensitivity edit only takes effect once re-pushed to the IMU
        # adapter; do it on close.
        if self._controller is not None:
            self._controller.apply_sensitivity()
        self._close_overlay()

    @staticmethod
    def _format_prop(pd, val):
        if pd.type == PropertyType.BOOL:
            return "ON" if val else "OFF"
        if pd.type == PropertyType.ENUM and pd.enum_names:
            if 0 <= val < len(pd.enum_names):
                return pd.enum_names[val]
        if pd.type == PropertyType.COLOUR:
            return "#%06X" % (val & 0xFFFFFF)
        return str(val)

    @staticmethod
    def _cycle_prop(pd, cur):
        t = pd.type
        if t == PropertyType.BOOL:
            return not cur
        if t == PropertyType.ENUM:
            lo = pd.min_value if pd.min_value is not None else 0
            if pd.enum_names:
                hi = len(pd.enum_names) - 1
            else:
                hi = pd.max_value if pd.max_value is not None else 255
            return lo if cur >= hi else cur + 1
        if t in (PropertyType.U8, PropertyType.U16):
            lo = pd.min_value if pd.min_value is not None else 0
            hi = pd.max_value if pd.max_value is not None else (
                255 if t == PropertyType.U8 else 65535
            )
            span = hi - lo
            step = span // 8 if span >= 8 else 1
            nxt = cur + step
            return lo if nxt > hi else nxt
        # COLOUR isn't cyclable from a single-button menu; leave as-is.
        return cur

    def _close_overlay(self) -> None:
        if self._director_overlay is not None:
            try:
                self._director_overlay._cleanup()
            except Exception:
                pass
        self._director_overlay = None
        self.button_states.clear()
        # Drop any edge state so a button still held when the overlay
        # closes doesn't immediately re-trigger.
        self._dir_buttons.reset()

    def _dbg_radio(self, where) -> None:
        """Log the actual radio channel + WiFi association vs the app's
        belief. The crux of the B9 channel mystery: if wlan reports a
        different channel than the scanner thinks, the channel-set is
        being silently overridden (WiFi association pins it)."""
        if not _DEBUG or self._wlan is None:
            return
        try:
            actual = self._wlan.config("channel")
        except Exception as exc:
            actual = "ERR(%s)" % exc
        try:
            conn = self._wlan.isconnected()
        except Exception:
            conn = "?"
        essid = ""
        try:
            if conn:
                essid = self._wlan.config("essid")
        except Exception:
            pass
        print("[dbg %s] radio_ch=%s wifi_conn=%s essid=%r | scanner_ch=%s recv_ch=%s tofu=%s"
              % (where, actual, conn, essid,
                 self._scanner.current_channel, self._receive_channel,
                 self._tofu.locked_id))

    def _dbg_frame(self, where, buf, frame, admitted) -> None:
        """Log one received frame: raw head, magic check, parse + admit."""
        if not _DEBUG:
            return
        n = len(buf)
        head = bytes(buf[: min(n, 12)]).hex()
        magic_ok = (n >= 3 and buf[0] == 0x4E and buf[1] == 0x4E)
        if frame is None:
            print("[dbg %s RX] len=%d magic=%s hex=%s -> DROPPED (parse-fail or dedup)"
                  % (where, n, magic_ok, head))
        else:
            print("[dbg %s RX] len=%d type=0x%02X src=0x%02X seq=%d -> admit=%s (recv_ch=%s tofu=%s)"
                  % (where, n, frame.message_type, frame.source_id,
                     frame.sequence_number, admitted, self._receive_channel,
                     self._tofu.locked_id))

    async def _scan_until_locked(self, wlan) -> bool:
        """Run the auto-scan state machine.

        Returns True if a channel was locked normally (a valid frame
        arrived). Returns False if channel-set is rejected by the
        platform - the caller should then drop into receive without
        having locked a specific channel.
        """
        listen_ms = self._scanner.listen_ms
        poll_ms = 50
        # Tagline shown on the searching / unlocked Lume screen. Users
        # who launched the app straight into Lume mode without a
        # Director in earshot see this until a frame arrives.
        self._status = "Open-source crowd lighting"

        # Q6 workaround: the badge's STA_IF only honours the FIRST
        # wlan.config(channel=N) call after each wlan.active(True), so
        # subsequent scan targets in a single session used to fail with
        # RuntimeError 0xffffffff and the scanner froze on the first
        # channel (usually 11). Bouncing STA_IF active(False)/active(True)
        # around every non-first channel-set resets that counter, letting
        # the SCAN_ORDER (11 -> 1 -> 6) actually rotate. Skip the bounce
        # on the first iteration because _acquire_radio just brought the
        # STA up - the first config call is already a fresh
        # first-config-after-active.
        first_iter = True
        while not self._scanner.is_locked and self._mode == "lume":
            ch = self._scanner.current_channel
            if not first_iter:
                if not self._bounce_radio():
                    self._status = "radio bounce err ch %d" % ch
                    print("[nocturnation] scan radio bounce failed at ch %d" % ch)
                    return False
            first_iter = False
            try:
                wlan.config(channel=ch)
            except Exception as exc:
                # STA_IF still rejects the config even after a bounce -
                # something deeper is wrong. Fall back to receive on
                # whichever channel was last successfully set (often
                # the first scan target). This preserves the pre-Q6-
                # workaround behaviour as the ultimate fallback.
                self._status = "ch %d err" % ch
                print("[nocturnation] wlan.config(channel=%d) failed: %s" % (ch, exc))
                return False

            # Channel set succeeded - remember it so the fallback path
            # can tell the operator which channel to align to.
            self._receive_channel = ch

            print("[nocturnation] scanning channel %d for %d ms" % (ch, listen_ms))
            # Read back the ACTUAL radio channel: if it differs from `ch`,
            # the channel-set was silently overridden (WiFi association).
            self._dbg_radio("scan-set-%d" % ch)
            elapsed = 0
            while elapsed < listen_ms:
                buf = self._try_recv()
                if buf is not None:
                    frame = parse_admittable(buf)
                    # TOFU + cross-range gate (Epic 5.5 B6). A frame
                    # that fails the gate (e.g. community-range id
                    # on ch 11) is dropped silently; the channel
                    # remains in scan because no eligible Director
                    # was found on it.
                    now_ms = time.ticks_ms() if time is not None else 0
                    admitted = (frame is not None
                                and self._tofu.admit(frame, ch, now_ms))
                    self._dbg_frame("scan", buf, frame, admitted)
                    if admitted:
                        # Dedup check post-admit so the debug overlay
                        # sees relayed dups (hop_count visible) while
                        # rendering still skips them. See _observe_frame
                        # for the is_duplicate contract.
                        is_dup = self._dedup.seen(frame.source_id,
                                                  frame.sequence_number)
                        self._observe_frame(frame, is_duplicate=is_dup,
                                            raw_buf=buf)
                        print("[nocturnation] locking channel %d "
                              "(src_id=0x%02X)" % (ch, frame.source_id))
                        self._scanner.lock()
                        self._status = "locked"
                        return True
                await asyncio.sleep_ms(poll_ms)
                elapsed += poll_ms

            self._scanner.advance()
        return True

    async def _receive_loop(self) -> None:
        # Block 6: perimeter LEDs continue animating when the app is
        # backgrounded (architecture spec section 7.3). We tick the
        # renderer from this loop rather than from update() so the
        # cadence is the same in both states. update() is foreground-
        # only by Tildagon contract; this loop runs always.
        poll_ms = 5
        render_interval_ms = 50  # ~20 Hz perimeter tick
        last_render_ms = 0 if time is None else time.ticks_ms()
        last_dbg_ms = last_render_ms
        # Return when the mode leaves "lume" so background_task can
        # switch to the Director session.
        while self._mode == "lume":
            buf = self._try_recv()
            if buf is not None:
                frame = parse_admittable(buf)
                # TOFU + cross-range gate (Epic 5.5 B6). Drops frames
                # from non-locked source_ids and community-range ids
                # on channel 11.
                now_ms = time.ticks_ms() if time is not None else 0
                admitted = (frame is not None
                            and self._tofu.admit(frame, self._receive_channel, now_ms))
                self._dbg_frame("rx", buf, frame, admitted)
                if admitted:
                    # Dedup check post-admit so the debug overlay
                    # sees relayed dups (hop_count visible) while
                    # rendering still skips them. See _observe_frame.
                    is_dup = self._dedup.seen(frame.source_id,
                                              frame.sequence_number)
                    self._observe_frame(frame, is_duplicate=is_dup,
                                        raw_buf=buf)
                    if frame.message_type == MessageType.LIGHT_PULSE:
                        print(
                            "[nocturnation] LIGHT r=%d g=%d b=%d cls=%d grp=%d"
                            % (
                                frame.r,
                                frame.g,
                                frame.b,
                                frame.target_class,
                                frame.target_group,
                            )
                        )
            # Periodic radio-state log: reveals whether the actual radio
            # channel drifts from what the app believes (B9 debug).
            if _DEBUG and time is not None:
                now = time.ticks_ms()
                if ticks_diff(now, last_dbg_ms) >= 2000:
                    self._dbg_radio("rx-loop")
                    last_dbg_ms = now
            # Expire the TOFU lock on extended silence. The signal_tracker
            # already shows NO SIGNAL on a 3 s gap; the TOFU timeout is
            # the longer 10 s threshold that decides "give up on this
            # Director and treat the next frame as a fresh lock".
            if time is not None and self._tofu.tick(time.ticks_ms()):
                print("[nocturnation] TOFU lock expired; ready to relock")
            # Signal-loss fallback transitions. Evaluated every poll
            # tick so the 10 s / 40 s edges fire promptly without
            # waiting for an inbound frame (which is the whole point -
            # they fire BECAUSE no frames are coming).
            if time is not None:
                self._evaluate_fallback(time.ticks_ms())
            # Epic 17: dynamic-repeater FSM peer-watch expiry. Runs at
            # every poll tick so the LISTENING → CANDIDATE trigger fires
            # on the 100 ms deadline even when no new frame arrives.
            if self._fsm is not None and time is not None:
                self._fsm.tick(time.ticks_ms())
            # Tick the perimeter at ~20 Hz independent of foreground
            # state. The settings menu, if open, skips this tick (the
            # menu owns the screen visually and our LEDs stay dark).
            if time is not None and not self._settings_open:
                now = time.ticks_ms()
                if ticks_diff(now, last_render_ms) >= render_interval_ms:
                    self._render_perimeter()
                    last_render_ms = now
            await asyncio.sleep_ms(poll_ms)

    def _try_recv(self):
        """Non-blocking ESP-NOW recv. Returns the message bytes or None."""
        if self._esp is None:
            return None
        # Tildagon espnow.recv(timeout_ms) returns (mac, msg). A zero
        # timeout returns immediately if no frame is pending.
        try:
            host, msg = self._esp.recv(0)
        except OSError:
            return None
        if msg is None:
            return None
        return bytes(msg)

    # -- Signal-loss fallback wash ----------------------------------

    def _emit_fallback_wash_start(self, now_ms):
        """Synthesise the calm blue/purple cycle wash + push it to
        both perimeter and LCD renderers as if it had arrived from the
        Director. Local dispatch only - never broadcast."""
        f = make_light_wash_frame(
            target_class=0, target_group=0,
            r1=FALLBACK_COLOUR_A[0], g1=FALLBACK_COLOUR_A[1], b1=FALLBACK_COLOUR_A[2],
            r2=FALLBACK_COLOUR_B[0], g2=FALLBACK_COLOUR_B[1], b2=FALLBACK_COLOUR_B[2],
            attack=FALLBACK_ATTACK_TICKS, release=50,
            intensity=FALLBACK_INTENSITY,
            cycle_ms=FALLBACK_CYCLE_PERIOD_MS,
            ttl_seconds=0, pulse_response=0,
        )
        self._renderer.on_light_wash(f, now_ms)
        self._lcd_renderer.on_light_wash(f, now_ms)
        print("[nocturnation] FALLBACK wash start (blue/purple cycle)")

    def _emit_fallback_wash_fade(self, now_ms):
        """Begin the ~25.5 s fade-to-black phase of the fallback. u8
        release_time caps the fade duration at the wash state machine's
        ceiling; functionally equivalent to the 30 s the operator
        asked for to the eye."""
        f = make_light_wash_end_frame(
            target_class=0, target_group=0,
            release_time=FALLBACK_FADE_TICKS,
        )
        self._renderer.on_light_wash_end(f, now_ms)
        self._lcd_renderer.on_light_wash_end(f, now_ms)
        print("[nocturnation] FALLBACK fade-to-black begin")

    def _emit_fallback_wash_recovery(self, now_ms):
        """Short, sharp fade-out when the Director comes back so the
        returning wash/pulse traffic isn't competing with the
        synthetic baseline."""
        f = make_light_wash_end_frame(
            target_class=0, target_group=0,
            release_time=FALLBACK_RECOVERY_TICKS,
        )
        self._renderer.on_light_wash_end(f, now_ms)
        self._lcd_renderer.on_light_wash_end(f, now_ms)
        print("[nocturnation] FALLBACK wash recovery (signal returned)")

    def _evaluate_fallback(self, now_ms):
        """Drive the fallback state machine off the SignalTracker's
        elapsed-since-last-frame value. Called once per receive-loop
        iteration so the transitions are bench-deterministic regardless
        of inbound traffic cadence."""
        if self._signal_tracker._last_frame_ms is None:
            return   # cold boot, never seen a Director
        age = ticks_diff(now_ms, self._signal_tracker._last_frame_ms)
        if not self._fallback_active and age > FALLBACK_ENTER_MS:
            self._fallback_active = True
            self._emit_fallback_wash_start(now_ms)
        if (self._fallback_active and not self._fallback_faded
                and age > FALLBACK_FADE_START_MS):
            self._fallback_faded = True
            self._emit_fallback_wash_fade(now_ms)

    def _observe_frame(self, frame, is_duplicate=False, raw_buf=None) -> None:
        # Epic 15 bench follow-up: diagnostic state updates on EVERY
        # admitted frame, including duplicates. This is what lets the
        # debug overlay show Hop:1 when a relayed frame arrives even
        # if it's a dup of a direct hop:0 we already rendered. Without
        # this, the operator couldn't tell whether the relay path was
        # alive (dup-filtered relays + healthy direct path looked the
        # same as no-relay).
        #
        # is_duplicate=True skips the render dispatch (LIGHT_PULSE /
        # WASH / TEXT_DISPLAY) so duplicates don't double-fire on the
        # output surfaces - that's still the dedup contract.
        self._frame_count += 1
        self._last_frame = frame
        # Epic 17 B1: notify the repeater FSM (if wired). Fires for
        # BOTH first-seen and duplicate admitted frames - the FSM needs
        # duplicates because a peer's relay of (src, seq) arriving after
        # ours is a dedup-duplicate but the load-bearing peer signal.
        if self._repeater_observer is not None:
            obs_now = time.ticks_ms() if time is not None else None
            self._repeater_observer(frame, is_duplicate, obs_now, raw_buf)
        # Epic 15 bench follow-up: capture per-frame diagnostics for
        # the debug overlay. hop_count from every admitted frame
        # (0 = direct from Director, 1+ = via one or more repeaters).
        # _last_frame_ms drives the prominent "Last: N.Ns" readout +
        # the green/amber/red signal-health background band. The 10 s
        # window tracks every frame (LIGHT_PULSE + LIGHT_WASH +
        # heartbeats) so frames/10s reflects total traffic rather
        # than just the 1 Hz heartbeat baseline.
        self._last_hop_count = frame.hop_count
        # Live-meter per-hop tally - any True at hop>=1 is cast-iron
        # proof a relay path reached us during this session. Clamp
        # defensively (hop>3 is dropped by parse_admittable already,
        # but better robust than crash on a garbage frame).
        if 0 <= frame.hop_count <= 3:
            self._hops_seen[frame.hop_count] = True
        if time is not None:
            now_frame = time.ticks_ms()
            self._last_frame_ms = now_frame
            self._frame_window.append(now_frame)
            cutoff = 10_000
            while (self._frame_window
                   and ticks_diff(now_frame, self._frame_window[0]) > cutoff):
                self._frame_window.pop(0)
        if frame.message_type == MessageType.HEARTBEAT and time is not None:
            now_hb = time.ticks_ms()
            self._last_heartbeat_ms = now_hb
            self._heartbeat_window.append(now_hb)
            # Prune to the last 10 s. ticks_diff handles wrap-around;
            # cheap linear walk because the window is at most ~12
            # entries (heartbeat is 1 Hz, capped at 1 Hz nominal).
            cutoff = 10_000
            while (self._heartbeat_window
                   and ticks_diff(now_hb, self._heartbeat_window[0]) > cutoff):
                self._heartbeat_window.pop(0)
        # Every accepted frame counts as Director-alive proof for the
        # NO SIGNAL detector, regardless of message type. Heartbeats
        # are just as good as LIGHT_PULSEs here.
        if time is not None:
            now_ms_record = time.ticks_ms()
            self._signal_tracker.record_frame(now_ms_record)
            # Cancel any fallback wash that was running. Short-release
            # END fades the synthetic baseline out before the Director's
            # returning traffic starts to compete with it.
            if self._fallback_active:
                self._fallback_active = False
                self._fallback_faded  = False
                self._emit_fallback_wash_recovery(now_ms_record)

        if time is None:
            return

        # Dedup gate (Epic 15 bench follow-up). Diagnostics above ran
        # for every admitted frame; rendering only runs for non-dup
        # frames so duplicates don't double-pulse the output surfaces.
        # This is the same contract the old in-process_frame dedup
        # provided, just moved one layer up so the debug overlay can
        # see relayed dups.
        if is_duplicate:
            return

        now_ms = time.ticks_ms()

        # HEARTBEAT and unknown / reserved-id frames just bump the frame
        # counter without further per-surface dispatch. LIGHT_PULSE plus
        # the LIGHT_WASH family (Epic 6C Phase G) are the routed types;
        # Epic 13 adds TEXT_DISPLAY + CLEAR_SCREEN for display content.
        mt = frame.message_type
        if mt not in (MessageType.LIGHT_PULSE,
                       MessageType.LIGHT_WASH,
                       MessageType.LIGHT_WASH_END,
                       MessageType.LIGHT_WASH_PULSE,
                       MessageType.TEXT_DISPLAY,
                       MessageType.CLEAR_SCREEN):
            return

        # Epic 13 display family carries its own target_group on the
        # payload rather than reusing the wash-family field. Apply the
        # group filter against the right attribute and dispatch directly
        # to the text renderer - no target_class routing (the message
        # type IS the class signal). Done here before the wash-family
        # target_class lookup so we don't accidentally fall through
        # to a missing-class branch.
        if mt == MessageType.TEXT_DISPLAY:
            if frame.text_target_group != 0 \
               and frame.text_target_group != self._settings.group:
                return
            self._lume_text_renderer.on_text_display(frame, now_ms)
            return
        if mt == MessageType.CLEAR_SCREEN:
            if frame.clear_target_group != 0 \
               and frame.clear_target_group != self._settings.group:
                return
            self._lume_text_renderer.on_clear_screen(frame, now_ms)
            return

        # Group filter per protocol manual section 4.2: target_group == 0
        # is broadcast (every receiver fires); otherwise must match the
        # operator-configured group exactly. A device whose own group is
        # 0 only accepts broadcasts, which is what the default settings
        # produce.
        if frame.target_group != 0 and frame.target_group != self._settings.group:
            return
        # Per-surface class routing per Epic 5 Q1. Light-class commands
        # arm the perimeter (wristband analogue); Screen-class arm the
        # LCD; MultiLedScreen arms both; All targets both. Other
        # classes (reserved) are silently dropped.
        cls = frame.target_class

        if mt == MessageType.LIGHT_PULSE:
            if cls in PERIMETER_CLASSES:
                self._renderer.dispatch(frame, now_ms)
            if cls in LCD_CLASSES:
                self._lcd_renderer.dispatch(frame, now_ms)
        elif mt == MessageType.LIGHT_WASH:
            # Tildagon is wash-capable on both surfaces (can_pulse +
            # can_wash + can_overlay). Hand the wash to whichever
            # surface(s) match the target class.
            if cls in PERIMETER_CLASSES:
                self._renderer.on_light_wash(frame, now_ms)
            if cls in LCD_CLASSES:
                self._lcd_renderer.on_light_wash(frame, now_ms)
        elif mt == MessageType.LIGHT_WASH_END:
            if cls in PERIMETER_CLASSES:
                self._renderer.on_light_wash_end(frame, now_ms)
            if cls in LCD_CLASSES:
                self._lcd_renderer.on_light_wash_end(frame, now_ms)
        elif mt == MessageType.LIGHT_WASH_PULSE:
            # The renderer's on_light_wash_pulse handler internally
            # drops the frame if it has no active wash (per design),
            # so the receive-side dispatch routes unconditionally.
            if cls in PERIMETER_CLASSES:
                self._renderer.on_light_wash_pulse(frame, now_ms)
            if cls in LCD_CLASSES:
                self._lcd_renderer.on_light_wash_pulse(frame, now_ms)

    def _lcd_background_rgb01(self):
        """Return (r, g, b) in 0..1 floats for ctx.rgb() to paint as the
        screen background. Falls back to black if no wash is active or
        the runtime time module isn't available (host tests).
        """
        if time is None:
            return (0.0, 0.0, 0.0)
        wash = self._lcd_renderer.current_colour(time.ticks_ms())
        if wash is None:
            return (0.0, 0.0, 0.0)
        r, g, b = wash
        return (r / 255.0, g / 255.0, b / 255.0)


__app_export__ = NocturNationApp
