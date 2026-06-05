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

# os.read() with sys.stdin.fileno() gives us raw bytes regardless of
# MicroPython's text-mode I/O layer (which on the Tildagon filters
# non-printable bytes and breaks the Enttec framing). Cache the fd
# once at module load if both pieces work.
try:
    import os
    _STDIN_FD = sys.stdin.fileno()
    _HAS_OS_READ = hasattr(os, "read")
except Exception:
    _STDIN_FD = None
    _HAS_OS_READ = False

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
        # Read-path diagnostics: which API is delivering bytes, what
        # the bytes look like, how many 0x7E start markers we've seen.
        # Surfaces in the diagnostics view to help debug text-mode /
        # NULL-truncation issues on first MicroPython contact.
        self._read_path = "?"      # "buffer" | "text" | "?"
        self._last_bytes_hex = ""  # hex dump of the most recent chunk
        self._start_bytes_seen = 0 # count of 0x7E bytes in stream

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
            # Diagnostic: keep a hex dump of the most recent chunk so
            # the operator can eyeball what's actually arriving.
            try:
                if isinstance(raw, str):
                    sample = raw[-16:]
                    self._last_bytes_hex = " ".join(
                        "%02X" % ord(c) for c in sample)
                else:
                    sample = raw[-16:]
                    self._last_bytes_hex = " ".join(
                        "%02X" % b for b in sample)
            except Exception:
                pass
            for b in raw:
                if isinstance(b, str):    # text-mode read
                    b = ord(b) & 0xFF
                else:
                    b = b & 0xFF
                if b == 0x7E:
                    self._start_bytes_seen += 1
                if self._parser.feed_byte(b) == FRAME_COMPLETE:
                    self._on_frame(ctx, now_ms)

    def _read_chunk(self, stream):
        """Read whatever bytes are available without blocking.

        Three paths tried in order of preference:
          1. os.read(stdin_fd, 512)        - raw bytes via POSIX read,
                                             bypasses MicroPython's
                                             text-mode I/O. THIS is
                                             the one that actually
                                             works on Tildagon's
                                             MicroPython USB-CDC.
          2. stream.buffer.read(512)       - binary read via the
                                             .buffer attribute, works
                                             on CPython 3 and some
                                             MicroPython builds.
          3. stream.read(512)              - text-mode fallback;
                                             filters non-printable
                                             bytes on MicroPython
                                             (broken for binary).

        Records which path returned data in self._read_path so the
        operator can see what's happening on screen.
        """
        # Path 1: os.read on the stdin fd. Skip if the module-level
        # probe found neither os.read nor a valid fd.
        if _HAS_OS_READ and _STDIN_FD is not None:
            try:
                data = os.read(_STDIN_FD, 512)
                if data:
                    self._read_path = "os.read"
                    return data
            except OSError:
                # EAGAIN-ish: no data right now.
                pass
            except Exception as e:
                self._error = "os.read: " + repr(e)
        # Path 2: stream.buffer.
        try:
            buf = stream.buffer
            data = buf.read(512)
            if data:
                self._read_path = "buffer"
                return data
        except AttributeError:
            pass
        except Exception:
            pass
        # Path 3: text-mode fallback.
        try:
            data = stream.read(512)
            if data:
                self._read_path = "text"
            return data
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
        # No view toggle - the combined view is always on. Crashes on
        # toggle pointed at this hook being entangled with framework
        # behaviour. Director-level shortcuts (PICKER / SETTINGS /
        # PAUSE) are handled upstream and don't reach here. Showing
        # this stub for clarity; deliberately a no-op.
        pass

    # ------------------------------------------------------------------
    # Rendering: status view + diagnostics view
    # ------------------------------------------------------------------

    def on_render(self, ctx):
        # Combined status + diagnostics view - no toggle. Two crashes
        # of the toggle path on first contact, so simplify by always
        # showing the load-bearing info inline. on_input_action no
        # longer needs to react to button presses for view switching.
        try:
            d = ctx.display()
            if d is None:
                return
            d.clear(0, 0, 0)
            self._render_combined(d, ctx)
        except Exception:
            # Render failure - leave the screen as-is (the framework
            # will keep showing the previous frame). Catching here so
            # a single bad draw doesn't propagate up and kill the app.
            pass

    def _render_combined(self, d, ctx):
        """Single view - status + diagnostics inline. No view toggle.

        Five lines total, all within the -75..+75 safe range. Each
        keeps to a single d.text() call so failures stay isolated.
        """
        # Truncate the hex line to 8 bytes (23 chars) to fit the
        # screen width at size 10.
        hex_str = self._last_bytes_hex
        if len(hex_str) > 23:
            hex_str = hex_str[:23]
        d.text(0, -75, "DMX Bridge", size=14, r=255, g=255, b=255)
        d.text(0, -45, "bytes:  %d" % self._byte_count,
               size=12, r=255, g=255, b=255)
        d.text(0, -25, "frames: %d" % self._frames_received,
               size=12, r=255, g=255, b=255)
        d.text(0,  -5, "0x7E:   %d  (%s)"
               % (self._start_bytes_seen, self._read_path),
               size=10, r=200, g=200, b=200)
        d.text(0,  25, hex_str if hex_str else "(no data)",
               size=10, r=255, g=255, b=160)
        if self._error:
            d.text(0, 60, self._error[:24], size=10, r=255, g=80, b=80)


def make_show():
    return DmxBridge()
