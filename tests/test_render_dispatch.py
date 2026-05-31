"""RenderDispatcher + parse_target tests.

The dispatcher mirrors the M5 dispatch_output_class_group: one
render_fx call fans to ESP-NOW broadcast + perimeter loopback + LCD
loopback, the latter two gated on target_class.
"""

import pytest

from nocturnation.director import RenderDispatcher, DispatchResult, parse_target
from nocturnation.render import RgbPulse, RgbWash, PerimeterRenderer, LcdRenderer
from nocturnation.protocol import parse_frame
from nocturnation.protocol.constants import DeviceClass, MessageType, Time, Chance


def _always_fire_perimeter():
    # rng=lambda:0.0 means every chance gate passes (0.0 < prob), so a
    # CHANCE_100 fire lights all 12 LEDs deterministically.
    return PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)


def _full_lcd():
    # Full mode so the LCD actually arms (Calm Mode disables it).
    return LcdRenderer(calm_mode=False)


def _pulse():
    return RgbPulse(255, 128, 0,
                    attack=Time.T_0_MS, sustain=Time.T_96_MS,
                    release=Time.T_480_MS, chance=Chance.CHANCE_100)


class TestParseTarget:
    def test_broadcast(self):
        assert parse_target("00:00") == (0, 0)

    def test_light_group_1(self):
        assert parse_target("01:01") == (1, 1)

    def test_two_hex_digits(self):
        assert parse_target("ff:ff") == (255, 255)
        assert parse_target("0a:1f") == (10, 31)

    def test_uppercase_hex(self):
        assert parse_target("FF:AB") == (255, 171)

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            parse_target(b"01:01")

    def test_rejects_missing_colon(self):
        with pytest.raises(ValueError):
            parse_target("0101")

    def test_rejects_three_parts(self):
        with pytest.raises(ValueError):
            parse_target("01:02:03")

    def test_rejects_non_hex(self):
        with pytest.raises(ValueError):
            parse_target("zz:01")

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            parse_target("100:00")


class TestBroadcast:
    def test_send_fn_receives_valid_frame(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append, source_id=0x42)
        d.dispatch("01:01", _pulse(), now_ms=1000)
        assert len(sent) == 1
        f = parse_frame(sent[0])
        assert f.source_id == 0x42
        assert f.target_class == DeviceClass.LIGHT
        assert f.target_group == 1
        assert (f.r, f.g, f.b) == (255, 128, 0)

    def test_broadcast_always_fires_even_for_screen_only(self):
        # Even a Screen-class target broadcasts (remote Light Lumes
        # filter it out themselves); the wire is class-agnostic.
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d.dispatch("02:00", _pulse(), now_ms=0)
        assert len(sent) == 1

    def test_sequence_increments_and_wraps(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        for _ in range(3):
            d.dispatch("00:00", _pulse(), now_ms=0)
        seqs = [parse_frame(b).sequence_number for b in sent]
        assert seqs == [0, 1, 2]

    def test_sequence_wraps_at_256(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d._sequence = 255
        d.dispatch("00:00", _pulse(), now_ms=0)
        d.dispatch("00:00", _pulse(), now_ms=0)
        seqs = [parse_frame(b).sequence_number for b in sent]
        assert seqs == [255, 0]

    def test_send_failure_is_swallowed(self):
        def boom(_payload):
            raise OSError("radio gone")
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=boom, perimeter=perimeter)
        result = d.dispatch("01:00", _pulse(), now_ms=0)
        # Broadcast failed but the local loopback still ran.
        assert result.sent is False
        assert result.perimeter_lit == 12

    def test_redundancy_repeats_same_sequence(self):
        # redundancy=3 -> each dispatch broadcasts the frame 3x with the
        # SAME sequence number (the receiver dedups). Reliability without
        # double-rendering.
        sent = []
        d = RenderDispatcher(send_fn=sent.append, redundancy=3)
        d.dispatch("01:00", _pulse(), now_ms=0)
        assert len(sent) == 3
        seqs = [parse_frame(b).sequence_number for b in sent]
        assert seqs == [0, 0, 0]
        # Next dispatch advances the sequence once, then repeats it.
        d.dispatch("01:00", _pulse(), now_ms=0)
        seqs2 = [parse_frame(b).sequence_number for b in sent]
        assert seqs2 == [0, 0, 0, 1, 1, 1]

    def test_no_send_fn_local_only(self):
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=None, perimeter=perimeter)
        result = d.dispatch("01:00", _pulse(), now_ms=0)
        assert result.sent is False
        assert result.perimeter_lit == 12
        assert bool(result) is True  # local effect makes it truthy


class TestLoopbackClassGating:
    def test_all_class_drives_both_surfaces(self):
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch("00:00", _pulse(), now_ms=0)
        assert result.perimeter_lit == 12
        assert result.lcd_armed is True

    def test_light_class_drives_perimeter_only(self):
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch("01:00", _pulse(), now_ms=0)
        assert result.perimeter_lit == 12
        assert result.lcd_armed is False

    def test_screen_class_drives_lcd_only(self):
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch("02:00", _pulse(), now_ms=0)
        assert result.perimeter_lit == 0
        assert result.lcd_armed is True

    def test_multiledscreen_class_drives_both(self):
        # 0x03 is the Tildagon's own class - it should loop back on
        # both surfaces, matching the receive-side routing.
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch("03:00", _pulse(), now_ms=0)
        assert result.perimeter_lit == 12
        assert result.lcd_armed is True

    def test_unrelated_class_drives_neither(self):
        # A reserved class (0x04) addresses neither local surface, but
        # still broadcasts for any future device that claims it.
        sent = []
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=sent.append, perimeter=perimeter, lcd=lcd)
        result = d.dispatch("04:00", _pulse(), now_ms=0)
        assert result.perimeter_lit == 0
        assert result.lcd_armed is False
        assert result.sent is True
        assert len(sent) == 1


class TestLoopbackRespectsRendererCaps:
    def test_calm_lcd_never_arms(self):
        # Calm Mode LCD is disabled; loopback to Screen does nothing
        # on the LCD even though the class matches.
        lcd = LcdRenderer(calm_mode=True)
        d = RenderDispatcher(send_fn=lambda p: None, lcd=lcd)
        result = d.dispatch("00:00", _pulse(), now_ms=0)
        assert result.lcd_armed is False

    def test_perimeter_frequency_cap_applies(self):
        # Two fires inside the Calm-mode 500 ms window: the second is
        # rate-limited to zero lit LEDs.
        perimeter = PerimeterRenderer(calm_mode=True, rng=lambda: 0.0)
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        first = d.dispatch("01:00", _pulse(), now_ms=0)
        second = d.dispatch("01:00", _pulse(), now_ms=100)
        assert first.perimeter_lit == 12
        assert second.perimeter_lit == 0


class TestHeartbeat:
    def test_beacons_immediately_when_nothing_sent(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append, source_id=0x20)
        assert d.heartbeat_tick(0) is True
        assert len(sent) == 1
        f = parse_frame(sent[0])
        assert f.message_type == MessageType.HEARTBEAT
        assert f.source_id == 0x20

    def test_skips_within_interval(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d.heartbeat_tick(0)        # beacon
        assert d.heartbeat_tick(500) is False   # < 1000 ms
        assert len(sent) == 1

    def test_beacons_again_after_interval(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d.heartbeat_tick(0)
        assert d.heartbeat_tick(1000) is True
        assert len(sent) == 2

    def test_recent_light_pulse_suppresses_heartbeat(self):
        # Skip-if-recent: a tap broadcast resets the timer, so no
        # heartbeat is needed for the next second.
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d.dispatch("01:00", _pulse(), now_ms=0)   # LIGHT_PULSE
        assert d.heartbeat_tick(500) is False
        # ...but once the gap exceeds the interval, the beacon resumes.
        assert d.heartbeat_tick(1001) is True

    def test_no_heartbeat_without_send_fn(self):
        d = RenderDispatcher(send_fn=None)
        assert d.heartbeat_tick(0) is False

    def test_heartbeat_does_not_loop_back_locally(self):
        # A heartbeat is a beacon, not a light - it must not arm LEDs.
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        d.heartbeat_tick(0)
        lit = sum(1 for i in range(1, 13) if perimeter._envelopes[i] is not None)
        assert lit == 0

    def test_shares_sequence_counter_with_light(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d.dispatch("01:00", _pulse(), now_ms=0)   # seq 0
        d.heartbeat_tick(2000)                      # seq 1
        seqs = [parse_frame(b).sequence_number for b in sent]
        assert seqs == [0, 1]


class TestDispatchResult:
    def test_truthy_when_sent(self):
        assert bool(DispatchResult(sent=True, perimeter_lit=0, lcd_armed=False)) is True

    def test_truthy_when_perimeter_lit(self):
        assert bool(DispatchResult(sent=False, perimeter_lit=3, lcd_armed=False)) is True

    def test_truthy_when_lcd_armed(self):
        assert bool(DispatchResult(sent=False, perimeter_lit=0, lcd_armed=True)) is True

    def test_falsy_when_nothing_happened(self):
        assert bool(DispatchResult(sent=False, perimeter_lit=0, lcd_armed=False)) is False


def _wash():
    return RgbWash(
        r1=255, g1=140, b1=30,
        r2=120, g2=30,  b2=200,
        attack=20, release=10,
        intensity=200, cycle_ms=5000,
        ttl_seconds=0, pulse_response=1,
    )


class TestDispatchWash:
    def test_send_fn_receives_valid_wash_frame(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append, source_id=0x42)
        d.dispatch_wash("01:00", _wash(), now_ms=1000)
        assert len(sent) == 1
        f = parse_frame(sent[0])
        assert f.message_type == MessageType.LIGHT_WASH
        assert f.source_id == 0x42
        assert f.target_class == DeviceClass.LIGHT
        assert f.target_group == 0
        assert (f.r1, f.g1, f.b1) == (255, 140, 30)
        assert (f.r2, f.g2, f.b2) == (120, 30, 200)
        assert f.wash_attack == 20
        assert f.wash_release == 10
        assert f.intensity == 200
        assert f.cycle_ms == 5000
        assert f.ttl_seconds == 0
        assert f.pulse_response == 1

    def test_perimeter_loopback_enters_wash(self):
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        result = d.dispatch_wash("01:00", _wash(), now_ms=0)
        assert result.perimeter_lit == 1
        assert perimeter.is_washing() is True

    def test_lcd_loopback_enters_wash(self):
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, lcd=lcd)
        result = d.dispatch_wash("02:00", _wash(), now_ms=0)
        assert result.lcd_armed is True
        assert lcd.is_washing() is True

    def test_class_gating_screen_only(self):
        # A Screen-class wash should arm LCD but not perimeter.
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch_wash("02:00", _wash(), now_ms=0)
        assert result.perimeter_lit == 0
        assert result.lcd_armed is True
        assert perimeter.is_washing() is False
        assert lcd.is_washing() is True

    def test_class_gating_light_only(self):
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch_wash("01:00", _wash(), now_ms=0)
        assert result.perimeter_lit == 1
        assert result.lcd_armed is False
        assert perimeter.is_washing() is True
        assert lcd.is_washing() is False

    def test_all_class_drives_both_surfaces(self):
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        result = d.dispatch_wash("00:00", _wash(), now_ms=0)
        assert result.perimeter_lit == 1
        assert result.lcd_armed is True

    def test_sequence_shared_with_pulse(self):
        # Wash and pulse share the dispatcher's sequence counter; this
        # matches the M5 side and lets the receiver dedup uniformly.
        sent = []
        d = RenderDispatcher(send_fn=sent.append)
        d.dispatch("01:00", _pulse(), now_ms=0)         # seq 0
        d.dispatch_wash("01:00", _wash(), now_ms=0)     # seq 1
        d.dispatch("01:00", _pulse(), now_ms=0)         # seq 2
        seqs = [parse_frame(b).sequence_number for b in sent]
        assert seqs == [0, 1, 2]

    def test_redundancy_applies_to_wash(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append, redundancy=3)
        d.dispatch_wash("01:00", _wash(), now_ms=0)
        assert len(sent) == 3
        # All three carry the same sequence (receiver dedups).
        seqs = [parse_frame(b).sequence_number for b in sent]
        assert seqs == [0, 0, 0]

    def test_send_failure_is_swallowed_local_loopback_still_runs(self):
        def boom(_p):
            raise OSError("radio gone")
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=boom, perimeter=perimeter)
        result = d.dispatch_wash("01:00", _wash(), now_ms=0)
        assert result.sent is False
        assert perimeter.is_washing() is True


class TestDispatchWashEnd:
    def test_send_fn_receives_valid_wash_end_frame(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append, source_id=0x42)
        d.dispatch_wash_end("01:00", release_time=15, now_ms=0)
        assert len(sent) == 1
        f = parse_frame(sent[0])
        assert f.message_type == MessageType.LIGHT_WASH_END
        assert f.source_id == 0x42
        assert f.target_class == DeviceClass.LIGHT
        assert f.target_group == 0
        assert f.release_time == 15

    def test_local_loopback_cancels_active_wash(self):
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        d.dispatch_wash("01:00", _wash(), now_ms=0)
        assert perimeter.is_washing() is True
        d.dispatch_wash_end("01:00", release_time=10, now_ms=500)
        # After END the renderer should leave Attack/Hold -> Release.
        assert perimeter.is_washing() is False

    def test_class_gating_screen_only(self):
        perimeter = _always_fire_perimeter()
        lcd = _full_lcd()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter, lcd=lcd)
        d.dispatch_wash("00:00", _wash(), now_ms=0)
        # End only Screen - perimeter should still be washing.
        d.dispatch_wash_end("02:00", release_time=10, now_ms=500)
        assert perimeter.is_washing() is True


class TestDispatchWashPulse:
    def test_send_fn_receives_valid_wash_pulse_frame(self):
        sent = []
        d = RenderDispatcher(send_fn=sent.append, source_id=0x42)
        d.dispatch_wash_pulse("01:00", _pulse(), now_ms=0)
        assert len(sent) == 1
        f = parse_frame(sent[0])
        assert f.message_type == MessageType.LIGHT_WASH_PULSE
        assert f.source_id == 0x42
        assert (f.r, f.g, f.b) == (255, 128, 0)

    def test_wash_pulse_drops_on_non_washing_perimeter(self):
        # On the local loopback the perimeter is not washing yet, so the
        # wash-pulse overlay is intentionally dropped (matches the Lume
        # receive-side semantic from Phase G).
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        result = d.dispatch_wash_pulse("01:00", _pulse(), now_ms=0)
        assert result.perimeter_lit == 0

    def test_wash_pulse_fires_when_washing(self):
        perimeter = _always_fire_perimeter()
        d = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        d.dispatch_wash("01:00", _wash(), now_ms=0)
        # Now overlay a wash-pulse: should light all 12 LEDs.
        result = d.dispatch_wash_pulse("01:00", _pulse(), now_ms=100)
        assert result.perimeter_lit == 12
