"""Perimeter LED renderer tests.

The renderer is two-phase: dispatch arms envelopes (chance-gated per
LED), tick computes current brightness. Both run on the host; the
hardware drive happens via the set_led callback passed to tick.

Calm Mode (default): 2 Hz dispatch cap, 50 % brightness cap.
Full mode: 4 Hz dispatch cap, 100 % brightness cap.
"""

import pytest

from nocturnation.protocol.constants import Time, Chance
from nocturnation.render.perimeter import (
    PerimeterRenderer,
    LED_MIN_INDEX,
    LED_MAX_INDEX,
    LED_COUNT,
    CALM_MIN_INTERVAL_MS,
    FULL_MIN_INTERVAL_MS,
)


class FakeFrame:
    """Minimal stand-in for protocol.Frame with just the LIGHT_COMMAND fields
    the renderer reads. Real Frame objects work equally well but are noisier
    to construct in test cases."""

    __slots__ = ("r", "g", "b", "attack", "sustain", "release", "chance")

    def __init__(self, r=255, g=0, b=0,
                 attack=Time.T_32_MS, sustain=Time.T_96_MS, release=Time.T_96_MS,
                 chance=Chance.CHANCE_100):
        self.r = r
        self.g = g
        self.b = b
        self.attack = attack
        self.sustain = sustain
        self.release = release
        self.chance = chance


def make_capture():
    """Return (captured_list, set_led_fn). The fn appends (idx, r, g, b) tuples
    to captured_list when invoked."""
    captured = []
    def set_led(i, r, g, b):
        captured.append((i, r, g, b))
    return captured, set_led


def always_pass_rng():
    return 0.0  # always < any chance probability, so every LED arms


def always_fail_rng():
    return 0.999  # > every chance < CHANCE_100; falls through to no LEDs lit


class TestDispatchChance:
    def test_chance_100_lights_all_12_leds(self):
        r = PerimeterRenderer(rng=always_pass_rng)
        lit = r.dispatch(FakeFrame(chance=Chance.CHANCE_100), now_ms=0)
        assert lit == LED_COUNT == 12

    def test_chance_4_with_always_fail_rng_lights_none(self):
        # 0.999 > 0.04, so no LED rolls under threshold.
        r = PerimeterRenderer(rng=always_fail_rng)
        lit = r.dispatch(FakeFrame(chance=Chance.CHANCE_4), now_ms=0)
        assert lit == 0

    def test_chance_50_with_alternating_rng_lights_half(self):
        # Alternate 0.4 (pass for >= CHANCE_67 and below) and 0.6 (fail).
        seq = iter([0.4, 0.6] * 12)
        r = PerimeterRenderer(rng=lambda: next(seq))
        lit = r.dispatch(FakeFrame(chance=Chance.CHANCE_50), now_ms=0)
        assert lit == 6  # six 0.4 passes, six 0.6 fails


class TestPrimerAndZeroDuration:
    def test_primer_rgb_zero_is_no_op(self):
        r = PerimeterRenderer(rng=always_pass_rng)
        lit = r.dispatch(FakeFrame(r=0, g=0, b=0), now_ms=0)
        assert lit == 0

    def test_primer_does_not_consume_rate_limit(self):
        # If primers consumed the cap, the main fire would be rejected.
        # We require primer + main close together to both run cleanly.
        r = PerimeterRenderer(rng=always_pass_rng)
        r.dispatch(FakeFrame(r=0, g=0, b=0), now_ms=0)
        lit = r.dispatch(FakeFrame(r=255, g=0, b=0), now_ms=50)
        assert lit == LED_COUNT  # not rate-limited

    def test_zero_duration_envelope_is_no_op(self):
        r = PerimeterRenderer(rng=always_pass_rng)
        lit = r.dispatch(
            FakeFrame(attack=Time.T_0_MS, sustain=Time.T_0_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        assert lit == 0


class TestFrequencyCap:
    def test_calm_mode_cap_500ms_blocks_499ms_repeat(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=True)
        assert r.dispatch(FakeFrame(), now_ms=0) == LED_COUNT
        assert r.dispatch(FakeFrame(), now_ms=499) == 0  # blocked

    def test_calm_mode_cap_allows_at_500ms(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=True)
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.dispatch(FakeFrame(), now_ms=500) == LED_COUNT  # allowed

    def test_full_mode_cap_250ms(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.dispatch(FakeFrame(), now_ms=249) == 0       # blocked
        assert r.dispatch(FakeFrame(), now_ms=250) == LED_COUNT  # allowed

    def test_set_calm_mode_switches_cap(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=True)
        assert CALM_MIN_INTERVAL_MS == 500
        r.set_calm_mode(False)
        assert FULL_MIN_INTERVAL_MS == 250
        # First dispatch is always allowed after the switch (re-init of state).
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.dispatch(FakeFrame(), now_ms=250) == LED_COUNT


class TestEnvelope:
    def test_idle_leds_set_to_zero(self):
        r = PerimeterRenderer(rng=always_pass_rng)
        captured, set_led = make_capture()
        r.tick(now_ms=0, set_led=set_led)
        # Every LED reported as off.
        assert len(captured) == LED_COUNT
        assert all(rgb == (i, 0, 0, 0) for i, rgb in enumerate(captured, start=LED_MIN_INDEX))

    def test_attack_phase_ramps_up(self):
        # Frame: attack 96 ms, sustain 0, release 0.
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_96_MS, sustain=Time.T_0_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        # At t=0, brightness ~0; at t=48ms ~50 %; at t=96ms envelope just ended.
        captured, set_led = make_capture()
        r.tick(now_ms=0, set_led=set_led)
        first_led = captured[0]
        assert first_led[1] == 0  # r at t=0 = 0

        captured, set_led = make_capture()
        r.tick(now_ms=48, set_led=set_led)
        first_led = captured[0]
        # 200 * 0.5 (mid-attack) * 1.0 (full brightness) = 100
        assert first_led[1] == 100

    def test_sustain_phase_holds_peak(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_192_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        captured, set_led = make_capture()
        r.tick(now_ms=100, set_led=set_led)  # mid-sustain
        first_led = captured[0]
        assert first_led[1] == 200  # full brightness

    def test_release_phase_ramps_down(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        # attack 0, sustain 0, release 96. Whole envelope = 0..96 ms decay.
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_0_MS, release=Time.T_96_MS),
            now_ms=0,
        )
        captured, set_led = make_capture()
        r.tick(now_ms=48, set_led=set_led)
        first_led = captured[0]
        # mid-release: 200 * 0.5 = 100
        assert first_led[1] == 100

    def test_envelope_clears_after_total(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(
            FakeFrame(r=255, g=0, b=0,
                      attack=Time.T_32_MS, sustain=Time.T_32_MS, release=Time.T_32_MS),
            now_ms=0,
        )
        captured, set_led = make_capture()
        # total = 96 ms; well past at 1000 ms.
        r.tick(now_ms=1000, set_led=set_led)
        # All LEDs off.
        assert all(rgb[1] == 0 and rgb[2] == 0 and rgb[3] == 0 for rgb in captured)


class TestCalmModeBrightnessCap:
    def test_calm_mode_caps_brightness_at_50_percent(self):
        # Sustain phase, calm mode: r=200 should output 100 (200 * 0.5).
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=True)
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_192_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        captured, set_led = make_capture()
        r.tick(now_ms=100, set_led=set_led)
        assert captured[0][1] == 100  # half of 200

    def test_full_mode_passes_full_brightness(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_192_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        captured, set_led = make_capture()
        r.tick(now_ms=100, set_led=set_led)
        assert captured[0][1] == 200  # full of 200


class TestClear:
    def test_clear_dampens_all_active_envelopes(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(
            FakeFrame(r=200, sustain=Time.T_3840_MS),
            now_ms=0,
        )
        r.clear()
        captured, set_led = make_capture()
        r.tick(now_ms=100, set_led=set_led)
        assert all(rgb[1] == 0 and rgb[2] == 0 and rgb[3] == 0 for rgb in captured)


class TestLEDIndexing:
    def test_indices_are_1_to_12(self):
        r = PerimeterRenderer(rng=always_pass_rng)
        captured, set_led = make_capture()
        r.tick(now_ms=0, set_led=set_led)
        indices = [c[0] for c in captured]
        assert indices == list(range(1, 13))
        # Per Tildagon hardware: tildagonos.leds[1..12] is the addressable range.
