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
    # asyncio is the integration path that actually works on the
    # Tildagon (reference: benclifford/tildagon-usbserial-kbd, which
    # reads single chars from sys.stdin via asyncio.StreamReader).
    # The synchronous select.poll() + sys.stdin.read() pattern we
    # tried first fights the framework's I/O loop; asyncio shares
    # sys.stdin cooperatively with the REPL and the framework, and
    # the REPL no longer eats our bytes because we yield properly to
    # the scheduler.
    import asyncio
    _HAS_ASYNCIO = True
except ImportError:
    _HAS_ASYNCIO = False

# Legacy: select.poll() path was a dead-end on the Tildagon. Kept
# imported so the existing tick()-based read code compiles, but the
# async reader is what actually runs.
try:
    import select
    _HAS_SELECT = True
except ImportError:
    _HAS_SELECT = False

# binascii.unhexlify gives us bytes from ASCII hex. The shim ships
# frames hex-encoded so MicroPython's text-mode USB-CDC layer can't
# eat non-printable bytes - hex chars are all in [0-9a-fA-F] which
# survives any text-mode handling.
import binascii


# ============================================================================
# Debug log file
#
# Written to /dmx_bridge.log on the badge filesystem. The LCD diagnostics
# are limited by screen real estate; this gives us proper post-mortem
# visibility - pull it back via:
#
#   mpremote cat :dmx_bridge.log
#
# Capped at ~20 KB so a long-running session can't fill the badge flash.
# All writes wrapped in try/except so a logging failure can't crash the
# Show. Low-frequency by design: probe state at module load, the first
# 5 chunks verbatim, errors, periodic stats every 10 seconds.
# ============================================================================

_LOG_PATH = "/dmx_bridge.log"
_LOG_MAX_BYTES = 20000


class _LogFile:
    """Tiny size-capped append-only log writer."""

    def __init__(self, path):
        self._path = path
        self._f = None
        self._size = 0
        try:
            self._f = open(path, "w")
        except Exception:
            self._f = None

    def write(self, msg):
        if self._f is None or self._size >= _LOG_MAX_BYTES:
            return
        try:
            line = msg + "\n"
            self._f.write(line)
            self._f.flush()
            self._size += len(line)
        except Exception:
            self._f = None

    def close(self):
        if self._f is not None:
            try:
                self._f.close()
            except Exception:
                pass
            self._f = None


_log = _LogFile(_LOG_PATH)
_log.write("== dmx_bridge log start ==")

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

_log.write("probe: _HAS_OS_READ=%s _STDIN_FD=%r _OS_READ_PROBE=%r"
           % (_HAS_OS_READ, _STDIN_FD, _OS_READ_PROBE))
_log.write("probe: _HAS_SELECT=%s" % _HAS_SELECT)

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
        # Quote-delimited hex decoder: shim sends '#"<hex>"\n' per
        # frame. The # opens a Python comment (REPL no-ops), " is the
        # frame delimiter we use (\n gets eaten by the REPL but " is
        # left intact because the REPL only sees a comment). Bytearray
        # so .extend() / del [:n] stay in-place.
        self._line_buf = bytearray()
        self._in_quote = False      # state: inside the "..." quote
        self._frames_decoded = 0    # successful hex unhexlify count
        self._bad_lines = 0         # lines that failed unhexlify
        # Log file diagnostics.
        self._chunks_logged = 0    # so we log only the first N chunks
        self._next_stats_log_ms = 0
        # Async reader task handle - the canonical Tildagon pattern for
        # reading USB-CDC bytes. enter() spawns it via
        # asyncio.create_task(); exit() cancels it.
        self._reader_task = None

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
        self._in_quote = False
        self._frames_decoded = 0
        self._bad_lines = 0
        self._start_bytes_seen = 0
        self._chunks_logged = 0
        self._next_stats_log_ms = 0
        self._reader_task = None
        self._ctx = ctx     # async reader uses this to dispatch events
        _log.write("enter: stdin=%r" % sys.stdin)
        # Spawn the async reader (the only pattern bench-proven to work
        # on Tildagon's MicroPython - see module-level comment).
        if _HAS_ASYNCIO:
            try:
                self._reader_task = asyncio.create_task(self._read_loop())
                _log.write("async reader: task spawned")
            except Exception as e:
                _log.write("async reader: spawn failed: %r" % e)
        else:
            _log.write("async reader: asyncio not available")
    def exit(self, ctx):
        # Cancel the async reader so it stops draining sys.stdin.
        if self._reader_task is not None:
            try:
                self._reader_task.cancel()
                _log.write("async reader: cancelled")
            except Exception as e:
                _log.write("async reader: cancel failed: %r" % e)
            self._reader_task = None
        # Drop Lumes cleanly with a 1 s release fade.
        try:
            ctx.render_wash_end(_BROADCAST_TARGET, 10)
        except Exception:
            pass
        self._poll = None
        self._ctx = None

    # ------------------------------------------------------------------
    # Tick: drain USB-CDC, parse, dispatch
    # ------------------------------------------------------------------

    def tick(self, ctx, now_ms):
        """Per-tick housekeeping only - byte reading happens in the
        async _read_loop coroutine. Periodic stats log + (in the
        future) any per-tick UI updates that need 'now' from the
        framework go here.
        """
        if now_ms >= self._next_stats_log_ms:
            self._next_stats_log_ms = now_ms + 10000
            _log.write(
                "stats: bytes=%d decoded=%d bad=%d 0x7E=%d "
                "parsed=%d pulses=%d washes=%d path=%s buf_len=%d"
                % (self._byte_count, self._frames_decoded,
                   self._bad_lines, self._start_bytes_seen,
                   self._frames_received, self._pulses_sent,
                   self._washes_sent, self._read_path,
                   len(self._line_buf)))

    async def _read_loop(self):
        """Async byte reader. Spawned by enter(), cancelled by exit().

        Wraps sys.stdin in asyncio.StreamReader and pulls chunks
        cooperatively - this is the canonical Tildagon pattern (ref:
        benclifford/tildagon-usbserial-kbd). The await yields back to
        the framework's scheduler so the REPL and our reader share
        sys.stdin without the REPL eating our delimiters or hanging
        under volume.

        Decoded bytes get fed straight into the parser; complete
        frames trigger _on_frame which dispatches via the saved ctx.
        """
        try:
            sr = asyncio.StreamReader(sys.stdin)
            self._read_path = "asyncio"
            _log.write("read loop: started, sr=%r" % sr)
            import time as _time
            while True:
                try:
                    chunk = await sr.read(64)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._error = "sr.read: " + repr(e)
                    _log.write("read loop: read error: %r" % e)
                    await asyncio.sleep_ms(10)
                    continue
                if not chunk:
                    await asyncio.sleep_ms(1)
                    continue
                if isinstance(chunk, str):
                    raw = chunk.encode("latin-1")
                else:
                    raw = chunk
                self._byte_count += len(raw)
                self._on_chunk(raw, _time.ticks_ms())
        except asyncio.CancelledError:
            _log.write("read loop: cancelled cleanly")
            raise
        except Exception as e:
            _log.write("read loop: fatal: %r" % e)

    def _on_chunk(self, chunk, now_ms):
        """Decode a freshly-read chunk: append to line buffer, find
        completed frames, hex-decode each, feed to parser, dispatch
        events on FRAME_COMPLETE.
        """
        if self._ctx is None:
            return
        # Diagnostic: snapshot the first 16 bytes of THIS chunk
        # so the operator can see what's arriving on the cable.
        try:
            sample = bytes(chunk[:16])
            self._last_bytes_hex = "".join("%02x" % b for b in sample)
        except Exception:
            pass
        # Log the first 5 chunks verbatim with hex so we can see what
        # the cable is actually carrying. Cap so we don't spam flash.
        if self._chunks_logged < 5:
            self._chunks_logged += 1
            try:
                sample_hex = "".join("%02x" % b for b in chunk[:64])
                _log.write("chunk %d: len=%d first64=%s"
                           % (self._chunks_logged, len(chunk), sample_hex))
            except Exception:
                pass
        self._line_buf.extend(chunk)
        # Frame extraction. The shim wraps each frame in either:
        #   #"<hex>"\n      - python envelope, " is the delimiter
        #   <hex>;          - legacy hex envelope, ; is the delimiter
        # We accept both so the parser is robust to shim version drift.
        while True:
            if self._in_quote:
                q_idx = self._line_buf.find(b'"')
                if q_idx < 0:
                    break
                hex_line = bytes(self._line_buf[:q_idx])
                del self._line_buf[:q_idx + 1]
                self._in_quote = False
            else:
                open_q = self._line_buf.find(b'"')
                legacy_semi = self._line_buf.find(b";")
                if open_q < 0 and legacy_semi < 0:
                    break
                if 0 <= open_q and (legacy_semi < 0
                                    or open_q < legacy_semi):
                    del self._line_buf[:open_q + 1]
                    self._in_quote = True
                    continue
                hex_line = bytes(self._line_buf[:legacy_semi])
                del self._line_buf[:legacy_semi + 1]
                hex_line = hex_line.strip()
                while hex_line and hex_line[:1] == b"#":
                    hex_line = hex_line[1:].strip()

            hex_line = hex_line.strip()
            if not hex_line:
                continue
            if self._frames_decoded == 0:
                try:
                    _log.write("first line: len=%d head=%s"
                               % (len(hex_line),
                                  hex_line[:64].decode("ascii", "ignore")))
                except Exception:
                    pass
            try:
                frame_bytes = binascii.unhexlify(hex_line)
            except Exception as e:
                self._bad_lines += 1
                if self._bad_lines <= 3:
                    try:
                        _log.write("bad line %d: %r head=%s"
                                   % (self._bad_lines, e,
                                      hex_line[:32].decode("ascii", "ignore")))
                    except Exception:
                        pass
                continue
            self._frames_decoded += 1
            for b in frame_bytes:
                if b == 0x7E:
                    self._start_bytes_seen += 1
                if self._parser.feed_byte(b) == FRAME_COMPLETE:
                    self._on_frame(self._ctx, now_ms)

    # _read_chunk() and the sync read paths (os.read, stream.buffer,
    # stream.read) were removed - replaced by the async _read_loop()
    # above. See module-level comments for the bench history.

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
