"""DirectorHost + end-to-end Show -> ShowContext -> dispatch tests.

DirectorHost is the object a ShowContext talks to. These tests cover
the host contract and the full path from a Show calling
ctx.render_fx() through to a broadcast frame + local loopback.
"""

from nocturnation.director import DirectorHost, RenderDispatcher
from nocturnation.hal import Capability, CapabilityMask
from nocturnation.render import RgbPulse, RgbWash, PerimeterRenderer
from nocturnation.protocol import parse_frame
from nocturnation.protocol.constants import DeviceClass, MessageType, Time, Chance
from nocturnation.shows import Show, ShowContext
from nocturnation.plugins import PropertyBag


class _BeatShow(Show):
    """Minimal Show: fires a pulse to Light group 1 on every tap."""

    def __init__(self):
        self._ctx = None

    def id(self):           return "beat_stub"
    def display_name(self): return "Beat Stub"
    def context(self):      return self._ctx

    def required_capabilities(self):
        return CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW, Capability.IMU_TAP)

    def on_tap_detected(self, ctx, strength):
        ev = RgbPulse(strength, 0, 0,
                      attack=Time.T_0_MS, sustain=Time.T_96_MS,
                      release=Time.T_480_MS, chance=Chance.CHANCE_100)
        ctx.render_fx("01:01", ev)


class TestDirectorHostContract:
    def test_now_ms_uses_injected_clock(self):
        clock_val = {"t": 0}
        host = DirectorHost(RenderDispatcher(), clock=lambda: clock_val["t"])
        clock_val["t"] = 4242
        assert host.now_ms() == 4242

    def test_default_clock_is_zero(self):
        host = DirectorHost(RenderDispatcher())
        assert host.now_ms() == 0

    def test_analyser_caps_always_empty(self):
        # Tildagon has no microphone.
        host = DirectorHost(RenderDispatcher())
        assert host.analyser_caps().empty() is True

    def test_imu_caps_default_empty(self):
        host = DirectorHost(RenderDispatcher())
        assert host.imu_caps().empty() is True

    def test_imu_caps_reports_injected(self):
        caps = CapabilityMask(Capability.IMU_TAP, Capability.IMU_MOTION)
        host = DirectorHost(RenderDispatcher(), imu_caps=caps)
        assert host.imu_caps().has(Capability.IMU_TAP) is True
        assert host.imu_caps().has(Capability.IMU_MOTION) is True

    def test_set_imu_caps(self):
        host = DirectorHost(RenderDispatcher())
        host.set_imu_caps(CapabilityMask(Capability.IMU_TAP))
        assert host.imu_caps().has(Capability.IMU_TAP) is True


class TestDispatchRenderFx:
    def test_returns_true_on_broadcast(self):
        host = DirectorHost(RenderDispatcher(send_fn=lambda p: None))
        assert host.dispatch_render_fx("01:01", RgbPulse(255, 0, 0)) is True

    def test_returns_true_on_local_loopback_only(self):
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        host = DirectorHost(RenderDispatcher(send_fn=None, perimeter=perimeter))
        assert host.dispatch_render_fx("01:00", RgbPulse(255, 0, 0)) is True

    def test_returns_false_when_nothing_happens(self):
        # No send_fn, no renderers: the frame is built but nothing
        # observable happens, so the host reports False.
        host = DirectorHost(RenderDispatcher(send_fn=None))
        assert host.dispatch_render_fx("01:00", RgbPulse(255, 0, 0)) is False

    def test_dispatch_uses_host_clock_for_now_ms(self):
        # The dispatcher's loopback needs now_ms; the host supplies it
        # from its clock. Verify the perimeter envelope starts at the
        # host clock value.
        clock_val = {"t": 7000}
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        host = DirectorHost(
            RenderDispatcher(send_fn=None, perimeter=perimeter),
            clock=lambda: clock_val["t"],
        )
        host.dispatch_render_fx("01:00", RgbPulse(255, 0, 0))
        # LED 1 envelope start_ms should equal the clock at dispatch.
        env = perimeter._envelopes[1]
        assert env is not None
        assert env[0] == 7000


class TestEndToEndShowToWire:
    def _wire(self, tmp_path):
        sent = []
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        dispatcher = RenderDispatcher(send_fn=sent.append, perimeter=perimeter, source_id=0x40)
        host = DirectorHost(dispatcher, clock=lambda: 0)
        show = _BeatShow()
        bag = PropertyBag(show, path=str(tmp_path / "p.json"))
        ctx = ShowContext(show, bag, host=host)
        show._ctx = ctx
        return show, ctx, sent, perimeter

    def test_tap_fires_frame_on_wire(self, tmp_path):
        show, ctx, sent, _perimeter = self._wire(tmp_path)
        show.on_tap_detected(ctx, 200)
        assert len(sent) == 1
        f = parse_frame(sent[0])
        assert f.source_id == 0x40
        assert f.target_class == DeviceClass.LIGHT
        assert f.target_group == 1
        assert f.r == 200  # strength mapped to red channel

    def test_tap_lights_local_perimeter(self, tmp_path):
        show, ctx, _sent, perimeter = self._wire(tmp_path)
        show.on_tap_detected(ctx, 200)
        # All 12 LEDs armed (CHANCE_100 + rng pinned to 0.0).
        lit = sum(1 for i in range(1, 13) if perimeter._envelopes[i] is not None)
        assert lit == 12

    def test_render_fx_through_context_returns_true(self, tmp_path):
        _show, ctx, _sent, _perimeter = self._wire(tmp_path)
        assert ctx.render_fx("01:01", RgbPulse(255, 0, 0)) is True


class TestDispatchRenderWash:
    """Host-level wash dispatch surface (Epic 6D B1)."""

    def _wash(self):
        return RgbWash(255, 140, 30, 120, 30, 200,
                       attack=20, release=10, intensity=200,
                       cycle_ms=5000, ttl_seconds=0, pulse_response=1)

    def test_render_wash_returns_true_on_broadcast(self):
        host = DirectorHost(RenderDispatcher(send_fn=lambda p: None))
        assert host.dispatch_render_wash("01:00", self._wash()) is True

    def test_render_wash_returns_true_on_local_loopback_only(self):
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        host = DirectorHost(RenderDispatcher(send_fn=None, perimeter=perimeter))
        assert host.dispatch_render_wash("01:00", self._wash()) is True
        assert perimeter.is_washing() is True

    def test_render_wash_end_cancels_local_wash(self):
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        host = DirectorHost(RenderDispatcher(send_fn=None, perimeter=perimeter))
        host.dispatch_render_wash("01:00", self._wash())
        assert perimeter.is_washing() is True
        host.dispatch_render_wash_end("01:00", release_time=10)
        assert perimeter.is_washing() is False

    def test_render_wash_pulse_returns_true_when_washing(self):
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        host = DirectorHost(RenderDispatcher(send_fn=None, perimeter=perimeter))
        host.dispatch_render_wash("01:00", self._wash())
        # Wash-pulse only fires when a wash is active locally.
        assert host.dispatch_render_wash_pulse(
            "01:00", RgbPulse(255, 0, 0)
        ) is True

    def test_wash_broadcasts_carry_correct_message_type(self):
        sent = []
        host = DirectorHost(RenderDispatcher(send_fn=sent.append))
        host.dispatch_render_wash("01:00", self._wash())
        host.dispatch_render_wash_end("01:00", release_time=10)
        host.dispatch_render_wash_pulse("01:00", RgbPulse(255, 0, 0))
        types = [parse_frame(b).message_type for b in sent]
        assert types == [
            MessageType.LIGHT_WASH,
            MessageType.LIGHT_WASH_END,
            MessageType.LIGHT_WASH_PULSE,
        ]


class TestShowContextWashSurface:
    """ShowContext mirror of the M5 surface (Epic 6D B1)."""

    def _wash(self):
        return RgbWash(255, 140, 30, intensity=200, cycle_ms=5000)

    def _wired(self, tmp_path):
        perimeter = PerimeterRenderer(calm_mode=False, rng=lambda: 0.0)
        dispatcher = RenderDispatcher(send_fn=lambda p: None, perimeter=perimeter)
        host = DirectorHost(dispatcher, clock=lambda: 0)
        show = _BeatShow()
        bag = PropertyBag(show, path=str(tmp_path / "p.json"))
        ctx = ShowContext(show, bag, host=host)
        show._ctx = ctx
        return show, ctx, perimeter

    def test_render_wash_via_context(self, tmp_path):
        _show, ctx, perimeter = self._wired(tmp_path)
        assert ctx.render_wash("01:00", self._wash()) is True
        assert perimeter.is_washing() is True

    def test_render_wash_end_via_context(self, tmp_path):
        _show, ctx, perimeter = self._wired(tmp_path)
        ctx.render_wash("01:00", self._wash())
        assert ctx.render_wash_end("01:00", release_time=10) is True
        assert perimeter.is_washing() is False

    def test_render_wash_pulse_via_context(self, tmp_path):
        _show, ctx, perimeter = self._wired(tmp_path)
        ctx.render_wash("01:00", self._wash())
        assert ctx.render_wash_pulse(
            "01:00", RgbPulse(255, 0, 0)
        ) is True

    def test_context_falls_back_safely_without_host(self, tmp_path):
        # No host wired - the methods must short-circuit to False rather
        # than throwing (host tests written before B1 land in this state).
        show = _BeatShow()
        bag = PropertyBag(show, path=str(tmp_path / "p.json"))
        ctx = ShowContext(show, bag, host=None)
        assert ctx.render_wash("01:00", self._wash()) is False
        assert ctx.render_wash_end("01:00", 10) is False
        assert ctx.render_wash_pulse("01:00", RgbPulse(255, 0, 0)) is False
