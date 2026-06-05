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
        # Use the same display API as the other Shows (Conductor /
        # motion_wave / simple_tap). ctx.display() returns a small
        # wrapper with .clear(r, g, b) and .text(x, y, ..., size=, r=,
        # g=, b=) methods; coordinates are centred on the screen.
        d = ctx.display()
        if d is None:
            return
        d.clear(0, 0, 0)
        if self._view_diagnostics:
            self._render_diagnostics(d)
        else:
            self._render_status(d, ctx)

    def _render_status(self, d, ctx):
        now_ms = ctx.now_ms() if hasattr(ctx, "now_ms") else 0
        connected = (self._frames_received > 0
                     and now_ms - self._last_frame_ms < _STALE_MS)
        d.text(0, -90, "DMX Bridge", size=16, r=255, g=255, b=255)
        d.text(0, -68, "(USB)",       size=10, r=160, g=160, b=160)
        if connected:
            d.text(0, -40, "CONNECTED", size=18, r=0,   g=255, b=0)
        else:
            d.text(0, -40, "waiting...", size=14, r=255, g=140, b=0)
        d.text(0, -10, "frames: %d" % self._frames_received,
               size=12, r=255, g=255, b=255)
        d.text(0,  8,  "bytes:  %d" % self._byte_count,
               size=12, r=255, g=255, b=255)
        d.text(0,  26, "pulses: %d" % self._pulses_sent,
               size=12, r=255, g=255, b=255)
        d.text(0,  44, "washes: %d" % self._washes_sent,
               size=12, r=255, g=255, b=255)
        if self._error:
            d.text(0, 68, self._error[:30], size=10, r=255, g=80, b=80)
        d.text(0, 92, "press button: diag", size=10, r=120, g=120, b=120)

    def _render_diagnostics(self, d):
        d.text(0, -100, "DMX channels", size=14, r=255, g=255, b=255)
        if not self._last_payload:
            d.text(0, -70, "no data yet", size=12, r=160, g=160, b=160)
            return
        labels = ("Master", "Strobe", "PulseR", "PulseG", "PulseB",
                  "PulseT", "WshAR", "WshAG", "WshAB",
                  "WshBR", "WshBG", "WshBB")
        for i, label in enumerate(labels):
            if i >= len(self._last_payload):
                break
            val = self._last_payload[i]
            y = -78 + i * 14
            d.text(0, y, "%02d %s %3d" % (i + 1, label, val),
                   size=10, r=255, g=255, b=255)


def make_show():
    return DmxBridge()
