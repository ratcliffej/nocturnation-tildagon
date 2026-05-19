"""DirectorController tests.

The controller is the testable heart of Director mode: input routing,
Show lifecycle / selection / persistence, sensitivity, tick gating,
and InputAction routing. Input sources are faked so these tests cover
routing, not detection tuning (the IMU/button detection has its own
tests).
"""

from nocturnation.director import (
    DirectorController,
    DirectorHost,
    RenderDispatcher,
    ButtonTapSource,
    RESULT_HANDLED,
    RESULT_OPEN_PICKER,
    RESULT_OPEN_SETTINGS,
)
from nocturnation.plugins import PowerProfile, PropertyDef, PropertyType
from nocturnation.shows import Show, ShowRegistry, InputAction


class _FakeImu:
    """Captures the controller-wired callbacks so a test can fire
    taps / motion directly, bypassing detection."""

    def __init__(self):
        self.on_tap = None
        self.on_motion = None
        self.sensitivity = None
        self.reset_count = 0

    def set_callbacks(self, on_tap=None, on_motion=None):
        self.on_tap = on_tap
        self.on_motion = on_motion

    def set_sensitivity(self, level):
        self.sensitivity = level

    def reset(self):
        self.reset_count += 1

    def poll(self, now_ms):
        pass

    # Test helpers
    def fire_tap(self, strength):
        self.on_tap(strength)

    def fire_motion(self, axis, magnitude):
        self.on_motion(axis, magnitude)


class _FakeShow(Show):
    def __init__(self, sid, sensitivity_default=None, tick_hz=0):
        self._sid = sid
        self._sensitivity_default = sensitivity_default
        self._tick_hz = tick_hz
        self.calls = []

    def id(self):
        return self._sid

    def display_name(self):
        return self._sid

    def properties(self):
        if self._sensitivity_default is None:
            return ()
        return (
            PropertyDef(
                key="sensitivity",
                type=PropertyType.ENUM,
                default_value=self._sensitivity_default,
                min_value=0,
                max_value=2,
                enum_names=("Low", "Medium", "High"),
            ),
        )

    def power(self):
        return PowerProfile(needs_audio_frames=False, tick_hz=self._tick_hz)

    def enter(self, ctx):                       self.calls.append(("enter",))
    def exit(self, ctx):                        self.calls.append(("exit",))
    def on_tap_detected(self, ctx, s):          self.calls.append(("tap", s))
    def on_beat_detected(self, ctx, s):         self.calls.append(("beat", s))
    def on_motion_event(self, ctx, a, m):       self.calls.append(("motion", a, m))
    def on_input_action(self, ctx, action):     self.calls.append(("input", action))
    def tick(self, ctx, now):                   self.calls.append(("tick", now))


def _controller(tmp_path, shows, imu=None, button=None, initial="",
                on_changed=None, clock=None):
    host = DirectorHost(RenderDispatcher(send_fn=None),
                        clock=clock if clock is not None else (lambda: 0))
    reg = ShowRegistry()
    for s in shows:
        reg.register(s)
    return DirectorController(
        host, reg, imu=imu, button_tap=button,
        initial_show_id=initial, on_active_show_changed=on_changed,
        property_bag_path=str(tmp_path / "p.json"),
    )


class TestEnter:
    def test_activates_first_show_when_no_initial(self, tmp_path):
        a, b = _FakeShow("alpha"), _FakeShow("beta")
        c = _controller(tmp_path, [a, b])
        c.enter()
        assert c.active_show is a
        assert ("enter",) in a.calls

    def test_activates_initial_show(self, tmp_path):
        a, b = _FakeShow("alpha"), _FakeShow("beta")
        c = _controller(tmp_path, [a, b], initial="beta")
        c.enter()
        assert c.active_show is b

    def test_unknown_initial_falls_back_to_first(self, tmp_path):
        a, b = _FakeShow("alpha"), _FakeShow("beta")
        c = _controller(tmp_path, [a, b], initial="ghost")
        c.enter()
        assert c.active_show is a

    def test_empty_registry_no_active_show(self, tmp_path):
        c = _controller(tmp_path, [])
        c.enter()  # must not crash
        assert c.active_show is None

    def test_resets_inputs(self, tmp_path):
        imu = _FakeImu()
        button = ButtonTapSource()
        c = _controller(tmp_path, [_FakeShow("alpha")], imu=imu, button=button)
        c.enter()
        assert imu.reset_count == 1

    def test_default_activation_does_not_persist(self, tmp_path):
        changed = []
        c = _controller(tmp_path, [_FakeShow("alpha")],
                        on_changed=lambda sid: changed.append(sid))
        c.enter()
        assert changed == []  # restoring the default isn't a user choice


class TestTapRouting:
    def test_tap_fires_both_tap_and_beat(self, tmp_path):
        imu = _FakeImu()
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show], imu=imu)
        c.enter()
        imu.fire_tap(200)
        assert ("tap", 200) in show.calls
        assert ("beat", 200) in show.calls

    def test_motion_routes_to_show(self, tmp_path):
        imu = _FakeImu()
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show], imu=imu)
        c.enter()
        imu.fire_motion(2, 128)
        assert ("motion", 2, 128) in show.calls

    def test_tap_with_no_active_show_is_safe(self, tmp_path):
        imu = _FakeImu()
        c = _controller(tmp_path, [], imu=imu)
        c.enter()
        imu.fire_tap(200)  # no active show; must not crash

    def test_button_tap_integration(self, tmp_path):
        button = ButtonTapSource()
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show], button=button)
        c.enter()
        c.poll_inputs(0, button_pressed=True)  # rising edge -> tap
        assert any(call[0] == "tap" for call in show.calls)
        assert any(call[0] == "beat" for call in show.calls)


class TestSelection:
    def test_select_switches_and_persists(self, tmp_path):
        changed = []
        a, b = _FakeShow("alpha"), _FakeShow("beta")
        c = _controller(tmp_path, [a, b],
                        on_changed=lambda sid: changed.append(sid))
        c.enter()                 # alpha active (default, no persist)
        assert c.select_show("beta") is True
        assert c.active_show is b
        assert ("exit",) in a.calls   # old show exited
        assert ("enter",) in b.calls  # new show entered
        assert changed == ["beta"]    # selection persisted

    def test_select_unknown_returns_false(self, tmp_path):
        a = _FakeShow("alpha")
        c = _controller(tmp_path, [a])
        c.enter()
        assert c.select_show("ghost") is False
        assert c.active_show is a

    def test_select_same_show_is_noop(self, tmp_path):
        a = _FakeShow("alpha")
        c = _controller(tmp_path, [a])
        c.enter()
        a.calls.clear()
        assert c.select_show("alpha") is True
        # No re-enter / re-exit churn.
        assert a.calls == []

    def test_show_ids_in_registration_order(self, tmp_path):
        c = _controller(tmp_path, [_FakeShow("alpha"), _FakeShow("beta")])
        assert c.show_ids() == ["alpha", "beta"]

    def test_active_show_id(self, tmp_path):
        c = _controller(tmp_path, [_FakeShow("alpha")])
        assert c.active_show_id() is None
        c.enter()
        assert c.active_show_id() == "alpha"


class TestSensitivity:
    def test_applies_show_sensitivity_to_imu(self, tmp_path):
        imu = _FakeImu()
        # Show declares sensitivity default High (2).
        show = _FakeShow("alpha", sensitivity_default=2)
        c = _controller(tmp_path, [show], imu=imu)
        c.enter()
        assert imu.sensitivity == 2

    def test_no_sensitivity_property_leaves_imu_untouched(self, tmp_path):
        imu = _FakeImu()
        show = _FakeShow("alpha", sensitivity_default=None)  # no property
        c = _controller(tmp_path, [show], imu=imu)
        c.enter()
        assert imu.sensitivity is None

    def test_sensitivity_reapplied_on_show_change(self, tmp_path):
        imu = _FakeImu()
        a = _FakeShow("alpha", sensitivity_default=0)  # Low
        b = _FakeShow("beta", sensitivity_default=2)   # High
        c = _controller(tmp_path, [a, b], imu=imu)
        c.enter()
        assert imu.sensitivity == 0
        c.select_show("beta")
        assert imu.sensitivity == 2


class TestInputAction:
    def test_pause_toggles_ctx(self, tmp_path):
        c = _controller(tmp_path, [_FakeShow("alpha")])
        c.enter()
        assert c.active_context.paused() is False
        assert c.on_input_action(InputAction.PAUSE) == RESULT_HANDLED
        assert c.active_context.paused() is True
        c.on_input_action(InputAction.PAUSE)
        assert c.active_context.paused() is False

    def test_picker_returns_open_picker(self, tmp_path):
        c = _controller(tmp_path, [_FakeShow("alpha")])
        c.enter()
        assert c.on_input_action(InputAction.PICKER) == RESULT_OPEN_PICKER

    def test_settings_returns_open_settings(self, tmp_path):
        c = _controller(tmp_path, [_FakeShow("alpha")])
        c.enter()
        assert c.on_input_action(InputAction.SETTINGS) == RESULT_OPEN_SETTINGS

    def test_confirm_reaches_show(self, tmp_path):
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show])
        c.enter()
        assert c.on_input_action(InputAction.CONFIRM) == RESULT_HANDLED
        assert ("input", InputAction.CONFIRM) in show.calls

    def test_cycle_reaches_show(self, tmp_path):
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show])
        c.enter()
        c.on_input_action(InputAction.CYCLE)
        assert ("input", InputAction.CYCLE) in show.calls


class TestTick:
    def test_tick_hz_zero_never_ticks(self, tmp_path):
        show = _FakeShow("alpha", tick_hz=0)
        c = _controller(tmp_path, [show])
        c.enter()
        c.tick(0)
        c.tick(1000)
        assert not any(call[0] == "tick" for call in show.calls)

    def test_tick_hz_gates_to_interval(self, tmp_path):
        show = _FakeShow("alpha", tick_hz=10)  # 100 ms interval
        c = _controller(tmp_path, [show])
        c.enter()
        c.tick(0)      # first tick fires
        c.tick(50)     # < 100 ms -> no
        c.tick(100)    # >= 100 ms -> yes
        ticks = [call for call in show.calls if call[0] == "tick"]
        assert ticks == [("tick", 0), ("tick", 100)]


class TestPollButton:
    def test_poll_button_drives_button_tap_only(self, tmp_path):
        # poll_button must not also poll the IMU (the app drives the IMU
        # from its background loop separately).
        imu = _FakeImu()
        button = ButtonTapSource()
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show], imu=imu, button=button)
        c.enter()
        c.poll_button(True, 0)  # rising edge -> tap
        assert any(call[0] == "tap" for call in show.calls)


class TestApplySensitivity:
    def test_apply_sensitivity_repushes(self, tmp_path):
        imu = _FakeImu()
        show = _FakeShow("alpha", sensitivity_default=2)
        c = _controller(tmp_path, [show], imu=imu)
        c.enter()
        assert imu.sensitivity == 2
        # Simulate the operator changing the property, then re-applying.
        c.active_context.set_property("sensitivity", 0)
        c.apply_sensitivity()
        assert imu.sensitivity == 0

    def test_apply_sensitivity_no_active_show_is_safe(self, tmp_path):
        imu = _FakeImu()
        c = _controller(tmp_path, [], imu=imu)
        c.apply_sensitivity()  # no active show; must not crash


class TestLifecycle:
    def test_exit_calls_show_exit(self, tmp_path):
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show])
        c.enter()
        c.exit()
        assert ("exit",) in show.calls

    def test_context_bound_to_show(self, tmp_path):
        show = _FakeShow("alpha")
        c = _controller(tmp_path, [show])
        c.enter()
        # bind_context made show.context() return the active context.
        assert show.context() is c.active_context
