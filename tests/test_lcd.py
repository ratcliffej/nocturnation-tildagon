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
    WASH_MAX_HOLD_MS,
)


class FakeWashFrame:
    """Stand-in for protocol.Frame carrying just the LIGHT_WASH fields
    the LCD renderer reads. Defaults match the lost-WASH_END failure
    case: ttl_seconds = 0 ("infinite") + pulse_response = 0."""

    def __init__(self, r1=255, g1=0, b1=0, r2=0, g2=0, b2=200,
                 wash_attack=0, wash_release=0,
                 intensity=255, cycle_ms=0,
                 ttl_seconds=0, pulse_response=0):
        self.r1 = r1; self.g1 = g1; self.b1 = b1
        self.r2 = r2; self.g2 = g2; self.b2 = b2
        self.wash_attack = wash_attack
        self.wash_release = wash_release
        self.intensity = intensity
        self.cycle_ms = cycle_ms
        self.ttl_seconds = ttl_seconds
        self.pulse_response = pulse_response


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
    def test_default_is_full_mode_enabled(self):
        # Default flipped 2026-07-12 (v1.0.0): calm_mode=False, renderer
        # enabled out of the box.
        r = LcdRenderer()
        assert r.enabled is True

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

    def test_cap_value(self):
        # Was 250 ms (4 Hz) per architecture spec section 15.1, but
        # silently dropped every other sparkle at 140 BPM tempo. Bumped
        # to 60 ms (~16 Hz) so per-beat sparkles land through 200+ BPM.
        assert LCD_MIN_INTERVAL_MS == 60


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


class TestWashTtlFailsafe:
    """Lost-WASH_END failsafe: a LIGHT_WASH with ttl_seconds == 0 whose
    LIGHT_WASH_END is lost would otherwise hold forever; with
    pulse_response == 0 the renderer would also gate pulses. After
    WASH_MAX_HOLD_MS the receiver self-releases."""

    def test_ttl_zero_wash_self_releases_after_max_hold(self):
        r = LcdRenderer(calm_mode=False)
        r.on_light_wash(FakeWashFrame(), now_ms=0)
        # Just before the failsafe, still washing.
        assert r.is_washing() is True
        _ = r.current_colour(now_ms=WASH_MAX_HOLD_MS - 1)
        assert r.is_washing() is True
        # At the failsafe boundary (release == 0 so the release phase
        # collapses immediately), the wash is gone on the next tick.
        _ = r.current_colour(now_ms=WASH_MAX_HOLD_MS + 10)
        assert r.is_washing() is False

    def test_explicit_short_ttl_still_honoured(self):
        r = LcdRenderer(calm_mode=False)
        r.on_light_wash(FakeWashFrame(ttl_seconds=5), now_ms=0)
        _ = r.current_colour(now_ms=4_000)
        assert r.is_washing() is True
        _ = r.current_colour(now_ms=6_000)
        assert r.is_washing() is False

    def test_failsafe_does_not_fire_before_its_time(self):
        r = LcdRenderer(calm_mode=False)
        r.on_light_wash(FakeWashFrame(), now_ms=0)
        _ = r.current_colour(now_ms=60_000)
        assert r.is_washing() is True
