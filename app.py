"""NocturNation Tildagon receiver app.

Draws the brand-mark, drives ESP-NOW receive with auto-scan / dedup / hop
enforcement, dispatches LIGHT_* frames to perimeter + LCD renderers, and
offers a Director mode for authored Shows.

Design decisions and historical rationale: docs/tildagon-history.md.
"""

# The Tildagon launcher loads this module as ``apps.<dir>.app`` and does
# NOT add the app's own directory to sys.path. Derive it from the
# launcher module name so absolute imports resolve on the badge whatever
# the install dir is called. See docs/tildagon-history.md.
import sys as _sys
try:
    _pkg = __name__.rsplit(".", 1)[0]
    _APP_DIR = "/" + _pkg.replace(".", "/") if "." in __name__ else "/apps/nocturnation"
    if _APP_DIR not in _sys.path:
        _sys.path.append(_APP_DIR)
except Exception as _exc:  # pragma: no cover - defensive only
    print("[nocturnation] could not extend sys.path: %s" % _exc)

# Boot-time instrumentation. Prints [boot] +Nms at each checkpoint so
# reflashing gives a self-contained boot budget on the serial console.
# ticks_ms is MicroPython; fall back to time.time on host CPython.
try:
    from time import ticks_ms as _ticks_ms, ticks_diff as _ticks_diff
except ImportError:  # pragma: no cover - host CPython path
    from time import time as _time_time
    def _ticks_ms():
        return int(_time_time() * 1000)
    def _ticks_diff(a, b):
        return a - b
try:
    import gc as _gc
except ImportError:  # pragma: no cover - unreachable on both runtimes
    _gc = None

_boot_t0 = _ticks_ms()
_BOOT_TRACE = True

def _boot_mark(label):
    if not _BOOT_TRACE:
        return
    dt = _ticks_diff(_ticks_ms(), _boot_t0)
    if _gc is not None and hasattr(_gc, "mem_free"):
        try:
            print("[boot] +%dms  free=%d  alloc=%d  %s" % (
                dt, _gc.mem_free(), _gc.mem_alloc(), label))
            return
        except Exception:
            pass
    print("[boot] +%dms  %s" % (dt, label))

_boot_mark("sys.path ready")

import app
from events.input import Buttons, BUTTON_TYPES

# Optional imports: only available on the badge runtime. On the host
# (pytest) these modules don't exist; the async background_task simply
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
try:
    import imu
except ImportError:
    imu = None
# Stop badge WiFi manager before ESP-NOW or the firmware channel-sweeps
# for an AP and wrecks reception.
try:
    import wifi as _badge_wifi
except ImportError:
    _badge_wifi = None
# Kill list of badge OS scheduler tasks that contend with our tick +
# radio use. Stopped in __init__, restarted in _quit. Frontboards first
# because they own the LCD backing store the other apps draw onto.
# espnow_service and PowerEventHandler deliberately kept alive (radio
# backing + charge-state events). See docs/tildagon-history.md.
try:
    from system.scheduler import scheduler as _sys_scheduler
except Exception:
    _sys_scheduler = None
try:
    from frontboards.twentysix import TwentyTwentySix as _TwentyTwentySix
except Exception:
    _TwentyTwentySix = None
try:
    from frontboards.twentyfour import TwentyTwentyFour as _TwentyTwentyFour
except Exception:
    _TwentyTwentyFour = None
try:
    from system.hexpansion.app import HexpansionManagerApp as _HexpansionManagerApp
except Exception:
    _HexpansionManagerApp = None
try:
    from system.boopscreen.app import BoopSpinner as _BoopSpinner
except Exception:
    _BoopSpinner = None
try:
    from system.patterndisplay.app import PatternDisplay as _PatternDisplay
except Exception:
    _PatternDisplay = None
try:
    from system.backleds.app import BackLEDManager as _BackLEDManager
except Exception:
    _BackLEDManager = None
try:
    from system.launcher.app import Launcher as _Launcher
except Exception:
    _Launcher = None
try:
    from system.notification.app import NotificationService as _NotificationService
except Exception:
    _NotificationService = None
try:
    from system.power.app import PowerManager as _PowerManager
except Exception:
    _PowerManager = None

_KILLABLE_SYSTEM_APP_CLASSES = [
    _TwentyTwentySix,
    _TwentyTwentyFour,
    _HexpansionManagerApp,
    _BoopSpinner,
    _PatternDisplay,
    _BackLEDManager,
    _Launcher,
    _NotificationService,
    _PowerManager,
]

import random

_boot_mark("badge OS imports done")

# IRQ-context RX timestamp + fast-relay state. Kept as module globals so
# the mp_sched-scheduled handler can access them without a Python-object
# attribute lookup (which may allocate). See docs/tildagon-history.md.
_espnow_last_arrival_ms = 0
_espnow_irq_installed = False

_pending_msgs = None
_relay_send_enabled = False
_relay_send_output_hop = 0
_relay_send_buffer = None
_broadcast_mac_bytes = b'\xff\xff\xff\xff\xff\xff'

# Wire-spec byte offsets duplicated as module constants so the IRQ
# fast-path doesn't have to import them from the protocol package.
# Must match protocol frame layout.
_HOP_COUNT_BYTE_OFFSET = 5
_MAX_HOP_COUNT_FOR_RELAY = 3
_MIN_FRAME_LEN_FOR_RELAY = 8


def _espnow_irq_handler(esp):
    # mp_sched context (scheduled from the WiFi-task receive callback).
    # Full Python semantics apply but stay tight - sync-hot path.
    global _espnow_last_arrival_ms
    _espnow_last_arrival_ms = time.ticks_ms()

    if _pending_msgs is None:
        return

    try:
        host, msg = esp.recv(0)
    except OSError:
        return
    if msg is None:
        return

    arrival = _espnow_last_arrival_ms
    _pending_msgs.append((host, msg, arrival))

    # Fast-relay path: when the FSM is ACTIVE/COOLDOWN and this frame's
    # hop+1 matches our elected output_hop, mutate the pre-allocated
    # relay buffer and send. Bookkeeping runs later on the async path via
    # FSM.on_admitted_frame -> _transmit_relay(skip_tx=True).
    if not _relay_send_enabled:
        return
    if _relay_send_buffer is None:
        return
    n = len(msg)
    if n < _MIN_FRAME_LEN_FOR_RELAY:
        return
    hop = msg[_HOP_COUNT_BYTE_OFFSET]
    new_hop = hop + 1
    if new_hop != _relay_send_output_hop:
        return
    if new_hop > _MAX_HOP_COUNT_FOR_RELAY:
        return

    max_n = len(_relay_send_buffer)
    if n > max_n:
        n = max_n
    for i in range(n):
        _relay_send_buffer[i] = msg[i]
    _relay_send_buffer[_HOP_COUNT_BYTE_OFFSET] = new_hop
    try:
        esp.send(_broadcast_mac_bytes,
                 memoryview(_relay_send_buffer)[:n])
    except OSError:
        pass


def _relay_state_change_cb(enabled, output_hop):
    global _relay_send_enabled, _relay_send_output_hop
    _relay_send_enabled = enabled
    _relay_send_output_hop = output_hop

_boot_mark("before nocturnation imports")

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

_boot_mark("nocturnation imports done")


# App version, read once from metadata.json. Fallback path list because
# __file__ semantics differ between host pytest and badge deploy;
# fallback to "?" surfaces as "v?" on the screen.
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


_GROUP_CYCLE = (0, 1, 2, 3)
_CHANNEL_CYCLE = ("auto", "1", "11")

# Bench debug: log actual radio channel vs the app's belief and every
# raw frame with its parse + TOFU verdict. Off by default.
_DEBUG = False

# Bench instrumentation for Phase 1 hop-0 paint-delta measurement.
# Emits [BENCH-RX] / [BENCH-PT] / [BENCH-DROP] / [BENCH-GAP] serial lines
# parsed by tools/bench_hop0_paint_delta.py. Off by default; flip on
# both devices for the measurement window then flip back.
_BENCH_HOP0 = False

# Mirror the flag onto the perimeter module so its drop-log gate matches.
from .nocturnation.render import perimeter as _bench_perimeter_mod
_bench_perimeter_mod._BENCH_DISPATCH_LOG = _BENCH_HOP0

# Director must not broadcast on channel 11 (Performance band reserved
# for commercial Directors with random-per-boot IDs). Explicit blocklist
# is a hard runtime gate catching accidental drift in DIRECTOR_CHANNEL.
DIRECTOR_CHANNEL = 1
DIRECTOR_FORBIDDEN_TX_CHANNELS = frozenset((11,))
DIRECTOR_SOURCE_ID = 0x20

# Signal-loss fallback wash timings. Match the StickC LumeMode constants
# so the fleet converges on the same idle effect at the same moment.
# release_time is u8 (~25.5 s cap), so the "30 s fade" the operator asked
# for is served by the maximum the wash state machine accepts.
FALLBACK_ENTER_MS         = 10000
FALLBACK_FADE_START_MS    = 40000
FALLBACK_CYCLE_PERIOD_MS  = 10000
FALLBACK_ATTACK_TICKS     = 30    # 100 ms units = 3 s
FALLBACK_FADE_TICKS       = 255   # 100 ms units, ~25.5 s (u8 max)
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
        _boot_mark("NocturNationApp.__init__ enter")
        self.button_states = Buttons(self)
        self._dedup = DedupRing()
        self._frame_count = 0
        self._last_frame = None
        # Phase 1 hop-0 paint-delta bench (_BENCH_HOP0) state.
        self._bench_dispatched_key = None
        self._bench_dispatched_ms = 0
        # Render-tick fleet alignment: perimeter tick anchor snaps to the
        # arrival_ms of each admitted frame so subsequent renders fire
        # relative to a shared physical event, not each device's local
        # paint phase. See docs/tildagon-history.md.
        self._render_snap_ms = None
        # Force-paint flag: set by _observe_frame on every pulse-family
        # dispatch so colour transitions land within one poll cadence of
        # dispatch, bypassing the 20 Hz render_interval_ms gate.
        self._render_force_paint = False
        # Debug-overlay diagnostic state.
        self._last_frame_ms     = 0
        self._last_heartbeat_ms = 0
        self._last_hop_count    = 0
        self._frame_window      = []   # list[int_ms], pruned to last 10 s
        self._heartbeat_window  = []   # list[int_ms], pruned to last 10 s
        self._hops_seen         = [False, False, False, False]
        # Optional observer notified for every admitted frame (both
        # first-seen and dedup-duplicate). Wired to the FSM in AUTO mode.
        # Signature: on_admitted_frame(frame, is_duplicate, now_ms, raw_buf).
        self._repeater_observer = None
        self._fsm = None
        self._status = "starting"
        self._esp = None
        self._wlan = None
        # Last channel wlan.config(channel=N) succeeded on; shown on the
        # LCD so the operator can align the Director Stick when
        # auto-scan is off.
        self._receive_channel = None
        self._settings = Settings.load()
        self._scanner = self._make_scanner()
        self._renderer = PerimeterRenderer(calm_mode=self._settings.calm_mode)
        self._lcd_renderer = LcdRenderer(calm_mode=self._settings.calm_mode)
        # LCD text renderer (TEXT_DISPLAY overlay). Independent of the
        # pulse/wash state machine - operator-paced content, no calm-mode
        # gate.
        self._lume_text_renderer = LumeTextRenderer()
        self._settings_open = False
        self._settings_menu = None
        # App role: "idle" (no radio, WiFi up), "lume" (receive), or
        # "director" (Show + IMU + TX). Launch straight into Lume; F
        # (CANCEL) in Lume calls _stop_to_idle.
        self._mode = "lume"
        self._idle_menu = None
        self._help_open = False
        self._help_matrix = None
        # True while we hold the radio (WiFi stopped + ESP-NOW up).
        self._radio_held = False
        # Director runtime, built lazily on first Director-mode entry.
        self._controller = None
        self._director_host = None
        self._dispatcher = None
        self._imu_adapter = None
        self._button_tap = None
        self._dir_buttons = DirectorButtonMapper()
        self._display = CtxDisplay()
        # TX heartbeat pip timestamp. Bumped on every successful
        # esp.send(); _draw_director paints a pip whose colour is
        # derived from its age. None = no TX has landed yet.
        self._director_last_tx_ms = None
        self._director_overlay = None
        self._dir_settings_defs = []
        self._picker_show_ids = []
        self._signal_tracker = SignalTracker()
        # Signal-loss fallback wash state. _fallback_last_check_ms
        # suppresses repeated emission while in the same state.
        self._fallback_active = False
        self._fallback_faded  = False
        # Trust-On-First-Use lock on Director source_id. Locks to the
        # first valid frame from a non-broadcast source; subsequent
        # frames from other IDs are dropped silently. Ch 11 only accepts
        # Performance-range IDs. Expires after 10 s of inactivity.
        self._tofu = TofuLock()
        # Bring the perimeter LEDs out of low-power before the first tick.
        if tildagonos is not None:
            try:
                tildagonos.set_led_power(True)
            except Exception as exc:
                print("[nocturnation] tildagonos.set_led_power failed: %s" % exc)
        # Stop the badge patterndisplay service from writing the LED ring
        # while we're active - both writing simultaneously flickers.
        self._patterns_inhibited = False
        self._inhibit_patterns()
        self._is_foreground = True
        # Subscribe to scheduler foreground events. The launcher caches
        # the app instance so __init__ runs only once; the events keep
        # _is_foreground in sync. Filter by event.app is self.
        if (
            eventbus is not None
            and RequestForegroundPushEvent is not None
            and RequestForegroundPopEvent is not None
        ):
            eventbus.on(RequestForegroundPushEvent, self._on_foreground_push, self)
            eventbus.on(RequestForegroundPopEvent, self._on_foreground_pop, self)
        self._stop_other_system_apps()
        self._first_update_traced = False
        _boot_mark("NocturNationApp.__init__ exit")

    def _on_foreground_push(self, event) -> None:
        # Perimeter continued animating in the background so renderer
        # state is fresh; the LCD envelope was dispatched but not drawn -
        # clear so wash starts cleanly from the next fire.
        if event.app is not self:
            return
        self._is_foreground = True
        self._inhibit_patterns()
        self._lcd_renderer.clear()
        self._lume_text_renderer.clear()
        print("[nocturnation] foreground push - LCD wash resumed")

    def _on_foreground_pop(self, event) -> None:
        # Perimeter LEDs keep animating while backgrounded; the OS
        # routes draw() to the new foreground app.
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
        if not self._first_update_traced:
            self._first_update_traced = True
            _boot_mark("first update() tick")
        if self._settings_open and self._settings_menu is not None:
            self._settings_menu.update(delta)
            return

        if self._help_open:
            if (self.button_states.get(BUTTON_TYPES["CANCEL"])
                    or self.button_states.get(BUTTON_TYPES["CONFIRM"])):
                self.button_states.clear()
                self._close_help()
            return

        if self._mode == "idle":
            # Menu can't be built in __init__ before the app is
            # registered with the scheduler; open lazily on first tick.
            if self._idle_menu is None and Menu is not None:
                self._open_idle_menu()
            if self._idle_menu is not None:
                self._idle_menu.update(delta)
            return

        if self._mode == "director":
            self._update_director(delta)
            return

        # Lume mode.
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._stop_to_idle()
            return
        if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.button_states.clear()
            self._open_settings()
            return
        # Perimeter ticking happens in background_task so it runs
        # whether we're foregrounded or not.

    def _update_director(self, delta: float) -> None:
        """Foreground input for Director mode.

        CONFIRM = manual tap (edge-detected by ButtonTapSource - read
        held state without clearing so the edge detector still fires).
        CANCEL = exit Director. UP/DOWN/LEFT/RIGHT -> InputAction via
        DirectorButtonMapper (short LEFT/RIGHT = palette, long = section).
        IMU tap-to-beat is polled from background_task.
        """
        if self._director_overlay is not None:
            self._director_overlay.update(delta)
            return
        if self._controller is None:
            return

        now = time.ticks_ms() if time is not None else 0

        # Deliberately NOT cleared - clear() would wipe the held state
        # the edge detector relies on.
        confirm = bool(self.button_states.get(BUTTON_TYPES["CONFIRM"]))
        self._controller.poll_button(confirm, now)

        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._stop_to_idle()
            return

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

    # ---------------------------------------------------------------------
    # Idle start menu + mode start/stop
    # ---------------------------------------------------------------------

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
            self._open_settings()
        elif idx == 3:
            self._open_help()
        elif idx == 4:
            self._quit()

    def _idle_menu_back(self) -> None:
        # Minimise to the launcher; WiFi stays up (we never took the
        # radio in idle).
        self.minimise()

    def _start_mode(self, mode) -> None:
        # The background loop sees the mode change and acquires the
        # radio (stopping WiFi).
        self._close_idle_menu()
        self._mode = mode
        self._settings.mode = mode
        try:
            self._settings.save()
        except Exception as exc:
            print("[nocturnation] settings save failed: %s" % exc)
        print("[nocturnation] starting %s mode" % mode)

    def _stop_to_idle(self) -> None:
        # Background loop releases the radio + restores WiFi on its next
        # idle iteration.
        self._mode = "idle"
        self._renderer.clear()
        self._lcd_renderer.clear()
        self._lume_text_renderer.clear()
        self._fallback_active = False
        self._fallback_faded  = False
        self._dark_perimeter()
        self._open_idle_menu()
        print("[nocturnation] stopped to idle")

    # ---------------------------------------------------------------------
    # Help screen - QR code to the project URL
    # ---------------------------------------------------------------------

    def _build_qr(self, url):
        # Lazy import so the ~1300-line uQR module only loads when Help
        # is actually opened.
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
        # wifi.connect() is non-blocking - reactivates the STA and kicks
        # off association without waiting.
        if _badge_wifi is None:
            return
        try:
            _badge_wifi.connect()
            print("[nocturnation] WiFi restored to the OS")
        except Exception as exc:
            print("[nocturnation] wifi restore failed: %s" % exc)

    def _stop_other_system_apps(self) -> None:
        # Record which classes were successfully stopped so _restart can
        # restore the same set on quit.
        self._stopped_system_apps = []
        if _sys_scheduler is None:
            return
        for cls in _KILLABLE_SYSTEM_APP_CLASSES:
            if cls is None:
                continue
            try:
                _sys_scheduler.stop_app(cls())
                self._stopped_system_apps.append(cls)
            except Exception as exc:
                print("[nocturnation] stop_app(%s) failed: %s"
                      % (cls.__name__, exc))
        if self._stopped_system_apps:
            print("[nocturnation] stopped %d system app(s) to reduce "
                  "scheduler contention" % len(self._stopped_system_apps))

    def _restart_other_system_apps(self) -> None:
        if _sys_scheduler is None:
            return
        stopped = getattr(self, "_stopped_system_apps", None)
        if not stopped:
            return
        for cls in stopped:
            try:
                _sys_scheduler.start_app(cls())
            except Exception as exc:
                print("[nocturnation] start_app(%s) failed: %s"
                      % (cls.__name__, exc))
        print("[nocturnation] restarted %d system app(s)" % len(stopped))
        self._stopped_system_apps = []

    def _quit(self) -> None:
        # Release ESP-NOW before WiFi reclaims the radio.
        try:
            if self._esp is not None:
                self._esp.active(False)
        except Exception as exc:
            print("[nocturnation] esp deactivate failed: %s" % exc)
        self._restore_wifi()
        self._resume_patterns()
        self._dark_perimeter()
        # Launcher must be restarted before RequestStopAppEvent so
        # there's something to fall through to.
        self._restart_other_system_apps()
        print("[nocturnation] quitting")
        if eventbus is not None and RequestStopAppEvent is not None:
            try:
                eventbus.emit(RequestStopAppEvent(self))
                return
            except Exception as exc:
                print("[nocturnation] stop-app emit failed: %s" % exc)
        self.minimise()

    def _open_settings(self) -> None:
        if Menu is None:
            print("[nocturnation] Menu component unavailable; cannot open settings")
            return
        self._close_idle_menu()
        self._settings_open = True
        # Clear in-flight envelopes so the LEDs don't hold a stale
        # brightness while _render_perimeter() stops ticking.
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
        # Menu doesn't clear button_states after invoking select_handler /
        # back_handler; without this the next update() cycle re-reads the
        # same CONFIRM press as a fresh menu-open.
        self.button_states.clear()
        if self._mode == "idle":
            self._open_idle_menu()

    def _settings_menu_items(self):
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
            self._settings.debug_mode = not self._settings.debug_mode
        elif idx == 4:
            # Repeat toggle. Takes effect on next Lume-mode session entry -
            # a mid-session toggle does not tear down an active FSM here.
            self._settings.repeat = "OFF" if self._settings.repeat == "AUTO" else "AUTO"
        elif idx == 5:
            # Rescan: TOFU-only reset. The Tildagon's radio doesn't
            # reliably support channel re-scanning post-boot (Q6), so the
            # channel stays the same.
            self._tofu.clear()
            print("[nocturnation] TOFU lock cleared by operator")
            self._close_settings()
            return
        elif idx == 6:
            self._close_settings()
            return
        # If save fails we keep the in-memory change so the UI is
        # consistent; next change retries.
        try:
            self._settings.save()
        except Exception as exc:
            print("[nocturnation] settings save failed: %s" % exc)
        self._rebuild_settings_menu()

    def _settings_back(self) -> None:
        self._close_settings()

    def _apply_calm_mode(self) -> None:
        on = self._settings.calm_mode
        self._renderer.set_calm_mode(on)
        self._lcd_renderer.set_calm_mode(on)
        if not on:
            # Switching into Full mode - clear the LCD wash so it starts
            # from black at the next dispatch rather than holding stale.
            self._lcd_renderer.clear()

    def _start_repeater(self) -> None:
        """Instantiate the dynamic-repeater FSM if Repeat=AUTO. No-op
        when Repeat=OFF or the radio isn't up.
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
            # add_peer isn't otherwise idempotent on this MicroPython build.
            pass
        esp_ref = self._esp

        def _relay_send(payload):
            # esp_ref captured at start; the FSM is torn down at
            # _stop_repeater before the radio is released, so this
            # closure only fires while _esp is live.
            esp_ref.send(BROADCAST_MAC, payload)

        def _relay_random(lo, hi):
            return random.randint(lo, hi)

        now = time.ticks_ms() if time is not None else 0
        # skip_tx=True when IRQ is installed: the mp_sched handler
        # already TX'd. FSM still runs bookkeeping so peer detection +
        # cooldown timers stay accurate.
        self._fsm = DynamicRepeater(
            _relay_send, _relay_random, now_ms=now,
            on_relay_state_change=_relay_state_change_cb,
            skip_tx=_espnow_irq_installed,
        )
        self._repeater_observer = self._fsm.on_admitted_frame
        print("[nocturnation] dynamic repeater armed (Repeat=AUTO"
              "%s)" % (", IRQ fast relay" if _espnow_irq_installed else ""))

    def _stop_repeater(self) -> None:
        # Idempotent - safe to call in any state.
        if self._fsm is not None:
            print("[nocturnation] dynamic repeater disarmed "
                  "(relayed=%d peer-seen=%d)" %
                  (self._fsm.relayed_count, self._fsm.peer_seen_count))
        self._fsm = None
        self._repeater_observer = None

    def _render_perimeter(self) -> None:
        # Commit with a single leds.write() so the ring updates atomically.
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
            # Don't let a hardware glitch take down the update loop.
            print("[nocturnation] perimeter render failed: %s" % exc)
        # Emit a [BENCH-PT] line the first render tick after each
        # LIGHT_PULSE dispatch, then clear the key.
        if _BENCH_HOP0 and self._bench_dispatched_key is not None:
            key = self._bench_dispatched_key
            print("[BENCH-PT] src=%d seq=%d ticks=%d delay_ms=%d"
                  % (key[0], key[1], now_ms,
                     ticks_diff(now_ms, self._bench_dispatched_ms)))
            self._bench_dispatched_key = None

    def draw(self, ctx) -> None:
        if self._settings_open and self._settings_menu is not None:
            if clear_background is not None:
                clear_background(ctx)
            else:
                ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
            self._settings_menu.draw(ctx)
            return

        if self._help_open:
            self._draw_help(ctx)
            return

        if self._mode == "idle":
            self._draw_idle(ctx)
            return

        if self._mode == "director":
            self._draw_director(ctx)
            return

        # Debug overlay owns the screen when enabled - bypasses the
        # background image + wash + text layers.
        if self._mode == "lume" and self._settings.debug_mode:
            self._draw_debug_overlay(ctx)
            return

        # DirID-keyed background image (JPG). Ctx caches by path so we
        # can call it every frame without re-decoding. Unknown DirID
        # falls back to default.jpg; missing default = paint solid wash
        # colour underneath.
        bg_path = bg_images.path_for_dir_id(self._tofu.locked_id)
        if bg_path is not None:
            ctx.image(bg_path, -120, -120, 240, 240)
        else:
            bg_r, bg_g, bg_b = self._lcd_background_rgb01()
            ctx.rgb(bg_r, bg_g, bg_b).rectangle(-120, -120, 240, 240).fill()

        # Once locked, the LCD is a content surface (mirrors StickC's
        # "LCD is content in Lume" role). Suppress the diagnostic HUD -
        # would be noise against operator-paced text content. NO SIGNAL
        # still surfaces as a small footer because radio liveness
        # matters even when a lyric is on the screen.
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
        ctx.move_to(0, 0).text(
            format_lock_label(
                channel=self._scanner.current_channel,
                scanner_locked=self._scanner.is_locked,
                tofu_locked_id=self._tofu.locked_id,
            )
        )

        # NO SIGNAL overlay takes precedence over the RGB triplet: it
        # answers the more important "is the Director alive?" question.
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

        # Version line above the tagline so the round display can't clip
        # it - earlier position at y=105 sat on the edge of the visible arc.
        ctx.font_size = 12
        ctx.move_to(0, -33).text("v%s" % _APP_VERSION)

        # Tildagon convention: C = select, F = back.
        ctx.font_size = 10
        ctx.move_to(0, 85).text("C: settings   F: exit")

    def _draw_debug_overlay(self, ctx) -> None:
        """Diagnostic readout for repeat-mode + range testing.

        Layout top-to-bottom (large fonts for outdoor readability):
        lock label, last frame age (prominent), frames/10s, hop count +
        live meter of higher hops seen, FSM state (if Repeat=AUTO).
        Background tints green/amber/red by last-frame-age band; text
        colour swaps to keep contrast on amber.
        See docs/tildagon-history.md for the design.
        """
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

        if band == "green":
            bg_r, bg_g, bg_b = 0.0, 0.5, 0.0
            fg = (1.0, 1.0, 1.0)
            dim = (0.85, 1.0, 0.85)
        elif band == "amber":
            # Black text on amber - yellow + white unreadable in bright
            # daylight at arm's length.
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

        ctx.rgb(*fg)
        ctx.font_size = 20
        ctx.move_to(0, -85).text(
            format_lock_label(
                channel=self._scanner.current_channel,
                scanner_locked=self._scanner.is_locked,
                tofu_locked_id=self._tofu.locked_id,
            )
        )

        # 1/10 s precision is what the operator can perceive while walking.
        ctx.font_size = 26
        if age_ms < 0:
            ctx.move_to(0, -45).text("Last: --")
        else:
            ctx.move_to(0, -45).text("Last: %d.%ds" % (age_ms // 1000,
                                                       (age_ms % 1000) // 100))

        ctx.font_size = 22
        fr_per_10s = len(self._frame_window)
        ctx.move_to(0, -10).text("Fr/10s: %d" % fr_per_10s)

        # "Hop: N (a b ...)" where (a b ...) are hop levels GREATER than
        # N observed in this session. Empty parens after a
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
            ctx.rgb(*dim)
            ctx.font_size = 18
            ctx.move_to(0, 60).text("Tot: %d" % self._frame_count)

        ctx.rgb(*fg)
        ctx.font_size = 14
        ctx.move_to(0, 95).text("C: toggle off")

    def _draw_idle(self, ctx) -> None:
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
        # Round 240 px screen inscribes a ~170 px square. Floor the
        # module size so corner finder patterns stay on-screen.
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
        # Caption below the code's bottom edge - keeps it where the round
        # screen is still wide enough for the full text.
        ctx.rgb(0, 0, 0)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 10
        ctx.move_to(0, (offset + code_px) + 8).text(self._settings.help_url)

    def _draw_director(self, ctx) -> None:
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

        # Third-party Show code is a system boundary - a crashing Show
        # must not take the UI down with it.
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

        # TX heartbeat pip. Drawn last so a Show that clears its own
        # background can't wipe it. (60, -85) keeps it inside the round
        # 240 display; 2 s outer threshold matches the 1 Hz heartbeat.
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
            ctx.rectangle(60, -85, 10, 10).fill()

    async def background_task(self) -> None:
        """ESP-NOW receive loop: auto-scan, lock, then receive forever.

        Per protocol manual section 5.3 the Lume tries channel 11 first
        then channel 1, each for ~2 s, repeating until a valid frame
        arrives. After lock, receive runs on the locked channel until
        the app is killed.

        Fallback: if the badge's networking layer rejects channel
        changes on STA_IF, we skip auto-scan and listen on whichever
        channel the radio is already on.
        """
        if espnow is None or network is None or asyncio is None:
            self._status = "no radio module"
            print("[nocturnation] required modules unavailable; receive disabled")
            return

        # Session loop: idle -> WiFi up, no radio; mode -> take radio
        # (wifi.stop + ESP-NOW); back to idle -> release and restore.
        while True:
            if self._mode == "idle":
                self._release_radio()
                await asyncio.sleep_ms(100)
                continue
            self._acquire_radio()
            if self._wlan is None:
                self._mode = "idle"
                await asyncio.sleep_ms(200)
                continue
            if self._mode == "director":
                await self._director_session(self._wlan)
            else:
                await self._lume_session(self._wlan)
            await asyncio.sleep_ms(20)

    def _acquire_radio(self) -> None:
        """Take the radio for ESP-NOW: stop the badge WiFi manager (or
        the ESP32 firmware keeps channel-sweeping for an AP), bring STA
        up, activate ESP-NOW. Idempotent while held."""
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
            # protocol=8 = WIFI_PROTOCOL_LR. Fleet-wide commitment - LR
            # peers can't decode standard 802.11b/g/n peers, so every
            # Director + Lume must enable this together. Raw int because
            # this MicroPython build has the setter but not the named
            # LR constant. See docs/tildagon-history.md.
            try:
                self._wlan.config(protocol=8)
            except Exception as exc:
                print("[nocturnation] wlan.config(protocol=8/LR) failed: %s" % exc)
            # PM_NONE needed or the STA sleeps through short ESP-NOW
            # bursts (Director sends 2x retransmits within ~2 ms).
            # Raises idle current; any future light-sleep work must keep
            # the radio awake for heartbeat windows or this bug returns.
            try:
                self._wlan.config(pm=network.WLAN.PM_NONE)
            except Exception as exc:
                print("[nocturnation] wlan.config(pm=PM_NONE) failed: %s" % exc)
            if self._esp is None:
                self._esp = espnow.ESPNow()
            # Default rxbuf ~526 B (~13 msg) is too small: high-rate
            # effects (Rainbow at 10 Hz * 2x TX = 20 msg/s) fill it and
            # some MicroPython builds then stop delivering entirely.
            # 8 KB gives ~200 msg headroom.
            try:
                self._esp.config(rxbuf=8192)
            except Exception as exc:
                print("[nocturnation] espnow.config(rxbuf) failed: %s" % exc)
            self._esp.active(True)
            # Pre-allocate IRQ fast-path buffers before installing the
            # handler - once irq() is set the callback can fire on the
            # next message arrival.
            global _pending_msgs, _relay_send_buffer
            if _pending_msgs is None:
                _pending_msgs = []
            if _relay_send_buffer is None:
                _relay_send_buffer = bytearray(32)   # protocol max frame
            # Older MicroPython builds without irq() degrade to the
            # async poll path - _try_recv falls back to esp.recv().
            global _espnow_irq_installed
            if not _espnow_irq_installed:
                try:
                    self._esp.irq(_espnow_irq_handler)
                    _espnow_irq_installed = True
                    print("[nocturnation] espnow.irq installed for RX + fast relay")
                except Exception as exc:
                    print("[nocturnation] espnow.irq unavailable, "
                          "using poll-time stamp + async relay: %s" % exc)
        except Exception as exc:
            print("[nocturnation] radio acquire failed: %s" % exc)
            self._wlan = None
            return
        # Reset per-session state so re-entry starts in truthful NO
        # SIGNAL rather than carrying a stale last-frame timestamp.
        self._scanner = self._make_scanner()
        self._tofu.clear()
        self._signal_tracker.reset()
        self._radio_held = True
        self._dbg_radio("acquire")

    def _bounce_radio(self) -> bool:
        """Cycle STA_IF active so the next wlan.config(channel=N) call
        becomes a fresh first-config-after-active (Q6 workaround).

        Cheaper than _release_radio + _acquire_radio because it skips
        the badge_wifi restore/stop and does NOT reset scanner / TOFU /
        signal-tracker (which must survive across scan rotations). LR
        PHY + PM_NONE need re-applying because active(False) throws
        them away. See docs/tildagon-history.md.
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
        # A pinned channel keeps the radio put so it can't mis-lock onto
        # a neighbour that bleeds a stray frame in.
        ch = self._settings.channel
        if ch == "1":
            return ChannelScanner(order=(1,))
        if ch == "11":
            return ChannelScanner(order=(11,))
        return ChannelScanner()

    async def _lume_session(self, wlan) -> None:
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
            # Auto-scan bailed because channel-set failed. The radio is
            # on whichever channel was last successfully set.
            if self._receive_channel is not None:
                self._status = "ch %d (no-scan)" % self._receive_channel
                print(
                    "[nocturnation] auto-scan unavailable; listening on channel %d "
                    "(align Director Stick to this channel)" % self._receive_channel
                )
            else:
                self._status = "no-scan"
                print("[nocturnation] auto-scan unavailable; listening on default channel")

        self._start_repeater()

        await self._receive_loop()

        # Tear down so a subsequent Director session doesn't inherit
        # stale state.
        self._stop_repeater()

    async def _director_session(self, wlan) -> None:
        """Director transmit session: build the runtime, claim the
        hobby channel, then poll IMU + tick the active Show + render the
        perimeter until the mode changes.
        """
        self._ensure_director()
        if self._controller is None or not self._controller.show_ids():
            print("[nocturnation] no Director shows available; back to idle")
            self._stop_to_idle()
            return

        # Hard guard against Director TX on forbidden channels. Today
        # DIRECTOR_CHANNEL is 1 so this is belt-and-braces; catches
        # accidental drift (settings injection, fork divergence).
        if DIRECTOR_CHANNEL in DIRECTOR_FORBIDDEN_TX_CHANNELS:
            print(
                "[nocturnation] director TX refused on forbidden channel %d "
                "(reserved for commercial Directors); back to idle"
                % DIRECTOR_CHANNEL
            )
            self._status = "ch %d off-limits" % DIRECTOR_CHANNEL
            self._stop_to_idle()
            return

        # Q6 workaround: if we arrived via a prior Lume session, that
        # first-config slot was already spent on the scan's channel 11,
        # so config(channel=1) below would silently fail and the
        # Director would broadcast on ch 11. Bounce STA_IF active state
        # so ch 1 becomes the fresh first-config after active(True).
        # See docs/tildagon-history.md.
        if not self._bounce_radio():
            print("[nocturnation] director: radio bounce failed; back to idle")
            self._status = "no radio"
            self._stop_to_idle()
            return

        # ESP-NOW peer table is wiped by esp.active(False) inside the
        # bounce. make_sender registered the broadcast peer ONCE during
        # _ensure_director, so heartbeat sends after the bounce would
        # raise "peer not found" and the exception would silently drop
        # the frame. Re-add here; idempotent-with-OSError.
        if self._esp is not None:
            try:
                self._esp.add_peer(BROADCAST_MAC)
            except OSError:
                pass
            except Exception as exc:
                print("[nocturnation] director: peer re-add failed: %s" % exc)

        try:
            wlan.config(channel=DIRECTOR_CHANNEL)
            self._receive_channel = DIRECTOR_CHANNEL
        except Exception as exc:
            print("[nocturnation] director wlan.config(channel=%d) failed: %s"
                  % (DIRECTOR_CHANNEL, exc))
            self._status = "ch %d config failed" % DIRECTOR_CHANNEL
            self._stop_to_idle()
            return

        # Readback verify: refuse to broadcast rather than silently TX
        # on the wrong channel. Best-effort; some MicroPython builds
        # don't support config("channel") readback.
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

        # Reset the pip so a stale timestamp from a prior Director
        # session doesn't paint green before the first TX.
        self._director_last_tx_ms = None

        self._controller.enter()
        self._bind_display()
        self._status = "director"

        poll_ms = 5
        # ~50 Hz IMU poll matches how the tap/motion detector was tuned
        # (faster lets the gravity EMA erode tap transients).
        imu_interval_ms = 20
        render_interval_ms = 50  # ~20 Hz perimeter tick
        now0 = time.ticks_ms() if time is not None else 0
        last_imu = now0
        last_render = now0
        while self._mode == "director":
            now = time.ticks_ms() if time is not None else 0
            # 1 Hz HEARTBEAT (skip-if-recent) so Lumes discover the
            # channel and keep TOFU lock between taps. Runs even while
            # an overlay is open.
            if time is not None:
                self._dispatcher.heartbeat_tick(now)
            if self._director_overlay is None:
                if time is not None and ticks_diff(now, last_imu) >= imu_interval_ms:
                    self._controller.poll_inputs(now, button_pressed=None)
                    last_imu = now
                self._controller.tick(now)
                if time is not None and ticks_diff(now, last_render) >= render_interval_ms:
                    self._render_perimeter()
                    last_render = now
            await asyncio.sleep_ms(poll_ms)

        self._controller.exit()

    # ---------------------------------------------------------------------
    # Director runtime + overlays
    # ---------------------------------------------------------------------

    def _ensure_director(self) -> None:
        """Build the Director runtime once. Idempotent."""
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
                # Wrap the sender to stamp _director_last_tx_ms on every
                # successful send - drives the LCD heartbeat pip.
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
            redundancy=3,   # ESP-NOW is lossy; match M5 master's 3x TX
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
        ctx.set_property(pd.key, self._cycle_prop(pd, cur))
        self._build_dir_settings_menu()

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
        # COLOUR isn't cyclable from a single-button menu.
        return cur

    def _close_overlay(self) -> None:
        if self._director_overlay is not None:
            try:
                self._director_overlay._cleanup()
            except Exception:
                pass
        self._director_overlay = None
        self.button_states.clear()
        # Drop edge state so a button still held when the overlay closes
        # doesn't immediately re-trigger.
        self._dir_buttons.reset()

    def _dbg_radio(self, where) -> None:
        # If wlan reports a different channel than the scanner thinks,
        # the channel-set is being silently overridden.
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
        platform - the caller drops into receive without a locked channel.
        """
        listen_ms = self._scanner.listen_ms
        poll_ms = 50
        self._status = "Open-source crowd lighting"

        # Q6 workaround: bounce STA_IF around every non-first channel-set
        # so SCAN_ORDER actually rotates (11 -> 1 -> 6). Skip on first
        # iteration - _acquire_radio just brought the STA up so the
        # first config call is a fresh first-config-after-active.
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
                # STA_IF still rejects even after a bounce - fall back
                # to receive on whichever channel was last successfully
                # set.
                self._status = "ch %d err" % ch
                print("[nocturnation] wlan.config(channel=%d) failed: %s" % (ch, exc))
                return False

            self._receive_channel = ch

            print("[nocturnation] scanning channel %d for %d ms" % (ch, listen_ms))
            self._dbg_radio("scan-set-%d" % ch)
            elapsed = 0
            while elapsed < listen_ms:
                buf, arrival_ms = self._try_recv()
                if buf is not None:
                    frame = parse_admittable(buf)
                    now_ms = time.ticks_ms() if time is not None else 0
                    admitted = (frame is not None
                                and self._tofu.admit(frame, ch, now_ms))
                    self._dbg_frame("scan", buf, frame, admitted)
                    if admitted:
                        # Dedup check post-admit so the debug overlay
                        # sees relayed dups (hop_count visible) while
                        # rendering still skips them.
                        is_dup = self._dedup.seen(frame.source_id,
                                                  frame.sequence_number)
                        self._observe_frame(frame, is_duplicate=is_dup,
                                            raw_buf=buf,
                                            arrival_ms=arrival_ms)
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
        # Tick renderer from this loop, not update() (update is
        # foreground-only by Tildagon contract), so perimeter LEDs
        # continue animating when the app is backgrounded.
        poll_ms = 5
        render_interval_ms = 50  # ~20 Hz perimeter tick
        last_render_ms = 0 if time is None else time.ticks_ms()
        last_dbg_ms = last_render_ms
        # Watch for asyncio-scheduler stalls (GC pause etc.) that could
        # slip envelope start times between two badges. Poll cadence is
        # 5 ms; anything >= 25 ms is a stall worth logging.
        last_poll_ms = last_render_ms
        while self._mode == "lume":
            if _BENCH_HOP0 and time is not None:
                now_poll = time.ticks_ms()
                gap = ticks_diff(now_poll, last_poll_ms)
                if gap >= 25:
                    print("[BENCH-GAP] ticks=%d gap_ms=%d"
                          % (now_poll, gap))
                last_poll_ms = now_poll
            # Drain up to N frames per iteration - a single-drain path
            # backs the rxbuf up under high-rate effects. Cap at 16 so a
            # broken sender can't starve the render/asyncio path.
            drain_limit = 16
            for _ in range(drain_limit):
                buf, arrival_ms = self._try_recv()
                if buf is None:
                    break
                frame = parse_admittable(buf)
                now_ms = time.ticks_ms() if time is not None else 0
                admitted = (frame is not None
                            and self._tofu.admit(frame, self._receive_channel, now_ms))
                self._dbg_frame("rx", buf, frame, admitted)
                if admitted:
                    is_dup = self._dedup.seen(frame.source_id,
                                              frame.sequence_number)
                    self._observe_frame(frame, is_duplicate=is_dup,
                                        raw_buf=buf,
                                        arrival_ms=arrival_ms)
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
            if _DEBUG and time is not None:
                now = time.ticks_ms()
                if ticks_diff(now, last_dbg_ms) >= 2000:
                    self._dbg_radio("rx-loop")
                    last_dbg_ms = now
            # TOFU lock expiry (10 s silence) - longer than the 3 s
            # NO SIGNAL threshold.
            if time is not None and self._tofu.tick(time.ticks_ms()):
                print("[nocturnation] TOFU lock expired; ready to relock")
            # Fallback state machine evaluated every poll so the
            # 10 s / 40 s edges fire promptly without waiting for an
            # inbound frame (which is the whole point - they fire
            # BECAUSE no frames are coming).
            if time is not None:
                self._evaluate_fallback(time.ticks_ms())
            # FSM peer-watch expiry every poll so LISTENING -> CANDIDATE
            # fires on the 100 ms deadline even when no new frame
            # arrives.
            if self._fsm is not None and time is not None:
                self._fsm.tick(time.ticks_ms())
            # Perimeter tick: fires on either force-paint (fresh
            # pulse-family dispatch this iteration) or 20 Hz idle
            # cadence for envelope decay smoothing. Settings menu owns
            # the screen when open.
            if time is not None and not self._settings_open:
                now = time.ticks_ms()
                if (self._render_force_paint
                        or ticks_diff(now, last_render_ms) >= render_interval_ms):
                    self._render_perimeter()
                    self._render_force_paint = False
                    # Snap the tick anchor to arrival_ms if a frame
                    # landed this iteration - keeps subsequent renders
                    # on a shared reference across the fleet.
                    if self._render_snap_ms is not None:
                        last_render_ms = self._render_snap_ms
                        self._render_snap_ms = None
                    else:
                        last_render_ms = now
            await asyncio.sleep_ms(poll_ms)

    def _try_recv(self):
        """Non-blocking ESP-NOW recv. Returns (msg_bytes, arrival_ms) or
        (None, None).

        Preferred drain path is _pending_msgs (filled by
        _espnow_irq_handler at mp_sched latency ~5 ms after physical
        arrival); fallback to esp.recv(0) for MicroPython builds where
        irq() couldn't be installed. arrival_ms comes from
        peers_table[host][1] (raw ESP-IDF C-callback stamp)
        preferentially, then _espnow_last_arrival_ms (mp_sched stamp),
        then poll-time.
        """
        if _pending_msgs is not None and len(_pending_msgs) > 0:
            host, msg, irq_arrival = _pending_msgs.pop(0)
            arrival = irq_arrival
            try:
                entry = self._esp.peers_table.get(host)
                if entry is not None and len(entry) >= 2:
                    arrival = entry[1]
            except (AttributeError, TypeError):
                pass
            return bytes(msg), arrival
        if self._esp is None:
            return None, None
        try:
            host, msg = self._esp.recv(0)
        except OSError:
            return None, None
        if msg is None:
            return None, None
        arrival = None
        try:
            entry = self._esp.peers_table.get(host)
            if entry is not None and len(entry) >= 2:
                arrival = entry[1]
        except (AttributeError, TypeError):
            pass
        if arrival is None:
            if _espnow_irq_installed:
                arrival = _espnow_last_arrival_ms
            else:
                arrival = time.ticks_ms() if time is not None else 0
        return bytes(msg), arrival

    # -- Signal-loss fallback wash ----------------------------------

    def _emit_fallback_wash_start(self, now_ms):
        # Local dispatch only - never broadcast.
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
        f = make_light_wash_end_frame(
            target_class=0, target_group=0,
            release_time=FALLBACK_FADE_TICKS,
        )
        self._renderer.on_light_wash_end(f, now_ms)
        self._lcd_renderer.on_light_wash_end(f, now_ms)
        print("[nocturnation] FALLBACK fade-to-black begin")

    def _emit_fallback_wash_recovery(self, now_ms):
        # Short fade-out when Director returns so its wash/pulse traffic
        # isn't competing with the synthetic baseline.
        f = make_light_wash_end_frame(
            target_class=0, target_group=0,
            release_time=FALLBACK_RECOVERY_TICKS,
        )
        self._renderer.on_light_wash_end(f, now_ms)
        self._lcd_renderer.on_light_wash_end(f, now_ms)
        print("[nocturnation] FALLBACK wash recovery (signal returned)")

    def _evaluate_fallback(self, now_ms):
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

    def _observe_frame(self, frame, is_duplicate=False, raw_buf=None,
                       arrival_ms=None) -> None:
        # Runs for EVERY admitted frame, including dedup-duplicates, so
        # the debug overlay can see relayed dups (Hop:1 when a relayed
        # frame arrives even if it's a dup of a direct hop:0 we already
        # rendered). Render dispatch is gated below by is_duplicate.
        self._frame_count += 1
        self._last_frame = frame
        # Snap the render anchor to arrival_ms - every admitted frame
        # is a shared physical event across the fleet. HEARTBEAT
        # arrivals cover quiet stretches when no LIGHT_PULSE would
        # otherwise re-anchor.
        if arrival_ms is not None:
            self._render_snap_ms = arrival_ms
        # Notify the FSM (if wired). Fires for both first-seen AND
        # duplicate admitted frames - the FSM needs duplicates because
        # a peer's relay of (src, seq) arriving after ours is a
        # dedup-duplicate but the load-bearing peer signal.
        if self._repeater_observer is not None:
            obs_now = time.ticks_ms() if time is not None else None
            self._repeater_observer(frame, is_duplicate, obs_now, raw_buf)
        self._last_hop_count = frame.hop_count
        # Clamp defensively (hop>3 is dropped by parse_admittable
        # already, but better robust than crash on a garbage frame).
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
            # ticks_diff handles wrap-around; cheap linear walk because
            # the window is at most ~12 entries (heartbeat is 1 Hz).
            cutoff = 10_000
            while (self._heartbeat_window
                   and ticks_diff(now_hb, self._heartbeat_window[0]) > cutoff):
                self._heartbeat_window.pop(0)
        # Every accepted frame counts as Director-alive proof for the
        # NO SIGNAL detector, regardless of message type.
        if time is not None:
            now_ms_record = time.ticks_ms()
            self._signal_tracker.record_frame(now_ms_record)
            # Short-release END fades the synthetic baseline out before
            # the Director's returning traffic starts to compete.
            if self._fallback_active:
                self._fallback_active = False
                self._fallback_faded  = False
                self._emit_fallback_wash_recovery(now_ms_record)

        if time is None:
            return

        # Dedup gate: rendering only runs for non-dup frames so
        # duplicates don't double-pulse the output surfaces. Diagnostics
        # above ran for every admitted frame so overlay sees relayed dups.
        if is_duplicate:
            return

        # Envelope start_ms comes from arrival_ms when the IRQ handler
        # stamped it. Using the IRQ stamp decouples the visible pulse
        # instant from async-scheduler stalls that otherwise slip
        # envelope start by 80-100 ms between two badges.
        if arrival_ms is not None and _espnow_irq_installed:
            now_ms = arrival_ms
        else:
            now_ms = time.ticks_ms()

        # HEARTBEAT and unknown / reserved-id frames just bump the frame
        # counter. LIGHT_PULSE + LIGHT_WASH family + TEXT_DISPLAY /
        # CLEAR_SCREEN are the routed types.
        mt = frame.message_type
        if mt not in (MessageType.LIGHT_PULSE,
                       MessageType.LIGHT_WASH,
                       MessageType.LIGHT_WASH_END,
                       MessageType.LIGHT_WASH_PULSE,
                       MessageType.TEXT_DISPLAY,
                       MessageType.CLEAR_SCREEN):
            return

        # Display family carries its own target_group (not the wash-
        # family field). Apply the group filter against the right
        # attribute; message type IS the class signal, so no
        # target_class routing.
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

        # Group filter per protocol manual section 4.2: target_group=0
        # is broadcast; otherwise must match the operator-configured
        # group exactly.
        if frame.target_group != 0 and frame.target_group != self._settings.group:
            return
        # Per-surface class routing: Light-class -> perimeter,
        # Screen-class -> LCD, MultiLedScreen -> both, All -> both.
        cls = frame.target_class

        if mt == MessageType.LIGHT_PULSE:
            if _BENCH_HOP0 and cls in PERIMETER_CLASSES:
                # Log RX instant; matching PT line on next
                # _render_perimeter tick. Format grepped by
                # tools/bench_hop0_paint_delta.py.
                print("[BENCH-RX] src=%d seq=%d hop=%d ticks=%d"
                      % (frame.source_id, frame.sequence_number,
                         frame.hop_count, now_ms))
                self._bench_dispatched_key = (frame.source_id,
                                               frame.sequence_number)
                self._bench_dispatched_ms = now_ms
            if cls in PERIMETER_CLASSES:
                self._renderer.dispatch(frame, now_ms)
                self._render_force_paint = True
            if cls in LCD_CLASSES:
                self._lcd_renderer.dispatch(frame, now_ms)
        elif mt == MessageType.LIGHT_WASH:
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
            # on_light_wash_pulse internally drops the frame if there's
            # no active wash (per design), so route unconditionally.
            if cls in PERIMETER_CLASSES:
                self._renderer.on_light_wash_pulse(frame, now_ms)
                self._render_force_paint = True
            if cls in LCD_CLASSES:
                self._lcd_renderer.on_light_wash_pulse(frame, now_ms)

    def _lcd_background_rgb01(self):
        # Falls back to black if no wash is active or the runtime time
        # module isn't available (host tests).
        if time is None:
            return (0.0, 0.0, 0.0)
        wash = self._lcd_renderer.current_colour(time.ticks_ms())
        if wash is None:
            return (0.0, 0.0, 0.0)
        r, g, b = wash
        return (r / 255.0, g / 255.0, b / 255.0)


__app_export__ = NocturNationApp
