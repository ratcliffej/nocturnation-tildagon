"""RenderDispatcher + parse_target tests.

The dispatcher mirrors the M5 dispatch_output_class_group: one
render_fx call fans to ESP-NOW broadcast + perimeter loopback + LCD
loopback, the latter two gated on target_class.
"""

import pytest

from nocturnation.director import RenderDispatcher, DispatchResult, parse_target
from nocturnation.render import RgbPulse, PerimeterRenderer, LcdRenderer
from nocturnation.protocol import parse_frame
from nocturnation.protocol.constants import DeviceClass, Time, Chance


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


class TestDispatchResult:
    def test_truthy_when_sent(self):
        assert bool(DispatchResult(sent=True, perimeter_lit=0, lcd_armed=False)) is True

    def test_truthy_when_perimeter_lit(self):
        assert bool(DispatchResult(sent=False, perimeter_lit=3, lcd_armed=False)) is True

    def test_truthy_when_lcd_armed(self):
        assert bool(DispatchResult(sent=False, perimeter_lit=0, lcd_armed=True)) is True

    def test_falsy_when_nothing_happened(self):
        assert bool(DispatchResult(sent=False, perimeter_lit=0, lcd_armed=False)) is False
