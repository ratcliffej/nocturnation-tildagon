"""IMU input adapter for the Tildagon Director.

Turns the badge accelerometer into NocturNation input events:

  - **Tap** -> on_tap callback. A sharp transient spike in the
    gravity-removed (high-pass) acceleration vector. This is the
    Director's primary tap-to-beat input.
  - **Motion** -> on_motion callback. Sustained movement (waving the
    badge around) reported as a dominant axis + magnitude.

The adapter is pure logic with the hardware read injected, matching
the PerimeterRenderer's `rng` pattern: pass `acc_read_fn` (a
callable returning an (x, y, z) tuple in m/s^2). On the badge this
defaults to the built-in `imu.acc_read`; in host tests it's a fake
that replays a canned acceleration trace.

Detection pipeline (per poll, ~50 Hz from the app's background_task):

  1. Maintain a slow EMA of each axis as the gravity vector.
  2. High-pass = raw - gravity (removes the ~9.81 m/s^2 gravity bias
     and any slow tilt).
  3. tap_mag = |high-pass|. A tap fires when tap_mag crosses the
     sensitivity-scaled tap threshold and we're past the refractory
     window. Strength scales the over-threshold magnitude to 0..255.
  4. A slower EMA of tap_mag is the motion envelope. Motion fires
     when the envelope sits above the (lower) motion floor, rate-
     limited, and is suppressed during the tap refractory window so a
     single tap doesn't also read as motion.

Thresholds are starting points for bench tuning (Epic 6B B9); the
sensitivity property (Low/Medium/High, a per-Show setting from B1)
scales them.

The IMU sub-capabilities this adapter provides are exported as
`IMU_ADAPTER_CAPS` so the Director host can declare them
(`host.set_imu_caps(IMU_ADAPTER_CAPS)`) and Shows that require
`Capability.IMU_TAP` gate on correctly.
"""

import math

from ..hal import Capability, CapabilityMask


# Sensitivity levels. Match the per-Show `sensitivity` enum property
# (default Medium) declared in Epic 6B B1.
SENSITIVITY_LOW = 0
SENSITIVITY_MEDIUM = 1
SENSITIVITY_HIGH = 2

# Per-sensitivity tuning. All accelerations are in m/s^2 of the
# gravity-removed (high-pass) vector. Higher sensitivity = lower
# thresholds = more responsive (and more false positives).
#
#   tap_threshold  - high-pass magnitude that counts as a tap onset
#   motion_floor   - motion-envelope level that counts as "in motion"
#
# Bench-tuned on real hardware (Epic 6B B9): a hand-held finger-tap on
# the Tildagon delivers only ~1.0-2.6 m/s^2 of high-pass acceleration
# (the hand absorbs most of the impulse), not the ~6-9 the first-cut
# values assumed. The thresholds below sit inside that measured band.
# `tap_threshold` stays above `motion_floor` at each level so a sharp
# tap reads as a tap while gentler sustained movement reads as motion -
# though on this badge the two bands nearly overlap, so expect brisk
# waving to occasionally register a tap.
#
# tap strength saturates `TAP_SATURATION_MS2` above the threshold.
_SENSITIVITY_TABLE = {
    SENSITIVITY_LOW:    {"tap_threshold": 2.2, "motion_floor": 1.5},
    SENSITIVITY_MEDIUM: {"tap_threshold": 1.5, "motion_floor": 1.0},
    # High retuned down (B9): a normal badge tap still needed too firm a
    # hit at 1.0, so trigger at 0.6 with the motion floor below it.
    SENSITIVITY_HIGH:   {"tap_threshold": 0.6, "motion_floor": 0.35},
}

# Magnitude above the tap threshold that maps to full strength (255).
# Real taps clear the threshold by only ~1-1.5 m/s^2, so the saturation
# span is small (was 10.0, tuned for the old hard-hit assumption).
TAP_SATURATION_MS2 = 2.0

# Minimum gap between taps. A second spike inside this window is the
# same physical tap ringing down; drop it. Mirrors the M5
# BeatDetector's refractory concept (200 ms there; taps are crisper
# than kicks so a shorter window reads double-taps as two events).
TAP_REFRACTORY_MS = 120

# Minimum gap between motion events so a sustained wave doesn't flood
# the active Show at the full 50 Hz poll rate.
MOTION_INTERVAL_MS = 100

# EMA smoothing factors.
#   gravity: slow, so a tap/wave doesn't drag the gravity estimate
#   motion:  faster, tracks the sustained-movement envelope
_GRAVITY_ALPHA = 0.05
_MOTION_ALPHA = 0.30

# IMU sub-capabilities this adapter produces. The Director host
# declares these so Shows requiring IMU_TAP / IMU_MOTION gate on.
IMU_ADAPTER_CAPS = CapabilityMask(Capability.IMU_TAP, Capability.IMU_MOTION)

# Axis indices reported to on_motion (match the Show hook contract:
# 0=X, 1=Y, 2=Z, 3=magnitude). The adapter reports the dominant axis.
AXIS_X = 0
AXIS_Y = 1
AXIS_Z = 2


def _clamp_u8(v):
    if v < 0:
        return 0
    if v > 255:
        return 255
    return int(v)


class ImuAdapter:
    """Accelerometer -> tap / motion events.

    Construct with:
      acc_read_fn - callable() -> (x, y, z) in m/s^2. Defaults to the
                    badge's `imu.acc_read` (lazy-imported so host
                    tests that inject a fake never touch the module).
      on_tap      - callable(strength) where strength is 0..255.
      on_motion   - callable(axis, magnitude) where axis is 0/1/2 and
                    magnitude is 0..255.
      sensitivity - SENSITIVITY_LOW / _MEDIUM / _HIGH.

    Drive it by calling `poll(now_ms)` at ~50 Hz from the app loop.
    """

    __slots__ = (
        "_acc_read",
        "_on_tap",
        "_on_motion",
        "_tap_threshold",
        "_motion_floor",
        "_gravity",
        "_motion_env",
        "_primed",
        "_last_tap_ms",
        "_last_motion_ms",
    )

    def __init__(self, acc_read_fn=None, on_tap=None, on_motion=None,
                 sensitivity=SENSITIVITY_MEDIUM):
        if acc_read_fn is None:
            import imu  # badge built-in; only imported when not injected
            acc_read_fn = imu.acc_read
        self._acc_read = acc_read_fn
        self._on_tap = on_tap
        self._on_motion = on_motion

        self._gravity = None       # (x, y, z) EMA; None until primed
        self._motion_env = 0.0     # smoothed high-pass magnitude
        self._primed = False
        self._last_tap_ms = None
        self._last_motion_ms = None

        self.set_sensitivity(sensitivity)

    def set_sensitivity(self, level):
        """Set the detection sensitivity (Low/Medium/High). Unknown
        values fall back to Medium so a corrupt property can't disable
        input entirely."""
        cfg = _SENSITIVITY_TABLE.get(level, _SENSITIVITY_TABLE[SENSITIVITY_MEDIUM])
        self._tap_threshold = cfg["tap_threshold"]
        self._motion_floor = cfg["motion_floor"]

    def set_callbacks(self, on_tap=None, on_motion=None):
        """Re-point the event callbacks (e.g. when the active Show
        changes). Passing None leaves a callback unset."""
        self._on_tap = on_tap
        self._on_motion = on_motion

    def reset(self):
        """Forget the gravity estimate and timing state. Use on
        Director-mode entry so the first poll re-primes cleanly."""
        self._gravity = None
        self._motion_env = 0.0
        self._primed = False
        self._last_tap_ms = None
        self._last_motion_ms = None

    def poll(self, now_ms):
        """Read the accelerometer once and emit any events.

        Returns a tuple (tap_fired, motion_fired) of bools, useful for
        tests and diagnostics. Callbacks are invoked as a side effect.
        """
        ax, ay, az = self._acc_read()

        # Prime the gravity estimate on the first reading; no detection
        # until we have a baseline (otherwise the initial gravity bias
        # reads as a giant tap).
        if not self._primed:
            self._gravity = (ax, ay, az)
            self._primed = True
            return (False, False)

        gx, gy, gz = self._gravity
        gx += _GRAVITY_ALPHA * (ax - gx)
        gy += _GRAVITY_ALPHA * (ay - gy)
        gz += _GRAVITY_ALPHA * (az - gz)
        self._gravity = (gx, gy, gz)

        # High-pass (gravity-removed) acceleration.
        hx = ax - gx
        hy = ay - gy
        hz = az - gz
        tap_mag = math.sqrt(hx * hx + hy * hy + hz * hz)

        # Motion envelope: a slower EMA of the instantaneous magnitude.
        self._motion_env += _MOTION_ALPHA * (tap_mag - self._motion_env)

        tap_fired = self._maybe_tap(tap_mag, now_ms)
        motion_fired = self._maybe_motion(hx, hy, hz, now_ms, tap_fired)
        return (tap_fired, motion_fired)

    def _maybe_tap(self, tap_mag, now_ms):
        if tap_mag < self._tap_threshold:
            return False
        if self._last_tap_ms is not None and (now_ms - self._last_tap_ms) < TAP_REFRACTORY_MS:
            return False
        self._last_tap_ms = now_ms
        over = tap_mag - self._tap_threshold
        strength = _clamp_u8(over / TAP_SATURATION_MS2 * 255.0)
        # A tap should always be visible even at the threshold edge;
        # floor the strength so a just-over tap isn't strength 0.
        if strength < 1:
            strength = 1
        if self._on_tap is not None:
            self._on_tap(strength)
        return True

    def _maybe_motion(self, hx, hy, hz, now_ms, tap_fired):
        # Suppress motion during/at a tap so one tap doesn't double-
        # report as motion.
        if tap_fired:
            return False
        if self._motion_env < self._motion_floor:
            return False
        if self._last_motion_ms is not None and (now_ms - self._last_motion_ms) < MOTION_INTERVAL_MS:
            return False
        self._last_motion_ms = now_ms

        # Dominant axis = largest absolute high-pass component.
        axis = AXIS_X
        biggest = abs(hx)
        if abs(hy) > biggest:
            axis = AXIS_Y
            biggest = abs(hy)
        if abs(hz) > biggest:
            axis = AXIS_Z
            biggest = abs(hz)

        # Magnitude reported as the motion envelope scaled to 0..255,
        # saturating at the same range as taps.
        magnitude = _clamp_u8(self._motion_env / TAP_SATURATION_MS2 * 255.0)
        if self._on_motion is not None:
            self._on_motion(axis, magnitude)
        return True
