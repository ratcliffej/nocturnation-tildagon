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

# binascii.unhexlify gives us bytes from ASCII hex. The shim ships
# frames hex-encoded so MicroPython's text-mode USB-CDC layer can't
# eat non-printable bytes - hex chars are all in [0-9a-fA-F] which
# survives any text-mode handling.
import binascii

# os.read() with sys.stdin.fileno() gives us raw bytes regardless of
# MicroPython's text-mode I/O layer (which on the Tildagon filters
# non-printable bytes and breaks the Enttec framing). Cache the fd
# once at module load if both pieces work. Record the probe outcome
# as a string so the operator can see on-screen WHY a path was
# skipped.
_OS_READ_PROBE = ""
try:
    import os
    _HAS_OS_READ = hasattr(os, "read")
    if not _HAS_OS_READ:
        _OS_READ_PROBE = "no os.read"
except Exception as _e:
    os = None
    _HAS_OS_READ = False
    _OS_READ_PROBE = "no os: " + repr(_e)[:16]

_STDIN_FD = None
if _HAS_OS_READ:
    try:
        _STDIN_FD = sys.stdin.fileno()
        if _STDIN_FD is None or _STDIN_FD < 0:
            _OS_READ_PROBE = "fd=%r" % _STDIN_FD
            _STDIN_FD = None
    except Exception as _e:
        _OS_READ_PROBE = "fileno: " + repr(_e)[:16]

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
        # the bytes look like, how many 0x7E start markers we've seen
        # AFTER hex decode (so it counts real frame starts).
        self._read_path = "?"      # "buffer" | "text" | "?"
        self._last_bytes_hex = ""  # hex dump of the most recent line
        self._start_bytes_seen = 0
        # Line-buffered hex decoder: shim sends "aabbcc...e7\n" per
        # frame. Use a bytearray so .extend() / del [:n] are in-place
        # and don't churn the GC.
        self._line_buf = bytearray()
        self._frames_decoded = 0   # successful hex unhexlify count
        self._bad_lines = 0        # lines that failed unhexlify

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
        # 25 Hz tick to drain USB-CDC; lower than the 50 Hz first cut
        # to ease MicroPython GC pressure after the bench observed
        # freeze at ~24 KB. LCD refresh stays slow (5 Hz). With the
        # parser's pre-allocated buffer + a single bytes object per
        # read, steady-state allocation is small.
        return PowerProfile(
            needs_audio_frames=False,
            tick_hz=25,
            lcd_refresh_hz_max=5,
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
        self._line_buf = bytearray()
        self._frames_decoded = 0
        self._bad_lines = 0
        self._start_bytes_seen = 0
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
            # Append the raw chars/bytes into the line buffer. Bytes
            # come in as either bytes (binary path) or str (text-mode
            # fallback); coerce to bytes for the buffer. Hex chars are
            # 7-bit ASCII so the conversion is lossless either way.
            if isinstance(raw, str):
                # Convert str -> bytes via latin-1 (one-to-one,
                # roundtrip-safe for any byte value)
                chunk = raw.encode("latin-1")
            else:
                chunk = raw
            # Diagnostic: snapshot the first 16 bytes of THIS chunk
            # immediately so we can see what's arriving regardless of
            # whether line boundaries are found. Previously the hex
            # display only updated on line completion - so a stream
            # with no delimiters showed "(no data)" even with bytes
            # actively flowing.
            try:
                sample = bytes(chunk[:16])
                self._last_bytes_hex = "".join(
                    "%02x" % b for b in sample)
            except Exception:
                pass
            self._line_buf.extend(chunk)
            # Pull off as many complete lines as we have. Each line is
            # one hex-encoded Enttec Pro frame terminated by `;`. (Was
            # `\n` - MicroPython text-mode reads strip newlines, so
            # the device-side line buffer never saw the delimiter.
            # `;` is non-newline ASCII outside the hex alphabet so
            # MicroPython's line handling can't eat it.)
            while True:
                sep_idx = self._line_buf.find(b";")
                if sep_idx < 0:
                    break
                line = bytes(self._line_buf[:sep_idx])
                # Mutate buffer in-place to drop the consumed line +
                # delimiter; no whole-buffer reallocation.
                del self._line_buf[:sep_idx + 1]
                # Strip whitespace (CR or stray spaces).
                line = line.strip()
                if not line:
                    continue
                # Hex-decode to raw frame bytes.
                try:
                    frame_bytes = binascii.unhexlify(line)
                except Exception:
                    self._bad_lines += 1
                    continue
                self._frames_decoded += 1
                # Feed the decoded binary bytes to the Enttec parser.
                for b in frame_bytes:
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

        Within the -75..+75 safe range used by Conductor. Each line
        is a single d.text() call so failures stay isolated.
        """
        hex_str = self._last_bytes_hex
        if len(hex_str) > 16:
            hex_str = hex_str[:16]
        d.text(0, -75, "DMX Bridge", size=14, r=255, g=255, b=255)
        d.text(0, -55, "bytes:  %d" % self._byte_count,
               size=10, r=255, g=255, b=255)
        # Hex-decoded frame count first (most informative single number
        # in the hex-protocol world); then completed parse count.
        d.text(0, -40, "decoded: %d  bad: %d"
               % (self._frames_decoded, self._bad_lines),
               size=10, r=255, g=255, b=255)
        d.text(0, -25, "parsed: %d  0x7E: %d"
               % (self._frames_received, self._start_bytes_seen),
               size=10, r=200, g=200, b=200)
        d.text(0, -10, "out: pulse:%d wash:%d"
               % (self._pulses_sent, self._washes_sent),
               size=10, r=200, g=200, b=200)
        d.text(0,   5, "path: %s" % self._read_path,
               size=10, r=160, g=160, b=160)
        # Show the first 16 chars of the last hex line. Should look
        # like "7e06010200000000" - if it doesn't, the cable isn't
        # carrying what we expect.
        d.text(0,  25, hex_str if hex_str else "(no data)",
               size=10, r=255, g=255, b=160)
        if self._error:
            d.text(0, 50, self._error[:24], size=10, r=255, g=80, b=80)


def make_show():
    return DmxBridge()
