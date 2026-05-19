"""Reference Show tests: simple_tap + motion_wave.

These are the concrete Shows under apps/nocturnation/shows/. They're
exercised through a ShowContext with a fake host (records render_fx)
and a fake display (records draw calls).
"""

from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import PropertyBag, PluginKind
from nocturnation.shows import ShowContext, InputAction, show_registry, discover_shows

from shows.simple_tap import SimpleTap, make_show as make_simple_tap
from shows.motion_wave import MotionWave, make_show as make_motion_wave


class _FakeHost:
    def __init__(self):
        self.renders = []
        self.t = 0

    def dispatch_render_fx(self, target, ev):
        self.renders.append((target, ev))
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
# simple_tap
# ---------------------------------------------------------------------------

class TestSimpleTapIdentity:
    def test_factory_and_identity(self):
        s = make_simple_tap()
        assert isinstance(s, SimpleTap)
        assert s.id() == "simple_tap"
        assert s.display_name() == "Simple Tap"
        assert s.kind() == PluginKind.SHOW

    def test_required_capabilities(self):
        req = make_simple_tap().required_capabilities()
        assert req.has(Capability.DISPLAY)
        assert req.has(Capability.ESP_NOW)

    def test_properties_palette_and_sensitivity(self):
        keys = [p.key for p in make_simple_tap().properties()]
        assert "palette" in keys
        assert "sensitivity" in keys

    def test_default_palette_is_rainbow(self, tmp_path):
        s = make_simple_tap()
        ctx = _ctx(s, tmp_path)
        assert ctx.get_property("palette") == 3  # Rainbow


class TestSimpleTapTap:
    def test_tap_fires_render_fx_to_light_group_1(self, tmp_path):
        s = make_simple_tap()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        ctx.set_property("palette", 4)  # Red (fixed)
        s.on_tap_detected(ctx, 200)
        assert len(host.renders) == 1
        target, ev = host.renders[0]
        assert target == "01:01"
        assert (ev.r, ev.g, ev.b) == (255, 40, 40)

    def test_rainbow_advances_hue_across_taps(self, tmp_path):
        s = make_simple_tap()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)  # default palette Rainbow
        s.on_tap_detected(ctx, 200)
        s.on_tap_detected(ctx, 200)
        c0 = (host.renders[0][1].r, host.renders[0][1].g, host.renders[0][1].b)
        c1 = (host.renders[1][1].r, host.renders[1][1].g, host.renders[1][1].b)
        assert c0 != c1  # hue advanced

    def test_fixed_palette_is_stable(self, tmp_path):
        s = make_simple_tap()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        ctx.set_property("palette", 5)  # Green
        s.on_tap_detected(ctx, 200)
        s.on_tap_detected(ctx, 200)
        c0 = (host.renders[0][1].r, host.renders[0][1].g, host.renders[0][1].b)
        c1 = (host.renders[1][1].r, host.renders[1][1].g, host.renders[1][1].b)
        assert c0 == c1 == (40, 255, 80)


class TestSimpleTapInput:
    def test_cycle_advances_palette(self, tmp_path):
        s = make_simple_tap()
        ctx = _ctx(s, tmp_path)
        ctx.set_property("palette", 0)  # Cool
        s.on_input_action(ctx, InputAction.CYCLE)
        assert ctx.get_property("palette") == 1  # Natural

    def test_cycle_prev_wraps(self, tmp_path):
        s = make_simple_tap()
        ctx = _ctx(s, tmp_path)
        ctx.set_property("palette", 0)
        s.on_input_action(ctx, InputAction.CYCLE_PREV)
        assert ctx.get_property("palette") == 6  # Blue (wrapped)

    def test_confirm_triggers_tap(self, tmp_path):
        s = make_simple_tap()
        host = _FakeHost()
        ctx = _ctx(s, tmp_path, host=host)
        s.on_input_action(ctx, InputAction.CONFIRM)
        assert len(host.renders) == 1


class TestSimpleTapRender:
    def test_render_draws_title_and_palette(self, tmp_path):
        s = make_simple_tap()
        host = _FakeHost()
        disp = _FakeDisplay()
        ctx = _ctx(s, tmp_path, host=host, display=disp)
        ctx.set_property("palette", 4)  # Red
        s.on_render(ctx)
        assert "Simple Tap" in disp.texts
        assert "Red" in disp.texts
        assert len(disp.clears) == 1

    def test_render_no_display_is_safe(self, tmp_path):
        s = make_simple_tap()
        ctx = _ctx(s, tmp_path, host=_FakeHost(), display=None)
        s.on_render(ctx)  # must not crash


# ---------------------------------------------------------------------------
# motion_wave
# ---------------------------------------------------------------------------

class TestMotionWave:
    def test_factory_and_identity(self):
        m = make_motion_wave()
        assert isinstance(m, MotionWave)
        assert m.id() == "motion_wave"
        assert m.display_name() == "Motion Wave"

    def test_motion_fires_render_fx_axis_colour(self, tmp_path):
        m = make_motion_wave()
        host = _FakeHost()
        ctx = _ctx(m, tmp_path, host=host)
        m.on_motion_event(ctx, 0, 255)  # X axis, full magnitude
        target, ev = host.renders[0]
        assert target == "01:00"
        assert (ev.r, ev.g, ev.b) == (255, 0, 0)

    def test_magnitude_scales_brightness(self, tmp_path):
        m = make_motion_wave()
        host = _FakeHost()
        ctx = _ctx(m, tmp_path, host=host)
        m.on_motion_event(ctx, 2, 128)  # Z axis, half magnitude
        _t, ev = host.renders[0]
        # Z base is (0,0,255); 128/255 ~ 0.502 -> b ~ 128.
        assert ev.r == 0 and ev.g == 0
        assert 126 <= ev.b <= 130

    def test_render_draws_title(self, tmp_path):
        m = make_motion_wave()
        disp = _FakeDisplay()
        ctx = _ctx(m, tmp_path, host=_FakeHost(), display=disp)
        m.on_motion_event(ctx, 1, 200)  # Y
        m.on_render(ctx)
        assert "Motion Wave" in disp.texts
        assert "axis Y" in disp.texts


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def setup_method(self):
        show_registry().clear()

    def teardown_method(self):
        show_registry().clear()

    def test_discovers_both_reference_shows(self):
        n = discover_shows()  # walks apps/nocturnation/shows/
        assert n >= 2
        ids = [s.id() for s in show_registry()]
        assert "simple_tap" in ids
        assert "motion_wave" in ids
