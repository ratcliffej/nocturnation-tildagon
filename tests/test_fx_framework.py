"""FX framework tests (Epic 10 B1).

Covers the registry / runner contracts. Concrete FX implementations
land in B3 with their own per-effect tests.
"""

import pytest

from nocturnation.fx import Fx, FxRegistry, FxRunner
from nocturnation.protocol import (
    FX_FLAG_REPLACE_RUNNING,
    FX_FLAG_START,
    make_light_fx_run_frame,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class RecordingFx(Fx):
    """Minimal Fx that records every lifecycle call so the runner can
    be inspected. Subclasses set their own id; multiple recording FXes
    can coexist in one test by varying the id."""

    id = 100
    name = "RecordingFx"
    category = "test"

    def __init__(self):
        super().__init__()
        self.starts = []
        self.ticks = []
        self.cancels = []
        # When set non-None, is_finished returns True at that now_ms.
        self.finish_at_ms = None

    def start(self, *, bpm, buildup_s, params, position_ms, now_ms):
        # NOTE: skip the base class's NotImplementedError; we just record.
        self._started_ms = now_ms
        self._cancelled_ms = None
        self.starts.append({
            "bpm": bpm, "buildup_s": buildup_s, "params": params,
            "position_ms": position_ms, "now_ms": now_ms,
        })

    def tick(self, now_ms):
        self.ticks.append(now_ms)

    def cancel(self, now_ms):
        self._cancelled_ms = now_ms
        self.cancels.append(now_ms)

    def is_finished(self, now_ms):
        # If a test sets finish_at_ms, that drives the lifetime
        # explicitly (lets us simulate a release fade-out window
        # without the base class's "finished on cancel" default).
        if self.finish_at_ms is not None:
            return now_ms >= self.finish_at_ms
        return self._cancelled_ms is not None


def make_recording_class(fx_id, name="Recording"):
    """Build a fresh Fx subclass with the given id. Returns the class."""
    return type(name, (RecordingFx,), {"id": fx_id, "name": name})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestFxRegistry:
    def test_register_and_lookup(self):
        reg = FxRegistry()
        Cls = make_recording_class(11, "Sparkle")
        reg.register(Cls)
        assert reg.get(11) is Cls
        assert reg.has(11)

    def test_unknown_id_returns_none(self):
        reg = FxRegistry()
        assert reg.get(99) is None
        assert not reg.has(99)

    def test_duplicate_id_rejected(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11, "A"))
        with pytest.raises(ValueError):
            reg.register(make_recording_class(11, "B"))

    def test_reserved_ids_rejected(self):
        reg = FxRegistry()
        with pytest.raises(ValueError):
            reg.register(make_recording_class(0, "Cancel"))
        with pytest.raises(ValueError):
            reg.register(make_recording_class(255, "Reserved"))

    def test_decorator_returns_cls(self):
        # The decorator should return the class so the user can still
        # reference it.
        reg = FxRegistry()
        Cls = make_recording_class(11)
        ret = reg.register(Cls)
        assert ret is Cls

    def test_all_ids_sorted(self):
        reg = FxRegistry()
        reg.register(make_recording_class(21, "Buildup"))
        reg.register(make_recording_class(7, "Fade"))
        reg.register(make_recording_class(11, "Sparkle"))
        assert reg.all_ids() == [7, 11, 21]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TestFxRunnerStart:
    def test_unknown_fx_id_drops_silently(self):
        reg = FxRegistry()
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=99), now_ms=0,
        )
        assert runner.current_fx is None
        assert runner.stats()["unknown_fx_drops"] == 1

    def test_start_admits_known_fx(self):
        reg = FxRegistry()
        Cls = make_recording_class(11)
        reg.register(Cls)
        runner = FxRunner(reg, default_bpm=120)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(
                fx_id=11, bpm=138, buildup_s=4,
                params=(80, 200, 200, 200, 0, 0),
                position_ms=1234,
            ),
            now_ms=100,
        )
        assert isinstance(runner.current_fx, Cls)
        rec = runner.current_fx.starts[0]
        assert rec["bpm"] == 138
        assert rec["buildup_s"] == 4
        assert rec["params"] == (80, 200, 200, 200, 0, 0)
        assert rec["position_ms"] == 1234
        assert rec["now_ms"] == 100

    def test_bpm_zero_falls_back_to_default(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg, default_bpm=124)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11, bpm=0),
            now_ms=0,
        )
        assert runner.current_fx.starts[0]["bpm"] == 124


class TestFxRunnerCancelAndReplace:
    def test_cancel_with_fx_id_zero(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11), now_ms=0,
        )
        first = runner.current_fx
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=0), now_ms=100,
        )
        assert runner.current_fx is None
        # The cancelled FX moves to cancelling_fx until is_finished.
        assert runner.cancelling_fx is first
        assert first.cancels == [100]

    def test_new_fx_id_supersedes_running(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11, "A"))
        reg.register(make_recording_class(13, "B"))
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11), now_ms=0,
        )
        first = runner.current_fx
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=13), now_ms=100,
        )
        assert runner.current_fx is not first
        assert runner.current_fx.id == 13
        assert runner.cancelling_fx is first
        assert first.cancels == [100]

    def test_same_fx_id_is_idempotent_without_replace_flag(self):
        # A re-broadcast with the same fx_id (e.g. orchestrator's 5 s
        # position re-emit) MUST NOT restart the running FX. Otherwise
        # the receiver would judder on every re-broadcast.
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11, flags=FX_FLAG_START),
            now_ms=0,
        )
        first = runner.current_fx
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11, flags=FX_FLAG_START),
            now_ms=5_000,
        )
        # Same instance still running; not cancelled.
        assert runner.current_fx is first
        assert first.cancels == []

    def test_replace_running_flag_forces_restart(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11, flags=FX_FLAG_START),
            now_ms=0,
        )
        first = runner.current_fx
        runner.on_fx_run_frame(
            make_light_fx_run_frame(
                fx_id=11,
                flags=FX_FLAG_START | FX_FLAG_REPLACE_RUNNING,
            ),
            now_ms=5_000,
        )
        # Old cancelled, new started.
        assert runner.current_fx is not first
        assert first.cancels == [5_000]


class TestFxRunnerTick:
    def test_tick_calls_current_fx(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11), now_ms=0,
        )
        fx = runner.current_fx
        runner.tick(now_ms=10)
        runner.tick(now_ms=20)
        assert fx.ticks == [10, 20]

    def test_tick_ticks_cancelling_fx_through_release(self):
        # After cancel, the FX should keep getting tick() calls until
        # is_finished() returns True, so it can render its fade-out.
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg)
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11), now_ms=0,
        )
        fx = runner.current_fx
        # Configure the FX to take 100 ms to finish releasing.
        fx.finish_at_ms = 200
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=0), now_ms=100,
        )
        # Mid-release: still ticking.
        runner.tick(now_ms=150)
        assert 150 in fx.ticks
        assert runner.cancelling_fx is fx
        # Past finish: dropped.
        runner.tick(now_ms=250)
        assert 250 in fx.ticks
        assert runner.cancelling_fx is None


class TestFxRunnerStats:
    def test_counters(self):
        reg = FxRegistry()
        reg.register(make_recording_class(11))
        runner = FxRunner(reg)

        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=99), now_ms=0,
        )
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=11), now_ms=10,
        )
        runner.on_fx_run_frame(
            make_light_fx_run_frame(fx_id=0), now_ms=20,
        )

        stats = runner.stats()
        assert stats["unknown_fx_drops"] == 1
        assert stats["runs_started"] == 1
        assert stats["runs_cancelled"] == 1
