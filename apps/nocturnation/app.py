"""NocturNation Tildagon receiver app.

Block 1 (shipped): minimal Tildagon OS app draws the brand-mark and
exits cleanly on CANCEL.

Block 2 (current): async background_task drives ESP-NOW receive with
channel auto-scan, deduplication, and hop-count enforcement per the
protocol manual. Received frames are counted and logged to serial;
rendering on the perimeter LEDs and LCD lands at Block 3.

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

    def update(self, delta: float) -> None:
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()

    def draw(self, ctx) -> None:
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
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
        poll_ms = 5
        while True:
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

    def _observe_frame(self, frame) -> None:
        self._frame_count += 1
        self._last_frame = frame


__app_export__ = NocturNationApp
