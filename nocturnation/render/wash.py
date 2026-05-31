"""RgbWash - the render event a Show hands to ctx.render_wash().

Cross-platform mirror of `dal::LightWashEvent` on the M5 firmware (see
include/dal/dal.h in nocturnation-stickc). Carries the two-anchor
palette (A and B colours), the wash envelope timings, the brightness
scalar, the A<->B<->A cycle period, the time-to-live, and the
pulse_response overlay flag.

The Director's RenderDispatcher maps these fields straight onto the
LIGHT_WASH payload bytes (16 bytes; see protocol manual section 3.3.3).

A Show typically constructs one like:

    from nocturnation.render import RgbWash

    ev = RgbWash(
        r1=255, g1=140, b1=30,    # warm orange (anchor A)
        r2=255, g2=60,  b2=80,    # warm pink   (anchor B)
        attack=20,                 # 2.0 s ramp-in (100 ms units)
        release=10,                # 1.0 s default fade-out
        intensity=200,             # 0..255 brightness scalar
        cycle_ms=5000,             # 5 s A<->B<->A drift
        ttl_seconds=0,             # 0 = infinite (Show owns cleanup)
        pulse_response=1,          # 1 = accept PULSE as overlay
    )
    ctx.render_wash("01:00", ev)
"""


# Constructor defaults are chosen to match the M5 firmware's
# `wash_demo_show` "warm orange <-> warm pink" baseline so a Show
# author can copy the example across hosts and get the same look.

class RgbWash:
    """Two-anchor wash colour + envelope + cycle metadata."""

    __slots__ = (
        "r1", "g1", "b1",
        "r2", "g2", "b2",
        "attack",          # 100 ms units (0..255)
        "release",         # 100 ms units (0..255)
        "intensity",       # 0..255 brightness scalar
        "cycle_ms",        # u16: 0 = hold anchor A
        "ttl_seconds",     # u16: 0 = infinite
        "pulse_response",  # 0 = drop PULSE while washing; 1 = additive overlay
    )

    def __init__(
        self,
        r1, g1, b1,
        r2=None, g2=None, b2=None,
        attack=20,
        release=10,
        intensity=200,
        cycle_ms=5000,
        ttl_seconds=0,
        pulse_response=1,
    ):
        # Single-colour washes default the B anchor to A so the Lume
        # holds one solid colour (cycle_ms can also be set to 0 for
        # the same effect; both paths converge at the renderer).
        self.r1 = r1 & 0xFF
        self.g1 = g1 & 0xFF
        self.b1 = b1 & 0xFF
        self.r2 = (r1 if r2 is None else r2) & 0xFF
        self.g2 = (g1 if g2 is None else g2) & 0xFF
        self.b2 = (b1 if b2 is None else b2) & 0xFF
        self.attack         = attack & 0xFF
        self.release        = release & 0xFF
        self.intensity      = intensity & 0xFF
        self.cycle_ms       = cycle_ms & 0xFFFF
        self.ttl_seconds    = ttl_seconds & 0xFFFF
        self.pulse_response = 1 if pulse_response else 0

    def __repr__(self):
        return (
            "RgbWash(r1=%d, g1=%d, b1=%d, r2=%d, g2=%d, b2=%d, "
            "attack=%d, release=%d, intensity=%d, cycle_ms=%d, "
            "ttl_seconds=%d, pulse_response=%d)"
            % (
                self.r1, self.g1, self.b1,
                self.r2, self.g2, self.b2,
                self.attack, self.release, self.intensity,
                self.cycle_ms, self.ttl_seconds, self.pulse_response,
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, RgbWash)
            and self.r1 == other.r1 and self.g1 == other.g1 and self.b1 == other.b1
            and self.r2 == other.r2 and self.g2 == other.g2 and self.b2 == other.b2
            and self.attack == other.attack
            and self.release == other.release
            and self.intensity == other.intensity
            and self.cycle_ms == other.cycle_ms
            and self.ttl_seconds == other.ttl_seconds
            and self.pulse_response == other.pulse_response
        )
