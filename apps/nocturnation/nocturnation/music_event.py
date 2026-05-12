"""Music-event-driven synthetic fires.

MUSIC_EVENT frames (protocol message_type 0x06) carry a one-byte event
type per protocol manual section 3.3.7:
  0  Unknown
  1  Drop       - master detected a beat drop
  2  Breakdown  - master detected a breakdown
  3  Build      - reserved; not currently emitted

The Tildagon turns these into synthetic LIGHT_COMMAND-shaped objects
that the perimeter and LCD renderers can dispatch on, so the operator
gets visible feedback to song structure beyond raw beat detection.
The synthesised fires are local-only - they don't go on the wire.

Per Epic 5 Block 6 scope:
  Drop      -> bright whiteout (high-energy attack, medium decay)
  Breakdown -> dim cool blue (slow attack, long sustain, long decay)
  Build / Unknown / other -> ignored
"""

from .protocol import Frame, MessageType
from .protocol.constants import Time, Chance, MusicEventType


def _make_synthetic(r, g, b, attack, sustain, release, chance):
    """Build a Frame-shaped object the renderers can dispatch on.

    Only the fields the renderers read are set; the rest stay None.
    target_class=0 (All) and target_group=0 (broadcast) route to both
    the perimeter and LCD surfaces regardless of operator group config.
    """
    f = Frame()
    f.message_type = MessageType.LIGHT_COMMAND
    f.target_class = 0x00  # All - both surfaces
    f.target_group = 0x00  # broadcast - bypasses group filter
    f.r = r
    f.g = g
    f.b = b
    f.attack = attack
    f.sustain = sustain
    f.release = release
    f.chance = chance
    return f


def synthesize_for_drop():
    """Bright whiteout for a DROP event. Punchy attack, full brightness,
    medium decay - reads as a peak moment."""
    return _make_synthetic(
        r=255, g=255, b=255,
        attack=Time.T_32_MS,
        sustain=Time.T_480_MS,
        release=Time.T_960_MS,
        chance=Chance.CHANCE_100,
    )


def synthesize_for_breakdown():
    """Dim cool blue for a BREAKDOWN event. Slow attack, long sustain,
    long decay - reads as a quiet, suspended moment."""
    return _make_synthetic(
        r=0, g=60, b=200,
        attack=Time.T_480_MS,
        sustain=Time.T_960_MS,
        release=Time.T_2400_MS,
        chance=Chance.CHANCE_100,
    )


def synthesize_for(event_type):
    """Return a synthetic Frame for the given MUSIC_EVENT type, or
    None for event types this app doesn't render (Unknown, Build,
    reserved values)."""
    if event_type == MusicEventType.DROP:
        return synthesize_for_drop()
    if event_type == MusicEventType.BREAKDOWN:
        return synthesize_for_breakdown()
    return None
