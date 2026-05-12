"""NocturNation Tildagon receiver app.

Block 1 (shipped): minimal Tildagon OS app draws the brand-mark and
exits cleanly on CANCEL.

Block 2 (shipped): async background_task drives ESP-NOW receive with
channel auto-scan, deduplication, and hop-count enforcement per the
protocol manual.

Block 3 (shipped): each accepted LIGHT_COMMAND is dispatched to the
PerimeterRenderer which arms per-LED envelopes; the update() tick
advances envelopes and pushes the resulting (r, g, b) per LED via
tildagonos.leds[i].

Block 4 (shipped): the LCD pulse renderer arms a full-screen colour
wash on each accepted dispatch; draw() paints it as the background
beneath the UI text. Calm Mode disables LCD pulsing entirely.

Block 5 (current): persistent settings (Calm Mode, group, channel) +
in-app menu via app_components.Menu. CONFIRM opens the menu; CANCEL
backs out. Class + group filter on inbound LIGHT_COMMAND per protocol
manual section 4.2 and Epic 5 Q1 / Q2.

Reference: https://tildagon.badge.emfcamp.org/tildagon-apps/development/
Block plan: nocturnation-m5 docs/epics/epic-05-tildagon.md
"""

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
    )
except ImportError:
    RequestForegroundPushEvent = None
    RequestForegroundPopEvent = None
try:
    from app_components import Menu, clear_background
except ImportError:
    Menu = None
    clear_background = None

# Relative imports against the internal nocturnation/ package: the
# Tildagon launcher loads this module as apps.nocturnation.app and does
# not add apps/nocturnation/ to sys.path, so absolute `from
# nocturnation.X import Y` fails on the badge. Relative imports resolve
# via the parent package (apps.nocturnation) and work on both runtimes.
# Host-side pytest never imports this file (only the nocturnation/
# package below), so the dot prefix is invisible to the test suite.
from .nocturnation.channel_scan import ChannelScanner
from .nocturnation.protocol import DedupRing, MessageType
from .nocturnation.receive import process_frame
from .nocturnation.render import LcdRenderer, PerimeterRenderer
from .nocturnation.settings import Settings


# Cycle order for the settings menu.
_GROUP_CYCLE = (0, 1, 2, 3)
_CHANNEL_CYCLE = ("auto", "1", "11")

# Class-to-surface routing per Epic 5 Q1. The Tildagon advertises as
# MultiLedScreen (0x03) but renders Light-class commands (0x01) on the
# perimeter too because the LED ring is a wristband-analogue. Screen
# (0x02) targets the LCD only; All (0x00) targets both surfaces.
_PERIMETER_CLASSES = (0x00, 0x01, 0x03)
_LCD_CLASSES = (0x00, 0x02, 0x03)


class NocturNationApp(app.App):
    """Tildagon OS app entry point.

    The badge calls __init__ once at start-up, then drives update(delta)
    and draw(ctx) at ~20 Hz. background_task() is an async coroutine
    that runs for the lifetime of the app, including while backgrounded.
    """

    def __init__(self) -> None:
        self.button_states = Buttons(self)
        self._dedup = DedupRing()
        self._scanner = ChannelScanner()
        self._frame_count = 0
        self._last_frame = None
        self._status = "starting"
        self._esp = None
        # Last channel for which wlan.config(channel=N) succeeded. None
        # if we never managed to set the radio channel at all. Shown on
        # the LCD so the operator knows which channel to align the
        # master Stick to when auto-scan is disabled.
        self._receive_channel = None
        # Load persisted settings before constructing renderers so the
        # initial Calm Mode state matches what the operator last chose.
        self._settings = Settings.load()
        # Perimeter LED renderer. Calm Mode default per persisted
        # settings (default True). Architecture spec section 15.
        self._renderer = PerimeterRenderer(calm_mode=self._settings.calm_mode)
        # LCD pulse renderer. Calm Mode disables the LCD wash entirely
        # per architecture spec section 15.3.
        self._lcd_renderer = LcdRenderer(calm_mode=self._settings.calm_mode)
        # In-app settings menu state.
        self._settings_open = False
        self._settings_menu = None
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

        Re-acquire the perimeter LED ring from the badge's patterndisplay
        service (which may have been re-enabled while we were minimised)
        and clear any stale envelope state so the ring starts dark.
        """
        if event.app is not self:
            return
        self._is_foreground = True
        self._inhibit_patterns()
        self._renderer.clear()
        self._lcd_renderer.clear()
        print("[nocturnation] foreground push - resuming receive + LEDs")

    def _on_foreground_pop(self, event) -> None:
        """Scheduler is taking this app out of the foreground.

        Release the perimeter LED ring back to the badge's patterndisplay
        service. The async background_task keeps running (per Tildagon OS
        contract) but our _receive_loop checks _is_foreground and stops
        processing inbound frames until we are foregrounded again.
        """
        if event.app is not self:
            return
        self._is_foreground = False
        self._resume_patterns()
        self._renderer.clear()
        self._lcd_renderer.clear()
        print("[nocturnation] foreground pop - paused receive, released LEDs")

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

        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._resume_patterns()
            self.minimise()
            return
        if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.button_states.clear()
            self._open_settings()
            return
        self._render_perimeter()

    def _open_settings(self) -> None:
        """Open the in-app settings menu (Block 5).

        Darkens the perimeter ring and clears any in-flight envelopes
        so the LEDs don't hold a stale brightness while update() is
        delegated to the menu and _render_perimeter() stops ticking.
        """
        if Menu is None:
            print("[nocturnation] Menu component unavailable; cannot open settings")
            return
        self._settings_open = True
        self._renderer.clear()
        self._lcd_renderer.clear()
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

    def _settings_menu_items(self):
        """Compose the menu line labels from current settings values."""
        return [
            "Calm Mode: %s" % ("ON" if self._settings.calm_mode else "OFF"),
            "Group: %d" % self._settings.group,
            "Channel: %s" % self._settings.channel,
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

        # Background: LCD pulse wash if Full mode is on and there's an
        # active envelope; otherwise black (Calm Mode keeps the LCD
        # quiet so the badge stays comfortable face-distance).
        bg_r, bg_g, bg_b = self._lcd_background_rgb01()
        ctx.rgb(bg_r, bg_g, bg_b).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        ctx.font_size = 24
        ctx.move_to(0, -50).text("NocturNation")

        ctx.font_size = 12
        ctx.move_to(0, -20).text(self._status)
        ctx.move_to(0, 0).text(
            "ch %d %s"
            % (self._scanner.current_channel, "locked" if self._scanner.is_locked else "scan")
        )
        ctx.move_to(0, 20).text("frames: %d" % self._frame_count)

        if (
            self._last_frame is not None
            and self._last_frame.message_type == MessageType.LIGHT_COMMAND
        ):
            f = self._last_frame
            ctx.move_to(0, 45).text("rgb %02x%02x%02x" % (f.r, f.g, f.b))

    async def background_task(self) -> None:
        """ESP-NOW receive loop: auto-scan, lock, then receive forever.

        Per protocol manual section 5.3 the slave tries channel 11 first
        (suggested show channel) then channel 1 (hobby), each for ~2 s,
        repeating until a valid frame arrives. After lock, receive runs
        on the locked channel until the app is killed.

        Fallback: if the badge's networking layer rejects channel
        changes on STA_IF (observed: RuntimeError 0xffffffff), we skip
        auto-scan and listen on whichever channel the radio is already
        on. The operator must align master + Tildagon channels manually
        in that case.
        """
        if espnow is None or network is None or asyncio is None:
            self._status = "no radio module"
            print("[nocturnation] required modules unavailable; receive disabled")
            return

        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        self._esp = espnow.ESPNow()
        self._esp.active(True)

        if not await self._scan_until_locked(wlan):
            # Auto-scan bailed because channel-set failed mid-scan. The
            # radio is on whichever channel was last successfully set,
            # or on the platform default if none succeeded.
            if self._receive_channel is not None:
                self._status = "ch %d (no-scan)" % self._receive_channel
                print(
                    "[nocturnation] auto-scan unavailable; listening on channel %d "
                    "(align master Stick to this channel)" % self._receive_channel
                )
            else:
                self._status = "no-scan"
                print("[nocturnation] auto-scan unavailable; listening on default channel")

        await self._receive_loop()

    async def _scan_until_locked(self, wlan) -> bool:
        """Run the auto-scan state machine.

        Returns True if a channel was locked normally (a valid frame
        arrived). Returns False if channel-set is rejected by the
        platform - the caller should then drop into receive without
        having locked a specific channel.
        """
        listen_ms = self._scanner.listen_ms
        poll_ms = 50
        self._status = "scanning"

        while not self._scanner.is_locked:
            ch = self._scanner.current_channel
            try:
                wlan.config(channel=ch)
            except Exception as exc:
                # Tildagon's networking layer can reject channel changes
                # on STA_IF with a non-OSError exception (observed:
                # RuntimeError: Wifi Unknown Error 0xffffffff). Treat any
                # failure as "this badge does not let us steer the radio"
                # and fall back to receive on whichever channel was last
                # successfully set (often the first scan target).
                self._status = "ch %d err" % ch
                print("[nocturnation] wlan.config(channel=%d) failed: %s" % (ch, exc))
                return False

            # Channel set succeeded - remember it so the fallback path
            # can tell the operator which channel to align to.
            self._receive_channel = ch

            print("[nocturnation] scanning channel %d for %d ms" % (ch, listen_ms))
            elapsed = 0
            while elapsed < listen_ms:
                buf = self._try_recv()
                if buf is not None:
                    frame = process_frame(buf, self._dedup)
                    if frame is not None:
                        self._observe_frame(frame)
                        print("[nocturnation] locking channel %d" % ch)
                        self._scanner.lock()
                        self._status = "locked"
                        return True
                await asyncio.sleep_ms(poll_ms)
                elapsed += poll_ms

            self._scanner.advance()
        return True

    async def _receive_loop(self) -> None:
        # Poll cadence: fast when foreground (responsive to incoming
        # LIGHT_COMMAND), idle when backgrounded (we are not driving
        # LEDs anyway, so we conserve CPU).
        poll_ms_fg = 5
        poll_ms_bg = 100
        while True:
            if not self._is_foreground:
                # Drain the espnow buffer so it does not fill while
                # we're backgrounded, but do not act on the contents.
                self._try_recv()
                await asyncio.sleep_ms(poll_ms_bg)
                continue
            buf = self._try_recv()
            if buf is not None:
                frame = process_frame(buf, self._dedup)
                if frame is not None:
                    self._observe_frame(frame)
                    if frame.message_type == MessageType.LIGHT_COMMAND:
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
            await asyncio.sleep_ms(poll_ms_fg)

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

    def _observe_frame(self, frame) -> None:
        self._frame_count += 1
        self._last_frame = frame
        # Non-light-command frames just bump the counter (they reach
        # us so the radio is alive, but there's nothing to render).
        if frame.message_type != MessageType.LIGHT_COMMAND or time is None:
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
        now_ms = time.ticks_ms()
        cls = frame.target_class
        if cls in _PERIMETER_CLASSES:
            self._renderer.dispatch(frame, now_ms)
        if cls in _LCD_CLASSES:
            self._lcd_renderer.dispatch(frame, now_ms)

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
