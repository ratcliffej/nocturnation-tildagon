"""Show base class + ShowContext tests."""

from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import (
    PluginKind,
    PowerProfile,
    PropertyBag,
    PropertyDef,
    PropertyType,
)
from nocturnation.shows import Show, ShowContext


class _StubShow(Show):
    """Concrete Show that records every hook invocation."""

    _PROPS = (
        PropertyDef(
            key="palette",
            type=PropertyType.ENUM,
            default_value=0,
            min_value=0,
            max_value=3,
            display_name="Palette",
            enum_names=("Cool", "Natural", "Warm", "Rainbow"),
        ),
    )

    def __init__(self):
        self.calls = []
        self._ctx = None  # built lazily after we hand it a property bag

    def id(self):
        return "stub_show"

    def display_name(self):
        return "Stub Show"

    def required_capabilities(self):
        return CapabilityMask(Capability.DISPLAY, Capability.IMU_TAP)

    def properties(self):
        return self._PROPS

    def power(self):
        return PowerProfile(needs_audio_frames=False, tick_hz=10)

    def context(self):
        return self._ctx

    # Hooks: log every invocation
    def enter(self, ctx):                                  self.calls.append(("enter",))
    def exit(self, ctx):                                   self.calls.append(("exit",))
    def on_beat_detected(self, ctx, strength):             self.calls.append(("beat", strength))
    def on_tap_detected(self, ctx, strength):              self.calls.append(("tap", strength))
    def on_motion_event(self, ctx, axis, magnitude):       self.calls.append(("motion", axis, magnitude))
    def on_input_action(self, ctx, action):                self.calls.append(("input", action))
    def on_render(self, ctx):                              self.calls.append(("render",))
    def on_property_changed(self, ctx, key):               self.calls.append(("property_changed", key))
    def tick(self, ctx, now_ms):                           self.calls.append(("tick", now_ms))


def _build_ctx(tmp_path, host=None):
    show = _StubShow()
    bag = PropertyBag(show, path=str(tmp_path / "p.json"))
    ctx = ShowContext(show, bag, host=host)
    show._ctx = ctx
    return show, ctx


class TestShowIdentity:
    def test_kind_is_show(self):
        assert _StubShow().kind() == PluginKind.SHOW

    def test_id_and_display_name(self):
        s = _StubShow()
        assert s.id() == "stub_show"
        assert s.display_name() == "Stub Show"

    def test_required_capabilities_declared(self):
        req = _StubShow().required_capabilities()
        assert req.has(Capability.DISPLAY) is True
        assert req.has(Capability.IMU_TAP) is True


class TestShowBaseHooksAreNoOp:
    def test_base_class_hooks_swallow_calls(self):
        # The Show base class declares every hook with a no-op default;
        # a concrete Show that only overrides `enter` should still
        # accept `on_beat_detected` etc. without raising.
        class Minimal(Show):
            def id(self): return "min"
            def display_name(self): return "Min"
            def context(self): return None
        m = Minimal()
        m.on_beat_detected(None, 100)
        m.on_tap_detected(None, 200)
        m.on_motion_event(None, 2, 128)
        m.on_render(None)
        m.tick(None, 0)


class TestShowContextRouting:
    def test_hook_calls_route_to_show(self, tmp_path):
        show, ctx = _build_ctx(tmp_path)
        show.enter(ctx)
        show.on_tap_detected(ctx, 200)
        show.on_motion_event(ctx, 2, 128)
        show.on_render(ctx)
        show.exit(ctx)
        assert show.calls == [
            ("enter",),
            ("tap", 200),
            ("motion", 2, 128),
            ("render",),
            ("exit",),
        ]


class TestShowContextProperties:
    def test_get_property_returns_default(self, tmp_path):
        _show, ctx = _build_ctx(tmp_path)
        assert ctx.get_property("palette") == 0

    def test_set_property_clamps_and_notifies(self, tmp_path):
        show, ctx = _build_ctx(tmp_path)
        stored = ctx.set_property("palette", 5)
        # Clamped to max 3.
        assert stored == 3
        assert ctx.get_property("palette") == 3
        # Show was notified.
        assert ("property_changed", "palette") in show.calls


class TestShowContextRenderFx:
    def test_render_fx_returns_false_with_no_host(self, tmp_path):
        # B2 has no host wired; render_fx is a stub. B3 lights it up.
        _show, ctx = _build_ctx(tmp_path)
        assert ctx.render_fx("01:01", object()) is False

    def test_render_fx_forwards_to_host(self, tmp_path):
        class FakeHost:
            def __init__(self):
                self.last = None
            def dispatch_render_fx(self, target, ev):
                self.last = (target, ev)
                return True
        host = FakeHost()
        _show, ctx = _build_ctx(tmp_path, host=host)
        ev = object()
        assert ctx.render_fx("00:00", ev) is True
        assert host.last == ("00:00", ev)


class TestShowContextCapabilityQueries:
    def test_no_host_returns_empty_masks(self, tmp_path):
        _show, ctx = _build_ctx(tmp_path)
        assert ctx.analyser_caps().empty() is True
        assert ctx.imu_caps().empty() is True

    def test_host_returns_its_caps(self, tmp_path):
        class FakeHost:
            def analyser_caps(self):
                return CapabilityMask(Capability.ANALYSER_BEAT_DETECTION)
            def imu_caps(self):
                return CapabilityMask(Capability.IMU_TAP, Capability.IMU_MOTION)
        _show, ctx = _build_ctx(tmp_path, host=FakeHost())
        assert ctx.analyser_caps().has(Capability.ANALYSER_BEAT_DETECTION) is True
        assert ctx.imu_caps().has(Capability.IMU_TAP) is True
        assert ctx.imu_caps().has(Capability.IMU_MOTION) is True


class TestShowContextPauseAndTime:
    def test_pause_toggles(self, tmp_path):
        _show, ctx = _build_ctx(tmp_path)
        assert ctx.paused() is False
        ctx.set_paused(True)
        assert ctx.paused() is True
        ctx.set_paused(False)
        assert ctx.paused() is False

    def test_since_enter_ms_uses_host_clock(self, tmp_path):
        class FakeHost:
            def __init__(self):
                self.t = 0
            def now_ms(self):
                return self.t
        host = FakeHost()
        _show, ctx = _build_ctx(tmp_path, host=host)
        host.t = 5000
        ctx.mark_entered(host.t)
        host.t = 5500
        assert ctx.since_enter_ms() == 500

    def test_no_host_returns_zero_time(self, tmp_path):
        _show, ctx = _build_ctx(tmp_path)
        assert ctx.now_ms() == 0
        assert ctx.since_enter_ms() == 0
