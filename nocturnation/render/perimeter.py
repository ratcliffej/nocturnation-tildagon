"""Perimeter LED renderer for the Tildagon's twelve-LED ring.

Each LIGHT_PULSE becomes per-LED envelopes (chance-gated independently
per LED, then ramped through attack/sustain/release). LIGHT_WASH adds a
uniform baseline all twelve LEDs follow with cosine-eased ping-pong
drift; pulses overlay additively on top.

Pure logic - the caller drives the actual hardware via the set_led
callback passed to tick().

Calm Mode (default on): 2 Hz dispatch cap, 50 % peak brightness.
Full-effect (operator opt-in): 16 Hz dispatch cap, 100 %.

Design decisions: docs/tildagon-history.md.
"""

import math

from ..clock import ticks_diff
from .envelope import TIME_MS, envelope_brightness  # noqa: F401 (re-exported)

# Protocol Chance enum (0..7) -> probability. Mirrors
# include/pixmob_protocol.h in the M5 firmware.
CHANCE_PROB = (1.00, 0.88, 0.67, 0.50, 0.32, 0.16, 0.10, 0.04)

# Tildagon perimeter API is 1-based (tildagonos.leds[1]..leds[12]).
# We mirror the indexing so a single integer flows straight through.
LED_MIN_INDEX = 1
LED_MAX_INDEX = 12
LED_COUNT = LED_MAX_INDEX - LED_MIN_INDEX + 1

# Epic 18 v0x04 LED-level addressing. Tildagon perimeter is a single
# chain of 12 pixels; mode-1 addressing accepts chain=0 (all chains,
# same index) or chain=1 (specific chain 1) and drops chain>=2 as
# there's no physical chain to write. Mode-2 uses a 12-bit tile mask
# packed LSB-first across the two modifier bytes; upper 4 bits of
# modifier2 are reserved. The mask width matches the ring width
# exactly so no tiling ambiguity here (unlike variable-length strips).
_LED_MODE_ALL             = 0
_LED_MODE_SINGLE_LED      = 1
_LED_MODE_REPEAT_PATTERN  = 2

# Escape hatch (Epic 18 B4): if the bench measurement ever shows the
# per-LED envelope tick can't hold its budget under sustained mode-1
# traffic, flip this to True to coalesce mode 1+ into mode-0 whole-ring
# writes on the Tildagon side only. Ships false today; the existing
# renderer already runs per-LED envelopes on every tick so honouring
# LedMode is basically free. Documented for future emergency use.
MODE_0_ONLY = False

# Frequency caps - minimum ms between accepted dispatch calls. Calm mode
# keeps the Harding-safe 500 ms (2 Hz) floor for audience badges. Full
# mode 60 ms (~16 Hz) covers per-beat sparkles to 200+ BPM; guards
# against pathological back-to-back dispatches, not legitimate music
# tempo. See docs/tildagon-history.md.
CALM_MIN_INTERVAL_MS = 500
FULL_MIN_INTERVAL_MS = 60

# Bench-time dispatch drop logging. Flipped on by app.py._BENCH_HOP0.
_BENCH_DISPATCH_LOG = False

CALM_BRIGHTNESS_CAP = 0.5
FULL_BRIGHTNESS_CAP = 1.0

# Lost-WASH_END failsafe: a wash with ttl_seconds == 0 is "infinite" per
# spec. If the WASH_END frame is lost and pulse_response = 0 gates
# PULSE, the Lume sits unresponsive forever. Self-release after this
# holds even if the END never arrives. Mirrors the LCD renderer.
WASH_MAX_HOLD_MS = 30 * 60 * 1000   # 30 minutes


# WashPhase constants (no IntEnum on MicroPython).
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

    Pulse attack lerps from the LED's current rendered colour (wash
    baseline + any prior pulse output) to the new pulse's target -
    produces smooth crossfades when attack > 0. T_0_MS attacks still
    snap, by design.
    """

    __slots__ = (
        "_calm_mode",
        "_min_interval_ms",
        "_brightness_cap",
        "_last_dispatch_ms",
        "_envelopes",
        "_last_rendered",
        "_wash",
        "_rng",
    )

    def __init__(self, calm_mode=False, rng=None):
        if rng is None:
            import random
            rng = random.random
        self._rng = rng

        # Index 0 unused so LED index 1..12 maps in directly.
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
    # Wash baseline - one wash struct for all 12 LEDs.
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

        # Cosine-ease drift between r1/g1/b1 and r2/g2/b2.
        if w["cycle_ms"] != 0:
            ph = 2.0 * math.pi * (now_ms - w["started_ms"]) / w["cycle_ms"]
            t = 0.5 - 0.5 * math.cos(ph)
            base_r = _lerp(w["r1"], w["r2"], t)
            base_g = _lerp(w["g1"], w["g2"], t)
            base_b = _lerp(w["b1"], w["b2"], t)
        else:
            base_r, base_g, base_b = w["r1"], w["g1"], w["b1"]

        scale = (w["intensity"] / 255.0) * self._brightness_cap

        post_r = _clip(base_r * scale)
        post_g = _clip(base_g * scale)
        post_b = _clip(base_b * scale)

        if phase == _WASH_HOLD:
            return (post_r, post_g, post_b)

        # Attack lerps pre_wash_* -> (post_r, post_g, post_b) over
        # attack_units * 100 ms. Use pre-cap pre_wash so the cap +
        # intensity already applied in post_* don't double-apply.
        elapsed = now_ms - w["phase_started_ms"]
        if phase == _WASH_ATTACK:
            atk_ms = w["attack_units"] * 100
            if atk_ms == 0 or elapsed >= atk_ms:
                return (post_r, post_g, post_b)
            t = elapsed / atk_ms
            return (
                _lerp(w["pre_wash_r"], post_r, t),
                _lerp(w["pre_wash_g"], post_g, t),
                _lerp(w["pre_wash_b"], post_b, t),
            )

        # Release: lerp pre_wash_* -> (release_end_r, _g, _b) over
        # release_units_active * 100 ms.
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
        # Capture the in-flight wash's instantaneous baseline as the
        # attack-lerp source so a transition is visually continuous.
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
            # Release defaults to fade-to-black; LIGHT_WASH_END may
            # override (or TTL expiry which reuses the wash's own release).
            "release_units_active": frame.wash_release,
            "release_end_r":       0,
            "release_end_g":       0,
            "release_end_b":       0,
        }

    def on_light_wash_end(self, frame, now_ms):
        if self._wash is None or self._wash["phase"] == _WASH_INACTIVE:
            return
        # Capture instantaneous wash colour as the fade-source.
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
        # True in Attack or Hold; used by wash_pulse dispatch (drops on
        # non-washing Lume per design).
        if self._wash is None:
            return False
        return self._wash["phase"] in (_WASH_ATTACK, _WASH_HOLD)

    def wash_pulse_response(self):
        """0 = ignore PULSE while washing; 1 = accept as overlay.
        Returns 1 when no wash is active (pre-wash semantics)."""
        if self._wash is None or self._wash["phase"] != _WASH_HOLD and self._wash["phase"] != _WASH_ATTACK:
            return 1
        return self._wash["pulse_response"]

    # ------------------------------------------------------------------
    # Pulse dispatch (LIGHT_PULSE + LIGHT_WASH_PULSE)
    # ------------------------------------------------------------------

    def dispatch(self, frame, now_ms):
        """Apply one LIGHT_PULSE to the perimeter ring.

        Returns the number of LEDs lit. When a wash is active and
        pulse_response = 0, the pulse is silently dropped; with
        pulse_response = 1 (the wash-demo default) it overlays additively.
        """
        # Primers and zero-duration envelopes don't count against the
        # cap so they don't consume the budget the main fire needs.
        gap = ticks_diff(now_ms, self._last_dispatch_ms)
        if gap < self._min_interval_ms:
            if _BENCH_DISPATCH_LOG:
                print("[BENCH-DROP] src=%d seq=%d ticks=%d reason=rate_limit gap=%d min=%d"
                      % (frame.source_id, frame.sequence_number, now_ms,
                         gap, self._min_interval_ms))
            return 0

        if frame.r == 0 and frame.g == 0 and frame.b == 0:
            if _BENCH_DISPATCH_LOG:
                print("[BENCH-DROP] src=%d seq=%d ticks=%d reason=black"
                      % (frame.source_id, frame.sequence_number, now_ms))
            return 0

        # A non-overlay wash drops PULSE.
        if self._wash is not None and self._wash["phase"] in (_WASH_ATTACK, _WASH_HOLD):
            if self._wash["pulse_response"] == 0:
                if _BENCH_DISPATCH_LOG:
                    print("[BENCH-DROP] src=%d seq=%d ticks=%d reason=wash_gated"
                          % (frame.source_id, frame.sequence_number, now_ms))
                return 0

        attack_ms = TIME_MS[frame.attack]
        sustain_ms = TIME_MS[frame.sustain]
        release_ms = TIME_MS[frame.release]
        total_ms = attack_ms + sustain_ms + release_ms
        if total_ms == 0:
            return 0

        chance_prob = CHANCE_PROB[frame.chance]
        cap = self._brightness_cap
        dst_r = _clip(frame.r * cap)
        dst_g = _clip(frame.g * cap)
        dst_b = _clip(frame.b * cap)

        # Epic 18 v0x04 LED-level addressing. Fields default to 0/0/0
        # (LedMode.ALL) so a Director that hasn't yet been taught to fill
        # them produces the v3-era whole-ring CHANCE roll below. Fields
        # are attribute-safe on Frames from older code paths via the
        # default None -> treat as ALL.
        led_mode      = getattr(frame, "led_mode",      None) or _LED_MODE_ALL
        led_modifier1 = getattr(frame, "led_modifier1", None) or 0
        led_modifier2 = getattr(frame, "led_modifier2", None) or 0

        if MODE_0_ONLY and led_mode != _LED_MODE_ALL:
            # Escape hatch coalesces mode-1/2 into whole-ring behaviour.
            led_mode      = _LED_MODE_ALL
            led_modifier1 = 0
            led_modifier2 = 0

        lit = 0
        if led_mode == _LED_MODE_SINGLE_LED:
            # Chain 0 (all chains, this index) and chain 1 (specific
            # chain 1) both hit our one physical ring. Chain >= 2 has
            # no matching hardware and drops silently.
            if led_modifier1 > 1:
                self._last_dispatch_ms = now_ms
                return 0
            # Wire LED-index is 0-based; Tildagon perimeter is 1-based.
            wire_idx = led_modifier2
            ring_idx = wire_idx + LED_MIN_INDEX
            if LED_MIN_INDEX <= ring_idx <= LED_MAX_INDEX:
                src_r, src_g, src_b = self._last_rendered[ring_idx]
                self._envelopes[ring_idx] = (
                    now_ms,
                    src_r, src_g, src_b,
                    dst_r, dst_g, dst_b,
                    attack_ms, sustain_ms, release_ms, total_ms,
                )
                lit = 1
        elif led_mode == _LED_MODE_REPEAT_PATTERN:
            # 12-bit tile mask, LSB=position 0 (wire index 0 = ring
            # position 0). Ring width matches tile width, so bit i lights
            # ring LED (i + LED_MIN_INDEX).
            mask = (led_modifier1 & 0xFF) | ((led_modifier2 & 0x0F) << 8)
            if mask == 0:
                self._last_dispatch_ms = now_ms
                return 0
            for wire_idx in range(LED_COUNT):
                if not (mask & (1 << wire_idx)):
                    continue
                ring_idx = wire_idx + LED_MIN_INDEX
                src_r, src_g, src_b = self._last_rendered[ring_idx]
                self._envelopes[ring_idx] = (
                    now_ms,
                    src_r, src_g, src_b,
                    dst_r, dst_g, dst_b,
                    attack_ms, sustain_ms, release_ms, total_ms,
                )
                lit += 1
        else:
            # LedMode.ALL (v3-parity path): per-LED CHANCE roll.
            for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
                if self._rng() < chance_prob:
                    src_r, src_g, src_b = self._last_rendered[i]
                    self._envelopes[i] = (
                        now_ms,
                        src_r, src_g, src_b,
                        dst_r, dst_g, dst_b,
                        attack_ms, sustain_ms, release_ms, total_ms,
                    )
                    lit += 1

        self._last_dispatch_ms = now_ms
        return lit

    def on_light_wash_pulse(self, frame, now_ms):
        # Same shape as PULSE but bypasses pulse_response (the "explicit
        # overlay" mechanism). Only fires if wash is active.
        if not self.is_washing():
            return 0
        # Force-accept the overlay by temporarily setting
        # pulse_response=1, then restoring - easier than duplicating
        # dispatch.
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

        set_led is invoked for every LED on every tick. Output is the
        wash baseline (uniform, post-intensity, post-cap) plus any
        active per-LED pulse overlay.
        """
        if self._wash is not None and self._wash["phase"] != _WASH_INACTIVE:
            self._advance_wash_phase(now_ms)

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
                    # Pulse fully replaces baseline during its lifetime;
                    # on a wash-active LED, release fades back to
                    # baseline rather than to black.
                    pr, pg, pb = self._pulse_color_at(env, elapsed, base)
                    r, g, b = pr, pg, pb

            self._last_rendered[i] = (r, g, b)
            set_led(i, r, g, b)

    def _pulse_color_at(self, env, elapsed, baseline):
        # Release falls back to baseline so a wash-active LED fades
        # back to the wash rather than to black.
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
        elif phase == _WASH_HOLD:
            # Effective hold cap: explicit ttl_seconds when set, else
            # WASH_MAX_HOLD_MS failsafe.
            effective_ttl_ms = (
                w["ttl_seconds"] * 1000
                if w["ttl_seconds"] != 0
                else WASH_MAX_HOLD_MS
            )
            if ticks_diff(now_ms, w["started_ms"]) >= effective_ttl_ms:
                # TTL expiry: release using the wash's own release as
                # the fade duration.
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
                self._wash = None

    def clear(self):
        """Reset every LED envelope + drop any active wash. Useful on
        Calm Mode change or backgrounding."""
        for i in range(LED_MIN_INDEX, LED_MAX_INDEX + 1):
            self._envelopes[i] = None
            self._last_rendered[i] = (0, 0, 0)
        self._wash = None
