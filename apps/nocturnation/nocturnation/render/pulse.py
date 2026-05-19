"""RgbPulse - the render event a Show hands to ctx.render_fx().

Cross-platform mirror of `dal::RgbPulseEvent` on the M5 firmware (see
docs/developing-shows.md in nocturnation-m5). Carries the colour plus
the PixMob ASR envelope and per-bracelet chance. The Director's
RenderDispatcher maps these fields straight onto the LIGHT_COMMAND
payload bytes.

The envelope timings are PixMob Time enum indices (not milliseconds):
the protocol's 3-bit envelope is non-linear, so pick from
`protocol.constants.Time` rather than passing arbitrary ms. Likewise
`chance` is a `protocol.constants.Chance` index.

A Show typically constructs one like:

    from nocturnation.render import RgbPulse
    from nocturnation.protocol.constants import Time, Chance

    ev = RgbPulse(255, 80, 0,
                  attack=Time.T_0_MS, sustain=Time.T_96_MS,
                  release=Time.T_480_MS, chance=Chance.CHANCE_100)
    ctx.render_fx("01:01", ev)
"""

from ..protocol.constants import Time, Chance


class RgbPulse:
    """Colour + envelope + chance for one render_fx fire."""

    __slots__ = ("r", "g", "b", "attack", "sustain", "release", "chance")

    def __init__(
        self,
        r,
        g,
        b,
        attack=Time.T_0_MS,
        sustain=Time.T_96_MS,
        release=Time.T_480_MS,
        chance=Chance.CHANCE_100,
    ):
        self.r = r & 0xFF
        self.g = g & 0xFF
        self.b = b & 0xFF
        self.attack = attack
        self.sustain = sustain
        self.release = release
        self.chance = chance

    def __repr__(self):
        return (
            "RgbPulse(r=%d, g=%d, b=%d, attack=%d, sustain=%d, "
            "release=%d, chance=%d)"
            % (
                self.r,
                self.g,
                self.b,
                self.attack,
                self.sustain,
                self.release,
                self.chance,
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, RgbPulse)
            and self.r == other.r
            and self.g == other.g
            and self.b == other.b
            and self.attack == other.attack
            and self.sustain == other.sustain
            and self.release == other.release
            and self.chance == other.chance
        )
