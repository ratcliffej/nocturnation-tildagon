"""ImuAdapter tests.

The adapter turns accelerometer samples into tap / motion events.
Hardware is injected as a fake `acc_read_fn` (matching the
PerimeterRenderer's injected-rng pattern), so these run on the host
with no badge.

Gravity is primed to (0, 0, 9.81) by the first sample in each trace;
the gravity EMA (alpha 0.05) then tracks slowly while the high-pass
captures transients. With a single-sample Z spike after priming the
high-pass works out to ~0.95 * (az - 9.81), so `_az_for_hp` inverts
that to place a tap at a chosen magnitude.

Thresholds are read from `_SENSITIVITY_TABLE` rather than hardcoded,
so these tests track the bench-tuned values instead of breaking on
every retune.
"""

from nocturnation.director import (
    ImuAdapter,
    IMU_ADAPTER_CAPS,
    SENSITIVITY_LOW,
    SENSITIVITY_MEDIUM,
    SENSITIVITY_HIGH,
)
from nocturnation.director.imu import (
    AXIS_X, AXIS_Y, AXIS_Z,
    _SENSITIVITY_TABLE,
    TAP_SATURATION_MS2,
)
from nocturnation.hal import Capability


G = 9.81  # resting gravity on Z, m/s^2


def _tap_thr(level):
    return _SENSITIVITY_TABLE[level]["tap_threshold"]


def _motion_floor(level):
    return _SENSITIVITY_TABLE[level]["motion_floor"]


def _az_for_hp(hp):
    """Z accel that, after gravity priming at G, yields high-pass `hp`."""
    return G + hp / 0.95


class _Recorder:
    """Collects tap / motion callbacks for assertions."""

    def __init__(self):
        self.taps = []
        self.motions = []

    def on_tap(self, strength):
        self.taps.append(strength)

    def on_motion(self, axis, magnitude):
        self.motions.append((axis, magnitude))


def _adapter(rec, sensitivity=SENSITIVITY_MEDIUM, acc_read_fn=None):
    return ImuAdapter(
        acc_read_fn=acc_read_fn,
        on_tap=rec.on_tap,
        on_motion=rec.on_motion,
        sensitivity=sensitivity,
    )


def _trace_reader(samples):
    """Return an acc_read_fn that walks `samples`, holding the last."""
    state = {"i": 0}

    def read():
        i = min(state["i"], len(samples) - 1)
        state["i"] += 1
        return samples[i]

    return read


def _spike(hp, level=SENSITIVITY_MEDIUM):
    """A prime-then-spike trace placing a single tap at high-pass `hp`."""
    return [(0, 0, G), (0, 0, _az_for_hp(hp))]


class TestCapabilityExport:
    def test_adapter_caps_are_tap_and_motion(self):
        assert IMU_ADAPTER_CAPS.has(Capability.IMU_TAP) is True
        assert IMU_ADAPTER_CAPS.has(Capability.IMU_MOTION) is True
        assert IMU_ADAPTER_CAPS.has(Capability.IMU) is False  # coarse flag not implied


class TestThresholdOrdering:
    def test_tap_threshold_above_motion_floor(self):
        # A sharp tap must read harder than gentle sustained motion at
        # every sensitivity level, or the two can't be told apart.
        for level in (SENSITIVITY_LOW, SENSITIVITY_MEDIUM, SENSITIVITY_HIGH):
            assert _tap_thr(level) > _motion_floor(level)

    def test_higher_sensitivity_lowers_thresholds(self):
        assert _tap_thr(SENSITIVITY_HIGH) < _tap_thr(SENSITIVITY_MEDIUM) < _tap_thr(SENSITIVITY_LOW)


class TestPriming:
    def test_first_poll_primes_no_events(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader([(0, 0, G)]))
        assert a.poll(0) == (False, False)
        assert rec.taps == []
        assert rec.motions == []

    def test_steady_gravity_no_events(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader([(0, 0, G)] * 10))
        for t in range(10):
            a.poll(t * 20)
        assert rec.taps == []
        assert rec.motions == []


class TestTapDetection:
    def test_spike_fires_tap(self):
        rec = _Recorder()
        hp = _tap_thr(SENSITIVITY_MEDIUM) + 1.0
        a = _adapter(rec, acc_read_fn=_trace_reader(_spike(hp)))
        a.poll(0)        # prime
        tap, _motion = a.poll(20)
        assert tap is True
        assert len(rec.taps) == 1
        assert rec.taps[0] >= 1  # strength floor invariant

    def test_just_over_threshold_floors_strength_to_at_least_1(self):
        rec = _Recorder()
        hp = _tap_thr(SENSITIVITY_MEDIUM) + 0.01
        a = _adapter(rec, acc_read_fn=_trace_reader(_spike(hp)))
        a.poll(0)
        a.poll(20)
        assert rec.taps == [max(1, rec.taps[0])]
        assert rec.taps[0] >= 1

    def test_strength_increases_with_spike(self):
        rec_small = _Recorder()
        a_small = _adapter(rec_small, acc_read_fn=_trace_reader(
            _spike(_tap_thr(SENSITIVITY_MEDIUM) + 0.4)))
        a_small.poll(0); a_small.poll(20)

        rec_big = _Recorder()
        a_big = _adapter(rec_big, acc_read_fn=_trace_reader(
            _spike(_tap_thr(SENSITIVITY_MEDIUM) + 1.5)))
        a_big.poll(0); a_big.poll(20)

        assert rec_small.taps[0] < rec_big.taps[0]

    def test_huge_spike_saturates_strength(self):
        rec = _Recorder()
        hp = _tap_thr(SENSITIVITY_MEDIUM) + TAP_SATURATION_MS2 + 5.0
        a = _adapter(rec, acc_read_fn=_trace_reader(_spike(hp)))
        a.poll(0)
        a.poll(20)
        assert rec.taps[0] == 255

    def test_below_threshold_no_tap(self):
        rec = _Recorder()
        # Below the tap threshold and below the motion floor (a single
        # spike only moves the motion envelope by ~0.30 * hp).
        hp = _tap_thr(SENSITIVITY_MEDIUM) - 0.3
        a = _adapter(rec, acc_read_fn=_trace_reader(_spike(hp)))
        a.poll(0)
        tap, motion = a.poll(20)
        assert tap is False
        assert rec.taps == []


class TestRefractory:
    def test_second_spike_within_refractory_dropped(self):
        rec = _Recorder()
        hp = _tap_thr(SENSITIVITY_MEDIUM) + 1.0
        a = _adapter(rec, acc_read_fn=_trace_reader(
            [(0, 0, G), (0, 0, _az_for_hp(hp)), (0, 0, _az_for_hp(hp))]))
        a.poll(0)            # prime
        a.poll(20)           # tap 1
        tap2, _ = a.poll(60)  # 40 ms later, inside 120 ms refractory
        assert tap2 is False
        assert len(rec.taps) == 1

    def test_second_spike_after_refractory_fires(self):
        rec = _Recorder()
        hp = _tap_thr(SENSITIVITY_MEDIUM) + 1.0
        a = _adapter(rec, acc_read_fn=_trace_reader(
            [(0, 0, G), (0, 0, _az_for_hp(hp)), (0, 0, G), (0, 0, _az_for_hp(hp))]))
        a.poll(0)             # prime
        a.poll(20)            # tap 1
        a.poll(40)            # back to gravity-ish
        tap2, _ = a.poll(200)  # 180 ms after tap 1, past refractory
        assert tap2 is True
        assert len(rec.taps) == 2


class TestSensitivity:
    def test_high_fires_where_medium_does_not(self):
        # A tap between the High and Medium thresholds.
        hp = (_tap_thr(SENSITIVITY_HIGH) + _tap_thr(SENSITIVITY_MEDIUM)) / 2.0
        trace = _spike(hp)

        med = _Recorder()
        a_med = _adapter(med, sensitivity=SENSITIVITY_MEDIUM,
                         acc_read_fn=_trace_reader(trace))
        a_med.poll(0); a_med.poll(20)
        assert med.taps == []

        high = _Recorder()
        a_high = _adapter(high, sensitivity=SENSITIVITY_HIGH,
                          acc_read_fn=_trace_reader(trace))
        a_high.poll(0); a_high.poll(20)
        assert len(high.taps) == 1

    def test_low_needs_firmer_tap(self):
        # A tap between the Medium and Low thresholds.
        hp = (_tap_thr(SENSITIVITY_MEDIUM) + _tap_thr(SENSITIVITY_LOW)) / 2.0
        trace = _spike(hp)

        low = _Recorder()
        a_low = _adapter(low, sensitivity=SENSITIVITY_LOW,
                         acc_read_fn=_trace_reader(trace))
        a_low.poll(0); a_low.poll(20)
        assert low.taps == []

        med = _Recorder()
        a_med = _adapter(med, sensitivity=SENSITIVITY_MEDIUM,
                         acc_read_fn=_trace_reader(trace))
        a_med.poll(0); a_med.poll(20)
        assert len(med.taps) == 1

    def test_set_sensitivity_runtime(self):
        hp = (_tap_thr(SENSITIVITY_HIGH) + _tap_thr(SENSITIVITY_MEDIUM)) / 2.0
        trace = _spike(hp)
        rec = _Recorder()
        a = _adapter(rec, sensitivity=SENSITIVITY_MEDIUM,
                     acc_read_fn=_trace_reader(trace))
        a.set_sensitivity(SENSITIVITY_HIGH)
        a.poll(0); a.poll(20)
        assert len(rec.taps) == 1

    def test_unknown_sensitivity_falls_back_to_medium(self):
        hp = _tap_thr(SENSITIVITY_MEDIUM) + 1.0  # fires at Medium
        trace = _spike(hp)
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(trace))
        a.set_sensitivity(99)  # nonsense -> Medium
        a.poll(0); a.poll(20)
        assert len(rec.taps) == 1


class TestMotion:
    def _oscillation(self, n, amp):
        # Z oscillates +/- amp around gravity: a "wave". |hz| ~ 0.95*amp
        # each poll; gravity stays ~G because the swings cancel.
        samples = [(0, 0, G)]  # prime
        for i in range(n):
            samples.append((0, 0, G + (amp if i % 2 == 0 else -amp)))
        return samples

    def _amp_for_hp(self, hp):
        return hp / 0.95

    def test_sustained_motion_fires(self):
        # |hz| sits between the motion floor and the tap threshold, so a
        # wave reads as motion without tripping taps.
        med_motion = _motion_floor(SENSITIVITY_MEDIUM)
        med_tap = _tap_thr(SENSITIVITY_MEDIUM)
        amp = self._amp_for_hp((med_motion + med_tap) / 2.0)
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(self._oscillation(20, amp)))
        for i in range(21):
            a.poll(i * 20)  # 50 Hz
        assert len(rec.motions) >= 1
        assert rec.taps == []  # below the tap threshold
        axis, mag = rec.motions[0]
        assert axis == AXIS_Z
        assert mag > 0

    def test_motion_rate_limited(self):
        med_motion = _motion_floor(SENSITIVITY_MEDIUM)
        med_tap = _tap_thr(SENSITIVITY_MEDIUM)
        amp = self._amp_for_hp((med_motion + med_tap) / 2.0)
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(self._oscillation(40, amp)))
        # 41 polls at 20 ms = 800 ms. Motion interval 100 ms -> at most
        # ~9 events even though the wave is continuous.
        for i in range(41):
            a.poll(i * 20)
        assert len(rec.motions) <= 9

    def test_no_motion_below_floor(self):
        # |hz| under the motion floor -> the envelope never crosses it.
        amp = self._amp_for_hp(_motion_floor(SENSITIVITY_MEDIUM) * 0.5)
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(self._oscillation(20, amp)))
        for i in range(21):
            a.poll(i * 20)
        assert rec.motions == []

    def test_tap_suppresses_motion_same_poll(self):
        # Build the envelope with a wave, then a firm spike. On the spike
        # poll the tap fires and motion is suppressed.
        med_motion = _motion_floor(SENSITIVITY_MEDIUM)
        med_tap = _tap_thr(SENSITIVITY_MEDIUM)
        amp = self._amp_for_hp((med_motion + med_tap) / 2.0)
        samples = [(0, 0, G)]
        for i in range(12):
            samples.append((0, 0, G + (amp if i % 2 == 0 else -amp)))
        samples.append((0, 0, _az_for_hp(med_tap + 2.0)))  # firm tap
        a = _adapter(_Recorder(), acc_read_fn=_trace_reader(samples))
        results = []
        for i in range(len(samples)):
            results.append(a.poll(i * 20))
        tap, motion = results[-1]
        assert tap is True
        assert motion is False


class TestReset:
    def test_reset_reprimes(self):
        rec = _Recorder()
        hp = _tap_thr(SENSITIVITY_MEDIUM) + 1.0
        a = _adapter(rec, acc_read_fn=_trace_reader(
            [(0, 0, G), (0, 0, _az_for_hp(hp)), (0, 0, _az_for_hp(hp))]))
        a.poll(0)    # prime
        a.poll(20)   # tap
        assert len(rec.taps) == 1
        a.reset()
        # Next poll re-primes (no tap even though it's a spike value).
        tap, _ = a.poll(40)
        assert tap is False
        assert len(rec.taps) == 1
