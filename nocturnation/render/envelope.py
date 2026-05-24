"""Shared envelope math for the perimeter and LCD renderers.

The protocol's Time enum (0..7) indexes into TIME_MS to recover the
millisecond duration of each envelope stage. envelope_brightness then
computes a 0..1 brightness multiplier for a given elapsed time through
the attack / sustain / release envelope.

Kept in its own module so the perimeter and LCD renderers can share
the same canonical timings without depending on each other.
"""

# Map the protocol's Time enum (0..7) to milliseconds.
# Mirrors include/pixmob_protocol.h Time in the M5 firmware.
TIME_MS = (0, 32, 96, 192, 480, 960, 2400, 3840)


def envelope_brightness(t, attack_ms, sustain_ms, release_ms):
    """Linear ASR envelope returning a 0..1 brightness multiplier.

    Stages:
      0..attack_ms                      ramp up from 0 to 1
      attack..attack+sustain            hold at 1
      sustain..total                    ramp down from 1 to 0

    attack_ms == 0 snaps to 1 immediately. release_ms == 0 snaps to 0
    after sustain ends. Callers must guarantee total > 0; out-of-range
    callers should be screened upstream (perimeter clears the envelope
    in tick(); LCD does the same in current_colour()).
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
