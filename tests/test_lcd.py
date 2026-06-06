"""LCD renderer tests.

Calm Mode (default): renderer is fully disabled. dispatch returns False,
current_colour returns None, the LCD stays on its static UI background.

Full mode: each accepted dispatch arms a full-screen envelope.
current_colour returns the (r, g, b) at that moment, capped at 60 % of
the LIGHT_PULSE's RGB to keep the face-distance wash from being
uncomfortably bright. Frequency cap is 4 Hz (250 ms minimum interval).
"""

from nocturnation.protocol.constants import Time, Chance
from nocturnation.render.lcd import (
    LcdRenderer,
    LCD_MIN_INTERVAL_MS,
    FULL_BRIGHTNESS_CAP,
)


class FakeFrame:
    __slots__ = ("r", "g", "b", "attack", "sustain", "release", "chance")

    def __init__(self, r=200, g=0, b=0,
                 attack=Time.T_32_MS, sustain=Time.T_192_MS, release=Time.T_96_MS,
                 chance=Chance.CHANCE_100):
        self.r = r
        self.g = g
        self.b = b
        self.attack = attack
        self.sustain = sustain
        self.release = release
        self.chance = chance


class TestCalmMode:
    def test_default_is_calm_mode_disabled(self):
        r = LcdRenderer()
        assert r.enabled is False

    def test_calm_mode_dispatch_returns_false(self):
        r = LcdRenderer(calm_mode=True)
        assert r.dispatch(FakeFrame(), now_ms=0) is False

    def test_calm_mode_current_colour_is_none(self):
        r = LcdRenderer(calm_mode=True)
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.current_colour(now_ms=50) is None

    def test_switching_to_calm_clears_active_wash(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(FakeFrame(sustain=Time.T_3840_MS), now_ms=0)
        # Wash is active.
        assert r.current_colour(now_ms=100) is not None
        # Switch to Calm Mode mid-envelope.
        r.set_calm_mode(True)
        assert r.current_colour(now_ms=200) is None


class TestFullModeDispatch:
    def test_full_mode_dispatch_accepts_valid_frame(self):
        r = LcdRenderer(calm_mode=False)
        assert r.dispatch(FakeFrame(), now_ms=0) is True

    def test_primer_dropped(self):
        r = LcdRenderer(calm_mode=False)
        assert r.dispatch(FakeFrame(r=0, g=0, b=0), now_ms=0) is False

    def test_primer_does_not_consume_rate_limit(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(FakeFrame(r=0, g=0, b=0), now_ms=0)
        # Main fire right after a primer is allowed.
        assert r.dispatch(FakeFrame(r=200), now_ms=50) is True

    def test_zero_duration_dropped(self):
        r = LcdRenderer(calm_mode=False)
        assert r.dispatch(
            FakeFrame(attack=Time.T_0_MS, sustain=Time.T_0_MS, release=Time.T_0_MS),
            now_ms=0,
        ) is False


class TestFrequencyCap:
    def test_cap_blocks_repeat_within_interval(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(FakeFrame(), now_ms=0)
        # At exactly cap - 1 ms, blocked.
        assert r.dispatch(FakeFrame(), now_ms=LCD_MIN_INTERVAL_MS - 1) is False

    def test_cap_allows_at_interval(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(FakeFrame(), now_ms=0)
        assert r.dispatch(FakeFrame(), now_ms=LCD_MIN_INTERVAL_MS) is True

    def test_cap_matches_4_hz(self):
        # The cap should be the Full-mode 4 Hz spec from architecture
        # spec section 15.1 - 250 ms minimum interval.
        assert LCD_MIN_INTERVAL_MS == 250


class TestChanceGate:
    """LIGHT_PULSE.chance must gate LCD dispatch the same way it gates
    the perimeter renderer - otherwise a low-chance sparkle setting
    fires the full-screen flash on every admitted pulse, swamping the
    intended sparse effect (bench-confirmed regression on a Tildagon
    with Calm Mode disabled, Epic 7 B7)."""

    def test_chance_100_always_fires(self):
        # Even with a deterministic rng that returns the worst-case
        # value (0.999), CHANCE_100 should always pass: 0.999 < 1.00.
        r = LcdRenderer(calm_mode=False, rng=lambda: 0.999)
        assert r.dispatch(FakeFrame(chance=Chance.CHANCE_100), now_ms=0) is True

    def test_chance_4_rolled_fail_drops_dispatch(self):
        # CHANCE_4 = 0.04. An rng returning 0.5 fails the roll (0.5 >= 0.04).
        r = LcdRenderer(calm_mode=False, rng=lambda: 0.5)
        assert r.dispatch(FakeFrame(chance=Chance.CHANCE_4), now_ms=0) is False

    def test_chance_4_rolled_pass_accepts_dispatch(self):
        # CHANCE_4 = 0.04. An rng returning 0.01 passes (0.01 < 0.04).
        r = LcdRenderer(calm_mode=False, rng=lambda: 0.01)
        assert r.dispatch(FakeFrame(chance=Chance.CHANCE_4), now_ms=0) is True

    def test_rolled_fail_consumes_rate_limit(self):
        # A rolled-fail dispatch still advances the rate-limit clock so
        # a fast-arriving sequence of rolled-fails throttles the same as
        # a sequence of rolled-passes. Otherwise low-chance sparkle at
        # high cadence would deliver one rolled-pass right after the
        # previous rolled-fail with no spacing.
        rng = iter([0.5, 0.01])
        r = LcdRenderer(calm_mode=False, rng=lambda: next(rng))
        assert r.dispatch(FakeFrame(chance=Chance.CHANCE_4), now_ms=0) is False
        assert r.dispatch(FakeFrame(chance=Chance.CHANCE_4),
                          now_ms=LCD_MIN_INTERVAL_MS - 1) is False  # rate-limited

    def test_rolled_pass_accepts_after_rate_window(self):
        rng = iter([0.5, 0.01])
        r = LcdRenderer(calm_mode=False, rng=lambda: next(rng))
        r.dispatch(FakeFrame(chance=Chance.CHANCE_4), now_ms=0)  # rolled-fail
        assert r.dispatch(FakeFrame(chance=Chance.CHANCE_4),
                          now_ms=LCD_MIN_INTERVAL_MS) is True


class TestEnvelopeShape:
    def test_attack_phase_ramps_up(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_96_MS, sustain=Time.T_0_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        # At t=48 (mid-attack), brightness = 0.5 * 0.6 cap = 0.3 of 200 = 60.
        rgb = r.current_colour(now_ms=48)
        assert rgb is not None
        assert rgb[0] == 60

    def test_sustain_phase_holds_peak(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(
            FakeFrame(r=200, g=0, b=0,
                      attack=Time.T_0_MS, sustain=Time.T_192_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        # At t=100 (mid-sustain), brightness = 1.0 * 0.6 cap = 0.6 of 200 = 120.
        rgb = r.current_colour(now_ms=100)
        assert rgb == (120, 0, 0)

    def test_envelope_clears_after_total(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(
            FakeFrame(r=255, attack=Time.T_32_MS, sustain=Time.T_32_MS, release=Time.T_32_MS),
            now_ms=0,
        )
        # total = 96 ms; well past at 1000 ms.
        assert r.current_colour(now_ms=1000) is None


class TestBrightnessCap:
    def test_peak_capped_at_60_percent(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(
            FakeFrame(r=255, g=255, b=255,
                      attack=Time.T_0_MS, sustain=Time.T_192_MS, release=Time.T_0_MS),
            now_ms=0,
        )
        # Pure white at sustain peak: each channel * 0.6 cap = 153.
        rgb = r.current_colour(now_ms=50)
        assert rgb == (153, 153, 153)

    def test_brightness_cap_constant(self):
        # Architecture spec section 15.2: no high-contrast full-screen
        # flashes. 60 % is the agreed bench-tested upper bound.
        assert FULL_BRIGHTNESS_CAP == 0.6


class TestClear:
    def test_clear_drops_active_envelope(self):
        r = LcdRenderer(calm_mode=False)
        r.dispatch(FakeFrame(sustain=Time.T_3840_MS), now_ms=0)
        assert r.current_colour(now_ms=100) is not None
        r.clear()
        assert r.current_colour(now_ms=100) is None
