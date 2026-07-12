"""Conductor Show tests (Epic 6D B3).

Operator-driven, no-mic reference Show. Tests exercise identity, the
property schema and defaults, the wash emission on enter + on property
change, the tap-driven pulse path with chance / palette / section
signature, palette cycling, and the starlight overlay. Driven through
a fake host that records `render_fx` / `render_wash` / `render_wash_end`
calls.
"""

from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import PropertyBag, PluginKind
from nocturnation.shows import ShowContext, InputAction
from nocturnation.protocol.constants import Time, Chance

from shows.conductor import Conductor, make_show as make_conductor


class _FakeHost:
    """Records every dispatch_* call so tests can assert against them."""

    def __init__(self):
        self.renders = []           # (target, ev) - RgbPulse via render_fx
        self.washes = []            # (target, ev) - RgbWash via render_wash
        self.wash_ends = []         # (target, release_time)
        self.t = 0

    def dispatch_render_fx(self, target, ev):
        self.renders.append((target, ev))
        return True

    def dispatch_render_wash(self, target, ev):
        self.washes.append((target, ev))
        return True

    def dispatch_render_wash_end(self, target, release_time):
        self.wash_ends.append((target, release_time))
        return True

    def dispatch_render_wash_pulse(self, target, ev):
        self.renders.append(("wash_pulse:" + target, ev))
        return True

    def now_ms(self):
        return self.t

    def analyser_caps(self):
        return CapabilityMask()

    def imu_caps(self):
        return CapabilityMask()


class _FakeDisplay:
    def __init__(self):
        self.texts = []
        self.clears = []

    def clear(self, r=0, g=0, b=0):
        self.clears.append((r, g, b))

    def fill_rect(self, x, y, w, h, r, g, b):
        pass

    def text(self, x, y, s, size=18, r=255, g=255, b=255, center=True):
        self.texts.append(s)


def _ctx(show, tmp_path, host=None, display=None):
    bag = PropertyBag(show, path=str(tmp_path / "p.json"))
    ctx = ShowContext(show, bag, host=host, display=display)
    show.bind_context(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Identity / capabilities / properties
# ---------------------------------------------------------------------------

class TestConductorIdentity:
    def test_factory_and_identity(self):
        s = make_conductor()
        assert isinstance(s, Conductor)
        assert s.id() == "conductor"
        assert s.display_name() == "Conductor"
        assert s.kind() == PluginKind.SHOW

    def test_required_capabilities(self):
        req = make_conductor().required_capabilities()
        assert req.has(Capability.DISPLAY)
        assert req.has(Capability.ESP_NOW)

    def test_property_keys_in_expected_order(self):
        keys = [p.key for p in make_conductor().properties()]
        assert keys == [
            "palette_set", "chance", "wash_speed",
            "sensitivity", "starlight", "section", "target_group",
        ]

    def test_property_defaults_match_B0(self, tmp_path):
        s = make_conductor()
        ctx = _ctx(s, tmp_path)
        assert ctx.get_property("palette_set") == 0    # Warm
        assert ctx.get_property("chance") == 4         # 32 %
        assert ctx.get_property("wash_speed") == 2     # Medium
        assert ctx.get_property("sensitivity") == 1    # Medium
        assert ctx.get_property("starlight") is False
        assert ctx.get_property("section") == 0        # Verse
        assert ctx.get_property("target_group") == 0


# ---------------------------------------------------------------------------
# Lifecycle - enter emits a wash, exit cancels it
# ---------------------------------------------------------------------------

class TestConductorLifecycle:
    def test_enter_emits_wash_for_verse(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        # One wash on entry, targeting Light class.
        assert len(host.washes) == 1
        target, ev = host.washes[0]
        assert target == "01:00"
        # Warm verse palette: (120, 30, 0) -> (60, 10, 0).
        assert (ev.r1, ev.g1, ev.b1) == (120, 30, 0)
        assert (ev.r2, ev.g2, ev.b2) == (60, 10, 0)
        assert ev.intensity == 140
        assert ev.cycle_ms == 5000  # Verse respects wash_speed default = Medium
        assert ev.pulse_response == 1

    def test_exit_cancels_wash(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        s.exit(ctx)
        assert len(host.wash_ends) == 1
        target, release_time = host.wash_ends[0]
        assert target == "01:00"
        assert release_time == 10   # 1.0 s


# ---------------------------------------------------------------------------
# Property changes that re-emit the wash
# ---------------------------------------------------------------------------

class TestConductorPropertyChanges:
    def test_section_change_re_emits_wash(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)            # one wash
        ctx.set_property("section", 2)   # BuildUp
        # Expect a second wash with BuildUp palette + override cycle_ms.
        assert len(host.washes) == 2
        target, ev = host.washes[1]
        assert ev.cycle_ms == 3000      # Warm BuildUp baked cycle_ms
        assert ev.intensity == 180

    def test_palette_set_change_re_emits_wash(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_property("palette_set", 1)   # Cool
        assert len(host.washes) == 2
        target, ev = host.washes[1]
        # Cool verse anchors: (0, 60, 100) -> (0, 20, 60).
        assert (ev.r1, ev.g1, ev.b1) == (0, 60, 100)
        assert (ev.r2, ev.g2, ev.b2) == (0, 20, 60)

    def test_wash_speed_change_re_emits_only_when_verse_or_chorus(self, tmp_path):
        # Verse / Chorus respect wash_speed; BuildUp / Drop / Breakdown
        # don't (they always use the palette-baked cycle_ms).
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_property("wash_speed", 4)   # Snappy = 2000 ms
        # Verse re-emit picks up the new cycle_ms.
        assert host.washes[-1][1].cycle_ms == 2000
        # Move to Drop - the override should pin cycle_ms regardless of speed.
        ctx.set_property("section", 3)
        assert host.washes[-1][1].cycle_ms == 4000  # Drop's palette-baked


# ---------------------------------------------------------------------------
# Tap - pulse fan-out with palette colour + chance + section signature
# ---------------------------------------------------------------------------

class TestConductorTap:
    def test_tap_fires_warm_pulse_colour(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        assert len(host.renders) == 1
        _target, ev = host.renders[0]
        # Warm pulse colour (255, 180, 60).
        assert (ev.r, ev.g, ev.b) == (255, 180, 60)
        assert ev.sustain == Time.T_96_MS    # not Drop section

    def test_tap_in_drop_section_uses_longer_sustain(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_property("section", 3)   # Drop
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        _target, ev = host.renders[0]
        assert ev.sustain == Time.T_192_MS

    def test_tap_chance_property_maps_to_chance_enum(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_property("chance", 0)    # 100 %
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        assert host.renders[0][1].chance == Chance.CHANCE_100
        ctx.set_property("chance", 7)    # 4 %
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        assert host.renders[0][1].chance == Chance.CHANCE_4

    def test_paused_skips_tap_pulse(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_paused(True)
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        assert host.renders == []

    def test_on_beat_detected_fires_same_path_as_tap(self, tmp_path):
        # IMU adapter routes a tap to both on_tap_detected + on_beat_detected
        # so beat-driven Shows are host-agnostic.
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        host.renders.clear()
        s.on_beat_detected(ctx, 100)
        assert len(host.renders) == 1

    def test_starlight_overlay_fires_extra_pulse(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_property("starlight", True)
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        assert len(host.renders) == 2

    def test_starlight_suppressed_in_breakdown(self, tmp_path):
        # Per Conductor design: starlight off during Breakdown so it
        # doesn't fight the quiet section's intent.
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        ctx.set_property("starlight", True)
        ctx.set_property("section", 4)   # Breakdown
        host.renders.clear()
        s.on_tap_detected(ctx, 200)
        assert len(host.renders) == 1


# ---------------------------------------------------------------------------
# Input - palette cycle forward/back
# ---------------------------------------------------------------------------

class TestConductorInput:
    def test_cycle_advances_palette(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        assert ctx.get_property("palette_set") == 0  # Warm
        s.on_input_action(ctx, InputAction.CYCLE)
        assert ctx.get_property("palette_set") == 1  # Cool

    def test_cycle_prev_wraps(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        s.on_input_action(ctx, InputAction.CYCLE_PREV)
        assert ctx.get_property("palette_set") == 3  # wrap to Mono

    def test_confirm_triggers_tap(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        host.renders.clear()
        s.on_input_action(ctx, InputAction.CONFIRM)
        # One pulse fired (Confirm -> on_tap_detected).
        assert len(host.renders) == 1

    def test_section_next_advances_section(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        assert ctx.get_property("section") == 0  # Verse
        s.on_input_action(ctx, InputAction.SECTION_NEXT)
        assert ctx.get_property("section") == 1  # Chorus

    def test_section_prev_wraps(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        s.on_input_action(ctx, InputAction.SECTION_PREV)
        # 5 sections: Verse -> wrap to Breakdown (index 4).
        assert ctx.get_property("section") == 4

    def test_section_next_walks_full_arc(self, tmp_path):
        # Verse -> Chorus -> BuildUp -> Drop -> Breakdown -> Verse.
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.enter(ctx)
        expected = [1, 2, 3, 4, 0]
        for i in expected:
            s.on_input_action(ctx, InputAction.SECTION_NEXT)
            assert ctx.get_property("section") == i


# ---------------------------------------------------------------------------
# Render path - draws without crashing, includes the expected labels
# ---------------------------------------------------------------------------

class TestConductorRender:
    def test_render_draws_title_palette_and_section(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        display = _FakeDisplay()
        ctx = _ctx(s, tmp_path, host=host, display=display)
        s.enter(ctx)
        s.on_render(ctx)
        assert "Conductor" in display.texts
        assert "Warm" in display.texts
        assert "Verse" in display.texts

    def test_render_no_display_is_safe(self, tmp_path):
        s = make_conductor()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host, display=None)
        s.enter(ctx)
        s.on_render(ctx)   # must not raise
