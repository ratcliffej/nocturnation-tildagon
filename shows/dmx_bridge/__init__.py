# SPDX-License-Identifier: MIT
"""DMX Bridge - external DMX (via the laptop shim) replaces the
Director's local creative role.

Reads Enttec DMX USB Pro framing from USB-CDC (sys.stdin in MicroPython
on the Tildagon), maps the 12-channel NocturNation fixture layout to
LIGHT_PULSE / LIGHT_WASH events, broadcasts those events to the Lume
fleet via the host's render_fx / render_wash dispatch (which routes
through ESP-NOW). The shim
(nocturnation-docs/tools/artnet-to-enttec-pro.py) is the upstream
feeder; QLC+ (or any Art-Net-capable console) is what the operator
drives.

This is not strictly a Show in the architecture spec §1.2 sense - it's
"external input source replaces the Show role" mode. It plugs into the
Show framework because the Director's picker overlay already has the
right shape for selecting between active behaviours (Q4 of Epic 7).

UI per Q9 is deliberately minimal:
  * Status view (default) - connection state, counts, rate.
  * Diagnostics view - 12-row hex/value dump of the most recent frame.
  * Back-gesture exits to the Director picker.
No other inputs are accepted so an operator can't accidentally derail
a live show.

The bridge depends on the Director's existing WiFi-stop-on-mode-entry
mechanism (Epic 6B B9): the app's _acquire_radio() runs wifi.stop()
before entering any Director-mode Show, including this one, so the
ESP-NOW radio is free.
"""

import sys

try:
    # MicroPython exposes `select` as a top-level module; CPython has
    # it too but the API surface is narrower. We use poll() which is
    # present in both.
    import select
    _HAS_SELECT = True
except ImportError:
    _HAS_SELECT = False

from nocturnation.shows import Show, InputAction
from nocturnation.hal import CapabilityMask
from nocturnation.plugins import PluginKind, PowerProfile
from nocturnation.render import RgbPulse, RgbWash
from nocturnation.director.dmx_parser import DmxParser, FRAME_COMPLETE
from nocturnation.director.dmx_channel_mapper import (
    DmxChannelMapper,
    EVENT_PULSE,
    EVENT_WASH,
)


# Render target: broadcast to all Light-class Lumes regardless of
# group. Matches the StickC DMX Bridge default per Epic 7 Q4. Class
# "01" = Light, group "00" = wildcard.
_BROADCAST_TARGET = "01:00"

# Frame-aged-out threshold for the "connected" indicator. If we
# haven't seen a frame in this many ms, the status view flips back to
# "waiting...".
_STALE_MS = 500


class DmxBridge(Show):
    """USB-CDC -> ESP-NOW DMX bridge Show. v1 fixed-source (USB only);
    the GPIO source option is captured in Epic 7 B8 but not wired here
    yet."""

    def __init__(self):
        # Re-init in enter(); these are placeholders so static analysis
        # doesn't complain.
        self._parser = None
        self._mapper = None
        self._poll = None
        # UI state.
        self._view_diagnostics = False
        # Stats.
        self._byte_count = 0
        self._frames_received = 0
        self._pulses_sent = 0
        self._washes_sent = 0
        self._last_frame_ms = 0
        self._last_payload = b""
        self._error = ""

    # ------------------------------------------------------------------
    # Plugin identity
    # ------------------------------------------------------------------

    def id(self):
        return "dmx-bridge"

    def display_name(self):
        return "DMX Bridge"

    def kind(self):
        return PluginKind.SHOW

    def required_capabilities(self):
        return CapabilityMask()

    def properties(self):
        return ()

    def power(self):
        # 50 Hz tick to drain USB-CDC promptly. Shim sends ~44 Hz of
        # frames; 20 ms ticks give us comfortable margin without
        # burning too much CPU. LCD refresh is slower (10 Hz) - the
        # status view only needs that to look live.
        return PowerProfile(
            needs_audio_frames=False,
            tick_hz=50,
            lcd_refresh_hz_max=10,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def enter(self, ctx):
        self._parser = DmxParser()
        self._mapper = DmxChannelMapper()
        self._byte_count = 0
        self._frames_received = 0
        self._pulses_sent = 0
        self._washes_sent = 0
        self._last_frame_ms = 0
        self._last_payload = b""
        self._view_diagnostics = False
        self._error = ""
        self._poll = None
        if not _HAS_SELECT:
            self._error = "select module not available"
            return
        try:
            self._poll = select.poll()
            self._poll.register(sys.stdin, select.POLLIN)
        except Exception as e:
            self._poll = None
            self._error = "poll setup: " + repr(e)

    def exit(self, ctx):
        # Drop Lumes cleanly with a 1 s release fade.
        try:
            ctx.render_wash_end(_BROADCAST_TARGET, 10)
        except Exception:
            pass
        self._poll = None

    # ------------------------------------------------------------------
    # Tick: drain USB-CDC, parse, dispatch
    # ------------------------------------------------------------------

    def tick(self, ctx, now_ms):
        if self._poll is None:
            return
        try:
            events = self._poll.poll(0)   # non-blocking
        except Exception as e:
            self._error = "poll: " + repr(e)
            return
        for stream, mask in events:
            if not (mask & select.POLLIN):
                continue
            raw = self._read_chunk(stream)
            if not raw:
                continue
            self._byte_count += len(raw)
            for b in raw:
                if isinstance(b, str):    # CPython quirk
                    b = ord(b)
                if self._parser.feed_byte(b) == FRAME_COMPLETE:
                    self._on_frame(ctx, now_ms)

    @staticmethod
    def _read_chunk(stream):
        """Read whatever bytes are available without blocking.

        MicroPython's sys.stdin is usually text-mode; .buffer.read()
        gives raw bytes when present. Fall back to ord()-of-string
        when not.
        """
        try:
            buf = stream.buffer
            return buf.read(512)
        except AttributeError:
            pass
        try:
            return stream.read(512)
        except Exception:
            return None

    def _on_frame(self, ctx, now_ms):
        self._frames_received += 1
        self._last_frame_ms = now_ms
        payload = self._parser.last_payload()
        self._last_payload = payload
        events = self._mapper.process(payload, now_ms)
        for evt_type, body in events:
            if evt_type == EVENT_PULSE:
                r, g, b = body
                try:
                    ctx.render_fx(_BROADCAST_TARGET, RgbPulse(r, g, b))
                    self._pulses_sent += 1
                except Exception as e:
                    self._error = "pulse: " + repr(e)
            elif evt_type == EVENT_WASH:
                ar, ag, ab, br, bg, bb = body
                try:
                    ctx.render_wash(
                        _BROADCAST_TARGET,
                        RgbWash(ar, ag, ab,
                                br, bg, bb,
                                cycle_ms=0,
                                pulse_response=1),
                    )
                    self._washes_sent += 1
                except Exception as e:
                    self._error = "wash: " + repr(e)

    # ------------------------------------------------------------------
    # Input handling: minimal per Q9 (diagnostics toggle + back-gesture)
    # ------------------------------------------------------------------

    def on_input_action(self, ctx, action):
        # The Director's picker / settings shortcuts (PICKER / SETTINGS
        # / PAUSE) are handled upstream by the controller. We only
        # toggle the diagnostics view here. CONFIRM and CYCLE_RIGHT
        # both flip the view - whichever is most natural on the
        # operator's button layout.
        if action in (InputAction.CONFIRM, InputAction.CYCLE_RIGHT,
                      InputAction.CYCLE_LEFT):
            self._view_diagnostics = not self._view_diagnostics

    # ------------------------------------------------------------------
    # Rendering: status view + diagnostics view
    # ------------------------------------------------------------------

    def on_render(self, ctx):
        # Get the Tildagon's draw context. The shape of `ctx.lcd()` may
        # vary by framework version; wrap in a try so a draw failure
        # doesn't crash the Show.
        try:
            lcd = ctx.lcd()
            gctx = lcd.gctx()
        except Exception:
            return
        try:
            gctx.save()
            gctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
            if self._view_diagnostics:
                self._render_diagnostics(gctx, ctx)
            else:
                self._render_status(gctx, ctx)
            gctx.restore()
        except Exception:
            pass

    def _render_status(self, gctx, ctx):
        now_ms = ctx.now_ms() if hasattr(ctx, "now_ms") else 0
        connected = (self._frames_received > 0
                     and now_ms - self._last_frame_ms < _STALE_MS)
        gctx.rgb(1, 1, 1).move_to(-110, -90).text("DMX Bridge (USB)")
        if connected:
            gctx.rgb(0, 1, 0).move_to(-110, -60).text("CONNECTED")
        else:
            gctx.rgb(1, 0.5, 0).move_to(-110, -60).text("waiting...")
        gctx.rgb(1, 1, 1)
        gctx.move_to(-110, -30).text("frames: %d" % self._frames_received)
        gctx.move_to(-110, -10).text("bytes : %d" % self._byte_count)
        gctx.move_to(-110,  10).text("pulses: %d" % self._pulses_sent)
        gctx.move_to(-110,  30).text("washes: %d" % self._washes_sent)
        if self._error:
            gctx.rgb(1, 0, 0).move_to(-110, 60).text(self._error[:30])
        gctx.rgb(0.5, 0.5, 0.5).move_to(-110, 90).text("press to toggle diag")

    def _render_diagnostics(self, gctx, ctx):
        gctx.rgb(1, 1, 1).move_to(-110, -100).text("DMX channels (last)")
        if not self._last_payload:
            gctx.rgb(0.6, 0.6, 0.6).move_to(-110, -70).text("no data yet")
            return
        labels = ("Master", "Strobe", "PulseR", "PulseG", "PulseB",
                  "PulseT", "WashAR", "WashAG", "WashAB",
                  "WashBR", "WashBG", "WashBB")
        for i, label in enumerate(labels):
            if i >= len(self._last_payload):
                break
            val = self._last_payload[i]
            y = -80 + i * 14
            gctx.rgb(1, 1, 1).move_to(-115, y).text(
                "%02d %s %3d" % (i + 1, label, val))


def make_show():
    return DmxBridge()
