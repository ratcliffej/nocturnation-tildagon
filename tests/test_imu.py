"""ImuAdapter tests.

The adapter turns accelerometer samples into tap / motion events.
Hardware is injected as a fake `acc_read_fn` (matching the
PerimeterRenderer's injected-rng pattern), so these run on the host
with no badge.

Gravity is primed to (0, 0, 9.81) by the first sample in each trace;
the gravity EMA (alpha 0.05) then tracks slowly while the high-pass
captures transients. With a single-sample spike after priming, the
high-pass Z works out to ~0.95 * (az - 9.81).
"""

from nocturnation.director import (
    ImuAdapter,
    IMU_ADAPTER_CAPS,
    SENSITIVITY_LOW,
    SENSITIVITY_MEDIUM,
    SENSITIVITY_HIGH,
)
from nocturnation.director.imu import AXIS_X, AXIS_Y, AXIS_Z
from nocturnation.hal import Capability


G = 9.81  # resting gravity on Z, m/s^2


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


class TestCapabilityExport:
    def test_adapter_caps_are_tap_and_motion(self):
        assert IMU_ADAPTER_CAPS.has(Capability.IMU_TAP) is True
        assert IMU_ADAPTER_CAPS.has(Capability.IMU_MOTION) is True
        assert IMU_ADAPTER_CAPS.has(Capability.IMU) is False  # coarse flag not implied


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
        # prime at gravity, then one Z spike well above the Medium
        # threshold (hz ~ 0.95 * (18 - 9.81) = 7.78 > 6.0).
        a = _adapter(rec, acc_read_fn=_trace_reader([(0, 0, G), (0, 0, 18.0)]))
        a.poll(0)        # prime
        tap, _motion = a.poll(20)
        assert tap is True
        assert len(rec.taps) == 1
        assert rec.taps[0] >= 1  # strength floor invariant

    def test_strength_scales_with_spike(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader([(0, 0, G), (0, 0, 18.0)]))
        a.poll(0)
        a.poll(20)
        # over = 7.78 - 6.0 = 1.78; strength = 1.78/10*255 ~ 45.
        assert 43 <= rec.taps[0] <= 47

    def test_huge_spike_saturates_strength(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader([(0, 0, G), (0, 0, 100.0)]))
        a.poll(0)
        a.poll(20)
        assert rec.taps[0] == 255

    def test_below_threshold_no_tap(self):
        rec = _Recorder()
        # hz ~ 0.95 * (13 - 9.81) = 3.03, below Medium 6.0.
        a = _adapter(rec, acc_read_fn=_trace_reader([(0, 0, G), (0, 0, 13.0)]))
        a.poll(0)
        tap, _ = a.poll(20)
        assert tap is False
        assert rec.taps == []


class TestRefractory:
    def test_second_spike_within_refractory_dropped(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(
            [(0, 0, G), (0, 0, 18.0), (0, 0, 18.0)]))
        a.poll(0)            # prime
        a.poll(20)           # tap 1
        tap2, _ = a.poll(60)  # 40 ms later, inside 120 ms refractory
        assert tap2 is False
        assert len(rec.taps) == 1

    def test_second_spike_after_refractory_fires(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(
            [(0, 0, G), (0, 0, 18.0), (0, 0, G), (0, 0, 18.0)]))
        a.poll(0)             # prime
        a.poll(20)            # tap 1
        a.poll(40)            # back to gravity-ish
        tap2, _ = a.poll(200)  # 180 ms after tap 1, past refractory
        assert tap2 is True
        assert len(rec.taps) == 2


class TestSensitivity:
    def test_high_fires_where_medium_does_not(self):
        # hz ~ 0.95 * (15 - 9.81) = 4.93. Above High (3.5), below
        # Medium (6.0).
        trace = [(0, 0, G), (0, 0, 15.0)]

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
        # hz ~ 0.95 * (17 - 9.81) = 6.83. Above Medium (6.0), below
        # Low (9.0).
        trace = [(0, 0, G), (0, 0, 17.0)]

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
        trace = [(0, 0, G), (0, 0, 15.0)]
        rec = _Recorder()
        a = _adapter(rec, sensitivity=SENSITIVITY_MEDIUM,
                     acc_read_fn=_trace_reader(trace))
        a.set_sensitivity(SENSITIVITY_HIGH)
        a.poll(0); a.poll(20)
        assert len(rec.taps) == 1

    def test_unknown_sensitivity_falls_back_to_medium(self):
        trace = [(0, 0, G), (0, 0, 18.0)]  # fires at Medium
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(trace))
        a.set_sensitivity(99)  # nonsense
        a.poll(0); a.poll(20)
        assert len(rec.taps) == 1


class TestMotion:
    def _oscillation(self, n, amp=5.0):
        # Z oscillates +/- amp around gravity: a "wave". |hz| ~ 0.95*amp
        # each poll (below the 6.0 tap threshold for amp=5), envelope
        # builds above the motion floor, gravity stays ~G.
        samples = [(0, 0, G)]  # prime
        for i in range(n):
            samples.append((0, 0, G + (amp if i % 2 == 0 else -amp)))
        return samples

    def test_sustained_motion_fires(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(self._oscillation(20)))
        for i in range(21):
            a.poll(i * 20)  # 50 Hz
        assert len(rec.motions) >= 1
        axis, mag = rec.motions[0]
        assert axis == AXIS_Z
        assert mag > 0

    def test_motion_rate_limited(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(self._oscillation(40)))
        # 41 polls at 20 ms = 800 ms. Motion interval 100 ms -> at most
        # ~8 events even though the wave is continuous.
        for i in range(41):
            a.poll(i * 20)
        assert len(rec.motions) <= 9

    def test_no_motion_below_floor(self):
        rec = _Recorder()
        # amp=1.5 -> |hz| ~ 1.4, envelope stays under Medium floor 2.5.
        a = _adapter(rec, acc_read_fn=_trace_reader(self._oscillation(20, amp=1.5)))
        for i in range(21):
            a.poll(i * 20)
        assert rec.motions == []

    def test_tap_suppresses_motion_same_poll(self):
        rec = _Recorder()
        # Build the envelope with a wave, then a big spike. On the spike
        # poll the tap fires and motion must be suppressed.
        samples = [(0, 0, G)]
        for i in range(12):
            samples.append((0, 0, G + (5.0 if i % 2 == 0 else -5.0)))
        samples.append((0, 0, 25.0))  # firm tap
        a = _adapter(rec, acc_read_fn=_trace_reader(samples))
        results = []
        for i in range(len(samples)):
            results.append(a.poll(i * 20))
        # Last poll is the spike: tap True, motion False.
        tap, motion = results[-1]
        assert tap is True
        assert motion is False


class TestReset:
    def test_reset_reprimes(self):
        rec = _Recorder()
        a = _adapter(rec, acc_read_fn=_trace_reader(
            [(0, 0, G), (0, 0, 18.0), (0, 0, 18.0)]))
        a.poll(0)    # prime
        a.poll(20)   # tap
        assert len(rec.taps) == 1
        a.reset()
        # Next poll re-primes (no tap even though it's a spike value).
        tap, _ = a.poll(40)
        assert tap is False
        assert len(rec.taps) == 1
