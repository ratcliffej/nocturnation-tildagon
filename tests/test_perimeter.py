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
    WASH_MAX_HOLD_MS,
)


class FakeFrame:
    """Minimal stand-in for protocol.Frame with just the LIGHT_PULSE fields
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

    def test_full_mode_cap_60ms(self):
        """Full-mode cap was 250 ms (4 Hz) and silently dropped every
        other sparkle at 140 BPM tempo. Bumped to 60 ms (~16 Hz) so
        per-beat sparkles land through 200+ BPM."""
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.dispatch(FakeFrame(), now_ms=FULL_MIN_INTERVAL_MS - 1) == 0       # blocked
        assert r.dispatch(FakeFrame(), now_ms=FULL_MIN_INTERVAL_MS) == LED_COUNT  # allowed

    def test_set_calm_mode_switches_cap(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=True)
        assert CALM_MIN_INTERVAL_MS == 500
        r.set_calm_mode(False)
        assert FULL_MIN_INTERVAL_MS == 60
        # First dispatch is always allowed after the switch (re-init of state).
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.dispatch(FakeFrame(), now_ms=FULL_MIN_INTERVAL_MS) == LED_COUNT


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


class TestAdrCrossfade:
    """Epic 6C Phase G ADR fix. The attack phase now lerps from the
    LED's *current rendered colour* to the new pulse's target, instead
    of from brightness 0. So a series of differently-coloured pulses
    arriving with overlapping envelopes crossfades smoothly rather than
    snapping through black between colours."""

    def test_attack_lerps_from_previous_pulse_colour(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        # First pulse: red at full sustain so the LEDs sit at red.
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_2400_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        # Drive the renderer once so _last_rendered captures the red.
        cap1, set_led1 = make_capture()
        r.tick(now_ms=10, set_led=set_led1)
        assert cap1[0][1] == 200  # red at full

        # Now arm a second pulse to blue with a 96ms attack (frequency
        # cap is bypassed by advancing now well beyond the Full-mode
        # FULL_MIN_INTERVAL_MS interval).
        r.dispatch(
            FakeFrame(r=0, g=0, b=200,
                      attack=Time.T_96_MS, sustain=Time.T_0_MS, release=Time.T_0_MS),
            now_ms=300,
        )
        # At mid-attack (48ms in), the LED should be midway between red
        # and blue, not at black mid-attack.
        cap2, set_led2 = make_capture()
        r.tick(now_ms=300 + 48, set_led=set_led2)
        red_component   = cap2[0][1]
        blue_component  = cap2[0][3]
        # Half-attack: roughly half red (was 200), half blue (target 200).
        assert 60 <= red_component  <= 140, f"red {red_component} should taper from 200"
        assert 60 <= blue_component <= 140, f"blue {blue_component} should ramp from 0 to 200"

    def test_attack_zero_still_snaps(self):
        # T_0_MS attack means no attack ramp - the pulse jumps to its
        # target colour immediately. Crossfade only engages on
        # non-zero-attack pulses. (Existing test_attack_phase_ramps_up
        # covers the from-black case; this is the regression guard for
        # T_0_MS attacks not accidentally lerping.)
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.dispatch(
            FakeFrame(r=255, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_96_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        cap, set_led = make_capture()
        r.tick(now_ms=0, set_led=set_led)   # at t=0, attack==0 means jump to dst
        assert cap[0][1] == 255  # full red immediately


class TestWashBaseline:
    """Epic 6C Phase G LIGHT_WASH support. Wash sits as a uniform
    baseline across the 12 LEDs with optional cosine-eased ping-pong
    drift between r1/r2."""

    def _wash_frame(self, **overrides):
        # Frame stand-in carrying the LIGHT_WASH fields.
        class F:
            r1 = 255; g1 = 0;   b1 = 0
            r2 = 0;   g2 = 0;   b2 = 200
            wash_attack    = 0     # no attack ramp - tests pre-condition on baseline
            wash_release   = 10
            intensity      = 255
            cycle_ms       = 0     # hold r1g1b1 (no drift)
            ttl_seconds    = 0
            pulse_response = 1
        f = F()
        for k, v in overrides.items():
            setattr(f, k, v)
        return f

    def test_wash_baseline_paints_all_leds_uniformly(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(), now_ms=0)
        cap, set_led = make_capture()
        # Advance past attack (which is 0 here).
        r.tick(now_ms=10, set_led=set_led)
        # All 12 LEDs should hold red (r1=255).
        reds = [c[1] for c in cap]
        assert all(r == 255 for r in reds)
        greens = [c[2] for c in cap]
        assert all(g == 0 for g in greens)

    def test_wash_drift_oscillates_between_colours(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        # cycle_ms = 1000: t=0 -> r1, t=500 -> r2, t=1000 -> r1.
        f = self._wash_frame(cycle_ms=1000)
        r.on_light_wash(f, now_ms=0)
        cap1, set_led1 = make_capture()
        r.tick(now_ms=1, set_led=set_led1)   # near start
        r0 = cap1[0]
        cap2, set_led2 = make_capture()
        r.tick(now_ms=500, set_led=set_led2)  # midpoint
        rmid = cap2[0]
        # Start should be ~red; midpoint should be ~blue.
        assert r0[1]   > rmid[1],  "midpoint should have less red than start"
        assert r0[3]   < rmid[3],  "midpoint should have more blue than start"

    def test_wash_end_fades_to_black(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(), now_ms=0)
        # Hold at red.
        cap, set_led = make_capture()
        r.tick(now_ms=10, set_led=set_led)
        assert cap[0][1] == 255

        # Cancel with a 100 ms release (10 * 100 ms).
        class End:
            release_time = 1   # 1 * 100 ms
        r.on_light_wash_end(End(), now_ms=20)
        # Mid-release: roughly half-red.
        cap2, set_led2 = make_capture()
        r.tick(now_ms=20 + 50, set_led=set_led2)
        assert 80 <= cap2[0][1] <= 180, f"mid-release should be mid-red; got {cap2[0][1]}"
        # Post-release: all black.
        cap3, set_led3 = make_capture()
        r.tick(now_ms=20 + 200, set_led=set_led3)
        assert all(c[1] == 0 and c[2] == 0 and c[3] == 0 for c in cap3)

    def test_pulse_on_wash_overlay_fades_back_to_baseline(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(), now_ms=0)  # red baseline (r=255, g=0, b=0)
        cap1, set_led1 = make_capture()
        r.tick(now_ms=10, set_led=set_led1)
        assert cap1[0][1] == 255  # holding red

        # Fire a blue pulse on top, with non-zero attack so the lerp is
        # observable, and ALSO non-zero release so we can observe the
        # fade-back to baseline mid-release.
        r.dispatch(
            FakeFrame(r=0, g=0, b=200,
                      attack=Time.T_96_MS, sustain=Time.T_0_MS, release=Time.T_192_MS),
            now_ms=300,
        )
        # During release, the pulse fades from blue back to the wash
        # baseline (red), NOT to black. At mid-release the LED is a
        # mix of blue and red, never zero on both.
        cap2, set_led2 = make_capture()
        r.tick(now_ms=300 + 96 + 96, set_led=set_led2)   # mid-release
        led = cap2[0]
        # mid-release: lerping from blue (0,0,200) towards red (255,0,0).
        # Some red present, some blue present. Both > 0.
        assert led[1] > 0, f"red should reappear mid-release; got {led}"

    def test_pulse_response_zero_drops_pulse(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(pulse_response=0), now_ms=0)
        cap1, set_led1 = make_capture()
        r.tick(now_ms=10, set_led=set_led1)
        baseline_red = cap1[0][1]

        r.dispatch(FakeFrame(r=0, g=0, b=255), now_ms=300)
        cap2, set_led2 = make_capture()
        r.tick(now_ms=320, set_led=set_led2)
        # Pulse was dropped; LED is still showing the wash baseline.
        assert cap2[0][1] == baseline_red
        assert cap2[0][3] == 0   # no blue overlay landed


class TestWashTtlFailsafe:
    """Lost-WASH_END failsafe (not a protocol change). A ttl_seconds == 0
    wash whose LIGHT_WASH_END frame is lost would otherwise hold forever;
    with pulse_response == 0 the Lume would sit unresponsive. The
    receiver releases itself after WASH_MAX_HOLD_MS."""

    def _wash_frame(self, **overrides):
        class F:
            r1 = 255; g1 = 0; b1 = 0
            r2 = 0;   g2 = 0; b2 = 200
            wash_attack    = 0
            wash_release   = 0   # instant release so post-failsafe is observable quickly
            intensity      = 255
            cycle_ms       = 0
            ttl_seconds    = 0   # "infinite" per spec
            pulse_response = 0   # blocks pulses while held - the bug case
        f = F()
        for k, v in overrides.items():
            setattr(f, k, v)
        return f

    def test_ttl_zero_wash_self_releases_after_max_hold(self):
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(), now_ms=0)
        # Just before the failsafe, the wash is still holding (no release).
        cap_before, set_before = make_capture()
        r.tick(now_ms=WASH_MAX_HOLD_MS - 1, set_led=set_before)
        assert r.is_washing() is True
        assert cap_before[0][1] == 255   # still red baseline
        # At the failsafe boundary, release fires. With release == 0 the
        # state collapses to "not washing" on the next tick.
        cap_after, set_after = make_capture()
        r.tick(now_ms=WASH_MAX_HOLD_MS + 10, set_led=set_after)
        assert r.is_washing() is False

    def test_explicit_short_ttl_still_honoured(self):
        # An explicit ttl_seconds shorter than WASH_MAX_HOLD_MS must still
        # release on the operator's schedule - the failsafe is the floor
        # for "infinite", not a ceiling for everything.
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(ttl_seconds=5), now_ms=0)
        r.tick(now_ms=4_000, set_led=make_capture()[1])
        assert r.is_washing() is True   # still holding at 4s
        r.tick(now_ms=6_000, set_led=make_capture()[1])
        assert r.is_washing() is False  # released after 5s, not waiting 30 min

    def test_failsafe_does_not_fire_before_its_time(self):
        # Generous failsafe; at the 1-minute mark the wash must still hold.
        r = PerimeterRenderer(rng=always_pass_rng, calm_mode=False)
        r.on_light_wash(self._wash_frame(), now_ms=0)
        r.tick(now_ms=60_000, set_led=make_capture()[1])
        assert r.is_washing() is True
