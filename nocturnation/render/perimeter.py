"""Perimeter LED renderer for the Tildagon's twelve-LED ring.

Each inbound LIGHT_PULSE becomes per-LED envelopes (chance-gated
independently per LED, then ramped through attack/sustain/release).
LIGHT_WASH adds a uniform baseline that all twelve LEDs follow with
cosine-eased ping-pong drift; pulses overlay additively on top.

The caller drives the actual hardware (tildagonos.leds[i] = (r,g,b))
via the set_led callback passed to tick(); this module stays pure
logic so the whole render contract is host-testable.

Frequency cap and brightness cap implement Calm Mode per architecture
spec section 15. Calm Mode (default on): 2 Hz dispatch cap, 50 % peak
brightness. Full-effect (operator opt-in): 4 Hz dispatch cap, 100 %.

Reference manuals:
- Protocol manual section 3.3.2/3/4/5 (LIGHT_PULSE + LIGHT_WASH family)
- Architecture spec section 15 (photosensitivity / Calm Mode)
- lume-capabilities-design.md (wash semantics)
- Epic 5 Block 3, Epic 6C Phase G
"""

import math

from ..clock import ticks_diff
from .envelope import TIME_MS, envelope_brightness  # noqa: F401 (re-exported)

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
# Calm mode keeps the 500 ms (2 Hz) Harding-safe floor for badges worn
# by non-consenting audience. Full mode was 250 ms (4 Hz) but that
# silently dropped every other sparkle at 140 BPM (sparkle_on_beat at
# 7 Hz = 143 ms gap); raised to 60 ms (~16 Hz) so per-beat sparkles
# land through 200+ BPM and sub-beat sparkles still fit. The operator
# opts into Full mode knowing it's bright; the cap exists only to
# protect against pathological back-to-back dispatches, not to throttle
# legitimate music tempo.
CALM_MIN_INTERVAL_MS = 500   # 2 Hz - Harding-safe for audience badges
FULL_MIN_INTERVAL_MS = 60    # ~16 Hz - covers per-beat sparkles to 200+ BPM

# Peak brightness multiplier applied per Calm Mode.
CALM_BRIGHTNESS_CAP = 0.5
FULL_BRIGHTNESS_CAP = 1.0

# Lost-WASH_END failsafe (not a protocol change). A LIGHT_WASH with
# ttl_seconds == 0 is "infinite" per the spec: it holds until an
# explicit LIGHT_WASH_END frame arrives. If that frame is lost, a
# wash with pulse_response = 0 also gates PULSE - so the Lume sits
# unresponsive forever. After WASH_MAX_HOLD_MS the receiver
# self-releases the wash so a missed WASH_END eventually recovers.
# Mirrors the cap in the LCD renderer.
WASH_MAX_HOLD_MS = 30 * 60 * 1000   # 30 minutes


# WashPhase string constants (no IntEnum on MicroPython).
_WASH_INACTIVE  = 0
_WASH_ATTACK    = 1
_WASH_HOLD      = 2
_WASH_RELEASE   = 3


def _clip(v):
    if v < 0: return 0
    if v > 255: return 255
    return int(v)


def _lerp(a, b, t):
    """t in [0, 1]. Returns int."""
    if t <= 0.0: return int(a)
    if t >= 1.0: return int(b)
    return int(a + (b - a) * t)


class PerimeterRenderer:
    """Render LIGHT_PULSE envelopes + LIGHT_WASH baseline onto the
    twelve Tildagon perimeter LEDs.

    Two-phase contract:
      dispatch(frame, now_ms)       - on every received LIGHT_PULSE.
      on_light_wash(frame, now_ms)  - on every received LIGHT_WASH.
      on_light_wash_end(frame, t)   - on every received LIGHT_WASH_END.
      on_light_wash_pulse(...)      - same dispatch as pulse, but only
                                       fires if a wash is active.
      tick(now_ms, set_led_fn)      - on every UI frame (~20 Hz).

    Calm Mode is on by default; the caller can flip it via
    set_calm_mode(). Caps only apply to the pulse rate-limit and the
    overall brightness cap. WASH is not rate-limited (it's a Director-
    side state transition, not a periodic flash).

    Pulse semantics (Epic 6C Phase G ADR fix): the attack phase now
    lerps from the LED's *current rendered colour* (wash baseline +
    any prior pulse output) to the new pulse's target colour. Pre-fix
    behaviour ramped from brightness 0, so a series of rainbow pulses
    snapped through black between colours; this fix produces smooth
    crossfades when attack > 0 (T_0_MS attacks still snap, by design).
    """

    __slots__ = (
        "_calm_mode",
        "_min_interval_ms",
        "_brightness_cap",
        "_last_dispatch_ms",
        "_envelopes",          # per-LED active pulse envelopes (1..LED_MAX_INDEX)
        "_last_rendered",   # per-LED last output (r,g,b) for next pulse's src-lerp
        "_wash",            # active wash dict, or None
        "_rng",
    )

    def __init__(self, calm_mode=True, rng=None):
        if rng is None:
            import random
            rng = random.random
        self._rng = rng

        # _envelopes[0] / _last_rendered[0] are unused so LED index 1..12 maps in.
        self._envelopes        = [None] * (LED_MAX_INDEX + 1)
        self._last_rendered = [(0, 0, 0)] * (LED_MAX_INDEX + 1)
        self._wash          = None

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

    # ------------------------------------------------------------------
    # Wash baseline (Epic 6C Phase G) - one wash struct for all 12 LEDs.
    # ------------------------------------------------------------------

    def _wash_baseline_at(self, now_ms):
        """Return (r, g, b) wash baseline at now_ms, post-intensity, post-cap.
        Returns (0, 0, 0) when no wash is active.
        """
        w = self._wash
        if w is None:
            return (0, 0, 0)
        phase = w["phase"]
        if phase == _WASH_INACTIVE:
            return (0, 0, 0)

        # Drift between r1/g1/b1 and r2/g2/b2 (cosine ease) - the "hold"
        # baseline post-attack.
        if w["cycle_ms"] != 0:
            ph = 2.0 * math.pi * (now_ms - w["started_ms"]) / w["cycle_ms"]
            t = 0.5 - 0.5 * math.cos(ph)
            base_r = _lerp(w["r1"], w["r2"], t)
            base_g = _lerp(w["g1"], w["g2"], t)
            base_b = _lerp(w["b1"], w["b2"], t)
        else:
            base_r, base_g, base_b = w["r1"], w["g1"], w["b1"]

        # Apply intensity (0..255 -> 0..1 scalar) + Calm-Mode brightness cap.
        scale = (w["intensity"] / 255.0) * self._brightness_cap

        post_r = _clip(base_r * scale)
        post_g = _clip(base_g * scale)
        post_b = _clip(base_b * scale)

        if phase == _WASH_HOLD:
            return (post_r, post_g, post_b)

        # Attack: lerp from pre_wash_* to (post_r, post_g, post_b) over
        # attack_units * 100 ms. Use the pre-cap pre_wash so the cap and
        # intensity already in post_* don't double-apply.
        elapsed = now_ms - w["phase_started_ms"]
        if phase == _WASH_ATTACK:
            atk_ms = w["attack_units"] * 100
            if atk_ms == 0 or elapsed >= atk_ms:
                # Done attacking - transition to Hold at end of tick math.
                # (Caller handles the transition.)
                return (post_r, post_g, post_b)
            t = elapsed / atk_ms
            return (
                _lerp(w["pre_wash_r"], post_r, t),
                _lerp(w["pre_wash_g"], post_g, t),
                _lerp(w["pre_wash_b"], post_b, t),
            )

        # Release: lerp from pre_wash_* (the wash colour we left from)
        # to (release_end_r, _g, _b) over release_units_active * 100 ms.
        rel_ms = w["release_units_active"] * 100
        if phase == _WASH_RELEASE:
            if rel_ms == 0 or elapsed >= rel_ms:
                return (w["release_end_r"], w["release_end_g"], w["release_end_b"])
            t = elapsed / rel_ms
            return (
                _lerp(w["pre_wash_r"], w["release_end_r"], t),
                _lerp(w["pre_wash_g"], w["release_end_g"], t),
                _lerp(w["pre_wash_b"], w["release_end_b"], t),
            )

        return (0, 0, 0)

    def on_light_wash(self, frame, now_ms):
        """Enter / supersede the wash state from a LIGHT_WASH frame."""
        # If a wash is already in flight, capture its instantaneous
        # baseline as the attack-lerp source so the transition is
        # visually continuous.
        if self._wash is not None and self._wash["phase"] != _WASH_INACTIVE:
            pre = self._wash_baseline_at(now_ms)
        else:
            pre = (0, 0, 0)

        self._wash = {
            "phase":               _WASH_ATTACK,
            "started_ms":          now_ms,
            "phase_started_ms":    now_ms,
            "r1": frame.r1, "g1": frame.g1, "b1": frame.b1,
            "r2": frame.r2, "g2": frame.g2, "b2": frame.b2,
            "attack_units":        frame.wash_attack,
            "release_units":       frame.wash_release,
            "intensity":           frame.intensity,
            "cycle_ms":            frame.cycle_ms,
            "ttl_seconds":         frame.ttl_seconds,
            "pulse_response":      frame.pulse_response,
            "pre_wash_r":          pre[0],
            "pre_wash_g":          pre[1],
            "pre_wash_b":          pre[2],
            # Release defaults to "fade to black"; LIGHT_WASH_END may
            # override (or TTL expiry which reuses the wash's own release).
            "release_units_active": frame.wash_release,
            "release_end_r":       0,
            "release_end_g":       0,
            "release_end_b":       0,
        }

    def on_light_wash_end(self, frame, now_ms):
        """Cancel the active wash with the operator-supplied release_time."""
        if self._wash is None or self._wash["phase"] == _WASH_INACTIVE:
            return
        # Capture the instantaneous wash colour as the fade-source.
        cur = self._wash_baseline_at(now_ms)
        self._wash["pre_wash_r"]           = cur[0]
        self._wash["pre_wash_g"]           = cur[1]
        self._wash["pre_wash_b"]           = cur[2]
        self._wash["release_units_active"] = frame.release_time
        self._wash["release_end_r"]        = 0
        self._wash["release_end_g"]        = 0
        self._wash["release_end_b"]        = 0
        self._wash["phase"]                = _WASH_RELEASE
        self._wash["phase_started_ms"]     = now_ms

    def is_washing(self):
        """True when the renderer is in Attack or Hold phase. Used by
        the wash_pulse dispatch (drop on non-washing Lume per design)."""
        if self._wash is None:
            return False
        return self._wash["phase"] in (_WASH_ATTACK, _WASH_HOLD)

    def wash_pulse_response(self):
        """0 = ignore PULSE while washing; 1 = accept as overlay.
        Caller of dispatch_light_pulse uses this to decide whether to
        drop the pulse when wash is active. Returns 1 (accept) when no
        wash is active - pre-Phase-G semantics."""
        if self._wash is None or self._wash["phase"] != _WASH_HOLD and self._wash["phase"] != _WASH_ATTACK:
            return 1
        return self._wash["pulse_response"]

    # ------------------------------------------------------------------
    # Pulse dispatch (LIGHT_PULSE + LIGHT_WASH_PULSE)
    # ------------------------------------------------------------------

    def dispatch(self, frame, now_ms):
        """Apply one LIGHT_PULSE to the perimeter ring.

        Returns the number of LEDs that were lit (0 if rate-limited,
        chance-gated to zero, the frame was a primer, or the envelope
        was zero-duration).

        Epic 6C Phase G: when a wash is active and `pulse_response = 0`,
        the pulse is silently dropped (per lume-capabilities-design.md
        §5). With pulse_response = 1 (the default for wash demos) the
        pulse overlays additively on the wash baseline.
        """
        # Frequency cap. Primers and zero-duration envelopes don't count
        # against the cap so they don't consume the budget that the
        # subsequent main fire needs.
        if ticks_diff(now_ms, self._last_dispatch_ms) < self._min_interval_ms:
            return 0

        if frame.r == 0 and frame.g == 0 and frame.b == 0:
            return 0

        # Wash + pulse_response gate: a non-overlay wash drops PULSE.
        if self._wash is not None and self._wash["phase"] in (_WASH_ATTACK, _WASH_HOLD):
            if self._wash["pulse_response"] == 0:
                return 0

        attack_ms = TIME_MS[frame.attack]
        sustain_ms = TIME_MS[frame.sustain]
        release_ms = TIME_MS[frame.release]
        total_ms = attack_ms + sustain_ms + release_ms
        if total_ms == 0:
            return 0

        chance_prob = CHANCE_PROB[frame.chance]
        cap = self._brightness_cap

        lit = 0
        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            if self._rng() < chance_prob:
                # Source colour for the attack lerp: the LED's current
                # rendered colour (last_rendered). This is the ADR fix -
                # previously the source was implicitly 0 (black), so a
                # series of fast-arriving colour-different pulses snapped
                # through black between colours rather than crossfading.
                src_r, src_g, src_b = self._last_rendered[i]
                self._envelopes[i] = (
                    now_ms,
                    src_r, src_g, src_b,
                    _clip(frame.r * cap),
                    _clip(frame.g * cap),
                    _clip(frame.b * cap),
                    attack_ms, sustain_ms, release_ms, total_ms,
                )
                lit += 1

        self._last_dispatch_ms = now_ms
        return lit

    def on_light_wash_pulse(self, frame, now_ms):
        """LIGHT_WASH_PULSE - same shape as PULSE, but only fires if
        wash is active. The design treats this as the LD's "explicit
        overlay" mechanism that bypasses pulse_response.
        """
        if not self.is_washing():
            return 0
        # Force-accept the overlay regardless of pulse_response by
        # temporarily setting pulse_response=1, then restoring. Easier
        # than duplicating the dispatch body.
        original = self._wash["pulse_response"]
        self._wash["pulse_response"] = 1
        try:
            return self.dispatch(frame, now_ms)
        finally:
            self._wash["pulse_response"] = original

    # ------------------------------------------------------------------
    # Per-tick render: wash baseline + per-LED pulse overlay.
    # ------------------------------------------------------------------

    def tick(self, now_ms, set_led):
        """Compute current LED states; call set_led(index, r, g, b) per LED.

        `set_led` is invoked for every LED on every tick. The output is
        the wash baseline (uniform across the ring, post-intensity +
        post-cap) plus any active per-LED pulse overlay.
        """
        # Wash phase machine: advance Attack -> Hold, handle TTL expiry,
        # finish Release.
        if self._wash is not None and self._wash["phase"] != _WASH_INACTIVE:
            self._advance_wash_phase(now_ms)

        # Compute the wash baseline once (uniform across the ring).
        base = self._wash_baseline_at(now_ms)

        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            r, g, b = base
            env = self._envelopes[i]
            if env is not None:
                start_ms, src_r, src_g, src_b, dst_r, dst_g, dst_b, atk, sus, rel, total = env
                elapsed = now_ms - start_ms
                if elapsed < 0:
                    pass  # not started yet, baseline only
                elif elapsed >= total:
                    self._envelopes[i] = None
                else:
                    # Compute the pulse output (attack-lerps from src to
                    # dst, sustain holds dst, release lerps dst back to
                    # baseline) and OVERWRITE the baseline. Pulse fully
                    # replaces baseline during its lifetime; on a
                    # wash-active LED, release fades back to baseline
                    # rather than to black.
                    pr, pg, pb = self._pulse_color_at(env, elapsed, base)
                    r, g, b = pr, pg, pb

            self._last_rendered[i] = (r, g, b)
            set_led(i, r, g, b)

    def _pulse_color_at(self, env, elapsed, baseline):
        """Compute the (r,g,b) output for a pulse at `elapsed` ms in.
        Falls back to `baseline` during release so a wash-active LED
        fades back to the wash baseline rather than to black."""
        _, src_r, src_g, src_b, dst_r, dst_g, dst_b, atk, sus, rel, _total = env
        if elapsed < atk:
            if atk == 0:
                return (dst_r, dst_g, dst_b)
            t = elapsed / atk
            return (
                _lerp(src_r, dst_r, t),
                _lerp(src_g, dst_g, t),
                _lerp(src_b, dst_b, t),
            )
        elapsed -= atk
        if elapsed < sus:
            return (dst_r, dst_g, dst_b)
        elapsed -= sus
        # Release: fade dst -> baseline.
        if rel == 0:
            return baseline
        t = elapsed / rel
        return (
            _lerp(dst_r, baseline[0], t),
            _lerp(dst_g, baseline[1], t),
            _lerp(dst_b, baseline[2], t),
        )

    def _advance_wash_phase(self, now_ms):
        w = self._wash
        phase = w["phase"]
        if phase == _WASH_ATTACK:
            atk_ms = w["attack_units"] * 100
            if atk_ms == 0 or (now_ms - w["phase_started_ms"]) >= atk_ms:
                w["phase"] = _WASH_HOLD
                w["phase_started_ms"] = now_ms
                # No need to retain pre_wash_* once attack is over.
        elif phase == _WASH_HOLD:
            # Effective hold cap: the operator's explicit ttl_seconds
            # when set; the lost-WASH_END failsafe (WASH_MAX_HOLD_MS)
            # when ttl_seconds == 0 ("infinite" per spec).
            effective_ttl_ms = (
                w["ttl_seconds"] * 1000
                if w["ttl_seconds"] != 0
                else WASH_MAX_HOLD_MS
            )
            if ticks_diff(now_ms, w["started_ms"]) >= effective_ttl_ms:
                # TTL expiry: kick off a release using the wash's
                # own release as the fade duration.
                cur = self._wash_baseline_at(now_ms)
                w["pre_wash_r"]           = cur[0]
                w["pre_wash_g"]           = cur[1]
                w["pre_wash_b"]           = cur[2]
                w["release_units_active"] = w["release_units"]
                w["release_end_r"]        = 0
                w["release_end_g"]        = 0
                w["release_end_b"]        = 0
                w["phase"]                = _WASH_RELEASE
                w["phase_started_ms"]     = now_ms
        elif phase == _WASH_RELEASE:
            rel_ms = w["release_units_active"] * 100
            if rel_ms == 0 or (now_ms - w["phase_started_ms"]) >= rel_ms:
                self._wash = None    # exit wash mode

    def clear(self):
        """Reset every LED envelope to idle + drop any active wash.
        Useful on Calm Mode change or backgrounding."""
        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            self._envelopes[i] = None
            self._last_rendered[i] = (0, 0, 0)
        self._wash = None
