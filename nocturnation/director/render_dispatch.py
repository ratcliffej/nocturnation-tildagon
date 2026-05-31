"""RenderDispatcher - the Director's render_fx fan-out.

Mirrors the M5 firmware's `dispatch_output_class_group` (see
src/dal/dal.cpp in nocturnation-m5 and the "What dispatch does for
you" section of docs/developing-shows.md). One `render_fx` call
fans to three sinks:

  1. ESP-NOW broadcast    - ALWAYS. Every Lume on the channel sees
                            the frame and applies its own class+group
                            routing.
  2. Perimeter LED loopback - when target_class is All (0x00), Light
                            (0x01), or MultiLedScreen (0x03). The
                            Tildagon Director is its own first Lume:
                            its perimeter ring is the local "Light"
                            surface.
  3. LCD loopback         - when target_class is All (0x00), Screen
                            (0x02), or MultiLedScreen (0x03).

The loopback is class-gated only, not group-gated: the Director
always sees its own output regardless of group, matching the M5
loopback (group filtering is a receive-side concern for *remote*
Lumes). The renderers apply their own Calm-Mode frequency / brightness
caps, so the dispatcher doesn't second-guess them.

The dispatcher is hardware-free: the ESP-NOW send is an injected
callable so the whole fan-out is host-testable. B6 wires the real
`espnow.send(broadcast_mac, ...)` via `espnow_sender.make_sender`.
"""

from ..protocol import (
    encode_light_pulse,
    encode_heartbeat,
    make_light_pulse_frame,
)
from ..protocol.constants import DeviceClass


# target_class values that drive each local surface. Mirrors the
# receive-side routing the Lume already uses (see app.py _observe_frame):
# perimeter accepts All/Light/MultiLedScreen, LCD accepts
# All/Screen/MultiLedScreen.
_PERIMETER_CLASSES = (DeviceClass.ALL, DeviceClass.LIGHT, DeviceClass.MULTI_LED_SCREEN)
_LCD_CLASSES = (DeviceClass.ALL, DeviceClass.SCREEN, DeviceClass.MULTI_LED_SCREEN)


def parse_target(target):
    """Parse a `"<hex_class>:<hex_group>"` target into (class, group).

    Both fields are 1-2 hex digits, 0x00..0xFF, separated by a single
    colon. Mirrors the Epic 4.65 structured-target format used by
    `render_fx` on M5.

        parse_target("00:00") -> (0, 0)      # broadcast everyone
        parse_target("01:01") -> (1, 1)      # Light class, group 1
        parse_target("ff:ff") -> (255, 255)

    Raises ValueError on a malformed target (a Show bug - surfaced
    loudly rather than silently mis-routing).
    """
    if not isinstance(target, str):
        raise ValueError("render_fx target must be a string, got %r" % (target,))
    parts = target.split(":")
    if len(parts) != 2:
        raise ValueError("render_fx target must be '<class>:<group>', got %r" % (target,))
    try:
        target_class = int(parts[0], 16)
        target_group = int(parts[1], 16)
    except ValueError:
        raise ValueError("render_fx target fields must be hex, got %r" % (target,))
    if not (0 <= target_class <= 0xFF) or not (0 <= target_group <= 0xFF):
        raise ValueError("render_fx target fields out of range 0x00-0xFF: %r" % (target,))
    return target_class, target_group


class DispatchResult:
    """Outcome of one render_fx fan-out.

    Truthy if anything happened (frame broadcast, or a local surface
    accepted the loopback). A render that was rate-limited on both
    local surfaces but still broadcast is truthy.
    """

    __slots__ = ("sent", "perimeter_lit", "lcd_armed")

    def __init__(self, sent, perimeter_lit, lcd_armed):
        self.sent = sent
        self.perimeter_lit = perimeter_lit
        self.lcd_armed = lcd_armed

    def __bool__(self):
        return self.sent or self.perimeter_lit > 0 or self.lcd_armed

    # MicroPython evaluates truthiness via __bool__; CPython too. Kept
    # explicit for clarity.
    __nonzero__ = __bool__

    def __repr__(self):
        return "DispatchResult(sent=%r, perimeter_lit=%r, lcd_armed=%r)" % (
            self.sent,
            self.perimeter_lit,
            self.lcd_armed,
        )


class RenderDispatcher:
    """Fan one render_fx call to the wire + the Director's own surfaces.

    Construct with:
      send_fn    - callable(payload_bytes) -> None. Broadcasts the
                   encoded frame. None disables the wire (local-only;
                   useful in tests). Exceptions from send_fn are
                   swallowed so a flaky radio doesn't kill the show -
                   the local loopback still runs.
      perimeter  - a PerimeterRenderer (or None to skip LED loopback).
      lcd        - an LcdRenderer (or None to skip LCD loopback).
      source_id  - the Director's source_id stamped into every frame.
                   Defaults to 0x01 (community range); the real value
                   is allocated per Epic 5.5 and injected by B6.
    """

    __slots__ = ("_send_fn", "_perimeter", "_lcd", "_source_id", "_sequence",
                 "_last_tx_ms", "_redundancy")

    def __init__(self, send_fn=None, perimeter=None, lcd=None, source_id=0x01,
                 redundancy=1):
        self._send_fn = send_fn
        self._perimeter = perimeter
        self._lcd = lcd
        self._source_id = source_id & 0xFF
        self._sequence = 0
        # How many times to repeat each LIGHT_PULSE broadcast. ESP-NOW
        # is fire-and-forget with no retransmission, so a single send is
        # lossy; the M5 master sends 3x for reliability and the receiver
        # dedups on (source_id, sequence). Default 1 keeps tests simple;
        # the app opts into 3. Heartbeats stay single (1 Hz is already
        # redundant over time).
        self._redundancy = max(1, redundancy)
        # Wall-clock of the last frame actually broadcast (any type).
        # Drives heartbeat skip-if-recent: a LIGHT_PULSE already
        # proves liveness, so a heartbeat is only sent to fill quiet
        # gaps. None = nothing sent yet (beacon immediately on entry).
        self._last_tx_ms = None

    @property
    def source_id(self):
        return self._source_id

    def set_source_id(self, source_id):
        """Update the Director's source_id (e.g. after Epic-5.5
        allocation completes). Takes effect on the next dispatch."""
        self._source_id = source_id & 0xFF

    def dispatch(self, target, ev, now_ms):
        """Encode + broadcast + locally loop back one render event.

        `target` is a `"<class>:<group>"` string; `ev` is an RgbPulse;
        `now_ms` is the Director clock. Returns a DispatchResult.

        Raises ValueError if `target` is malformed (Show bug).
        """
        target_class, target_group = parse_target(target)

        # 1. Encode + broadcast (always). Sequence wraps at 256; the
        #    dispatcher owns the counter.
        payload = encode_light_pulse(
            self._source_id,
            self._sequence,
            target_class,
            target_group,
            ev.r,
            ev.g,
            ev.b,
            ev.attack,
            ev.sustain,
            ev.release,
            ev.chance,
        )
        self._sequence = (self._sequence + 1) & 0xFF

        sent = False
        if self._send_fn is not None:
            # Repeat for reliability; the receiver dedups on
            # (source_id, sequence) so the repeats render once.
            for _ in range(self._redundancy):
                try:
                    self._send_fn(payload)
                    sent = True
                except Exception:
                    # Swallow radio errors: the show goes on locally,
                    # and the remaining repeats may still get through.
                    pass
            if sent:
                self._last_tx_ms = now_ms

        # 2. Local loopback. Build the Frame directly (no byte
        #    round-trip) and feed the renderers that match the class.
        perimeter_lit = 0
        lcd_armed = False
        if (self._perimeter is not None and target_class in _PERIMETER_CLASSES) or (
            self._lcd is not None and target_class in _LCD_CLASSES
        ):
            frame = make_light_pulse_frame(
                target_class,
                target_group,
                ev.r,
                ev.g,
                ev.b,
                ev.attack,
                ev.sustain,
                ev.release,
                ev.chance,
                source_id=self._source_id,
            )
            if self._perimeter is not None and target_class in _PERIMETER_CLASSES:
                perimeter_lit = self._perimeter.dispatch(frame, now_ms)
            if self._lcd is not None and target_class in _LCD_CLASSES:
                lcd_armed = self._lcd.dispatch(frame, now_ms)

        return DispatchResult(sent, perimeter_lit, lcd_armed)

    def heartbeat_tick(self, now_ms, interval_ms=1000):
        """Beacon a HEARTBEAT if no frame has gone out in `interval_ms`.

        Called every loop tick; self-throttles to ~1 Hz. Skip-if-recent:
        a LIGHT_PULSE broadcast already proves liveness, so a tapping
        Director needs no extra heartbeats - they only fill quiet gaps
        so a Lume can discover the channel and keep its TOFU lock.

        Heartbeats broadcast only (no local loopback - they're a beacon,
        not a light). Returns True if a heartbeat was sent this call.
        """
        if self._send_fn is None:
            return False
        if self._last_tx_ms is not None and (now_ms - self._last_tx_ms) < interval_ms:
            return False
        payload = encode_heartbeat(self._source_id, self._sequence, tick=now_ms)
        self._sequence = (self._sequence + 1) & 0xFF
        try:
            self._send_fn(payload)
        except Exception:
            return False
        self._last_tx_ms = now_ms
        return True
