"""Perimeter LED renderer for the Tildagon's twelve-LED ring.

Each inbound LIGHT_COMMAND becomes per-LED envelopes: chance-gated
independently per LED, then ramped through attack/sustain/release.
The caller drives the actual hardware (tildagonos.leds[i] = (r,g,b))
via the set_led callback passed to tick(); this module stays pure
logic so the whole render contract is host-testable.

Frequency cap and brightness cap implement Calm Mode per architecture
spec section 15. Calm Mode (default on): 2 Hz dispatch cap, 50 % peak
brightness. Full-effect (operator opt-in): 4 Hz dispatch cap, 100 %.

Reference manuals:
- Protocol manual section 3.3.4 (LIGHT_COMMAND fields)
- Architecture spec section 15 (photosensitivity / Calm Mode)
- Epic 5 Block 3
"""

# Map the protocol's Time enum (0..7) to milliseconds.
# Mirrors include/pixmob_protocol.h Time in the M5 firmware.
TIME_MS = (0, 32, 96, 192, 480, 960, 2400, 3840)

# Map the protocol's Chance enum (0..7) to a probability 0..1.
# Mirrors include/pixmob_protocol.h Chance in the M5 firmware.
CHANCE_PROB = (1.00, 0.88, 0.67, 0.50, 0.32, 0.16, 0.10, 0.04)

# Tildagon hardware: 12 perimeter LEDs, indexed 1..12. The API uses
# 1-based indexing (tildagonos.leds[1]..tildagonos.leds[12]); we mirror
# that here so a single integer flows from this module straight into the
# tildagonos call without translation.
LED_MIN_INDEX = 1
LED_MAX_INDEX = 12
LED_COUNT = LED_MAX_INDEX - LED_MIN_INDEX + 1

# Frequency caps: minimum milliseconds between accepted dispatch calls.
CALM_MIN_INTERVAL_MS = 500   # 2 Hz
FULL_MIN_INTERVAL_MS = 250   # 4 Hz

# Peak brightness multiplier applied per Calm Mode.
CALM_BRIGHTNESS_CAP = 0.5
FULL_BRIGHTNESS_CAP = 1.0


class PerimeterRenderer:
    """Render LIGHT_COMMAND envelopes onto the twelve Tildagon perimeter LEDs.

    The renderer is two-phase:
      dispatch(frame, now_ms)    - on every received LIGHT_COMMAND.
      tick(now_ms, set_led_fn)   - on every UI frame (~20 Hz).

    Calm Mode is on by default; the caller can flip it via set_calm_mode().
    """

    __slots__ = (
        "_calm_mode",
        "_min_interval_ms",
        "_brightness_cap",
        "_last_dispatch_ms",
        "_envelopes",
        "_rng",
    )

    def __init__(self, calm_mode=True, rng=None):
        """``rng`` is a zero-argument callable returning a float in [0, 1).
        Defaults to ``random.random``. Injected so tests can pin outcomes.
        """
        if rng is None:
            import random  # imported lazily so MicroPython startup stays slim
            rng = random.random
        self._rng = rng

        # _envelopes[0] is unused so LED index 1..12 maps straight in.
        self._envelopes = [None] * (LED_MAX_INDEX + 1)

        # Configure caps before _last_dispatch_ms initialisation so the
        # first dispatch is always allowed (we set it to -interval).
        self._calm_mode = bool(calm_mode)
        self._min_interval_ms = CALM_MIN_INTERVAL_MS if self._calm_mode else FULL_MIN_INTERVAL_MS
        self._brightness_cap = CALM_BRIGHTNESS_CAP if self._calm_mode else FULL_BRIGHTNESS_CAP
        self._last_dispatch_ms = -self._min_interval_ms - 1

    @property
    def calm_mode(self):
        return self._calm_mode

    def set_calm_mode(self, on):
        self._calm_mode = bool(on)
        self._min_interval_ms = CALM_MIN_INTERVAL_MS if self._calm_mode else FULL_MIN_INTERVAL_MS
        self._brightness_cap = CALM_BRIGHTNESS_CAP if self._calm_mode else FULL_BRIGHTNESS_CAP

    def dispatch(self, frame, now_ms):
        """Apply one LIGHT_COMMAND to the perimeter ring.

        Returns the number of LEDs that were lit (0 if rate-limited,
        chance-gated to zero, the frame was a primer, or the envelope
        was zero-duration).
        """
        # Frequency cap. Primers and zero-duration envelopes don't count
        # against the cap so they don't consume the budget that the
        # subsequent main fire needs.
        if now_ms - self._last_dispatch_ms < self._min_interval_ms:
            return 0

        # Primer (rgb=0) is invisible on LEDs; brightness multiplied by
        # zero is zero. Don't bother arming envelopes.
        if frame.r == 0 and frame.g == 0 and frame.b == 0:
            return 0

        # Resolve envelope timings from the protocol's Time enum.
        attack_ms = TIME_MS[frame.attack]
        sustain_ms = TIME_MS[frame.sustain]
        release_ms = TIME_MS[frame.release]
        total_ms = attack_ms + sustain_ms + release_ms
        if total_ms == 0:
            return 0

        chance_prob = CHANCE_PROB[frame.chance]

        lit = 0
        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            if self._rng() < chance_prob:
                self._envelopes[i] = (
                    now_ms,
                    frame.r,
                    frame.g,
                    frame.b,
                    attack_ms,
                    sustain_ms,
                    release_ms,
                    total_ms,
                )
                lit += 1

        self._last_dispatch_ms = now_ms
        return lit

    def tick(self, now_ms, set_led):
        """Compute current LED states; call set_led(index, r, g, b) per LED.

        ``set_led`` is invoked for every LED on every tick, including
        idle ones (set to 0,0,0) - the caller can choose to suppress
        no-op writes or just trust their write path to handle them.
        """
        cap = self._brightness_cap
        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            env = self._envelopes[i]
            if env is None:
                set_led(i, 0, 0, 0)
                continue

            start_ms, r, g, b, atk, sus, rel, total = env
            elapsed = now_ms - start_ms

            if elapsed < 0:
                set_led(i, 0, 0, 0)
                continue
            if elapsed >= total:
                self._envelopes[i] = None
                set_led(i, 0, 0, 0)
                continue

            level = _envelope_brightness(elapsed, atk, sus, rel) * cap
            set_led(i, int(r * level), int(g * level), int(b * level))

    def clear(self):
        """Reset every LED envelope to idle. Useful on Calm Mode change
        or backgrounding so the ring darkens cleanly.
        """
        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            self._envelopes[i] = None


def _envelope_brightness(t, attack_ms, sustain_ms, release_ms):
    """Linear ASR envelope returning 0..1.

    0..attack_ms      -> ramp up from 0 to 1
    attack..attack+sustain -> hold at 1
    sustain..total    -> ramp down from 1 to 0

    attack_ms == 0 snaps to 1 immediately. release_ms == 0 snaps to 0
    after sustain ends. Callers must guarantee total > 0; ticked-out
    envelopes are cleared in tick() before this is called.
    """
    if t < attack_ms:
        if attack_ms == 0:
            return 1.0
        return t / attack_ms
    t -= attack_ms
    if t < sustain_ms:
        return 1.0
    t -= sustain_ms
    if release_ms == 0:
        return 0.0
    if t < release_ms:
        return 1.0 - (t / release_ms)
    return 0.0
