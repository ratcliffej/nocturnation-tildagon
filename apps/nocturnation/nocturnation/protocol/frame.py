"""ESP-NOW frame parser + encoder. See protocol-manual.md section 3 for
the byte spec.

Block 2 first cut: parse the header + LIGHT_COMMAND payload. Other payload
types are validated for size but not unpacked; the parser returns the raw
payload bytes so receive-side handlers can deal with them as they land.

Epic 6B B3 adds the encode side: the Director needs to *build* a
LIGHT_COMMAND frame to broadcast (the Tildagon was receive-only until
Director mode landed). `encode_light_command` produces the wire bytes;
`make_light_command_frame` builds a Frame object directly for the
Director's local loopback (no byte round-trip through the parser).
"""

from .constants import (
    HEADER_SIZE,
    MAGIC_0,
    MAGIC_1,
    MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
    PAYLOAD_LENGTHS,
    MessageType,
)


class FrameError(Exception):
    """Raised on a structurally invalid frame.

    Receivers MUST drop these silently per protocol manual section 3.1;
    callers catch this and discard the frame without further processing.
    """


class Frame:
    """A parsed ESP-NOW frame.

    Attributes mirror the header layout in protocol manual section 3.1.
    For LIGHT_COMMAND frames the unpacked payload fields are attached as
    additional attributes (target_class, target_group, r, g, b, attack,
    sustain, release, chance); for other message types only ``payload``
    is populated.
    """

    __slots__ = (
        "protocol_version",
        "source_id",
        "sequence_number",
        "hop_count",
        "message_type",
        "payload_len",
        "payload",
        # LIGHT_COMMAND-specific
        "target_class",
        "target_group",
        "r",
        "g",
        "b",
        "attack",
        "sustain",
        "release",
        "chance",
        # HEARTBEAT-specific (spec v0.29 §3.3.1)
        "tick",
        "days_since_2026",
        "centiseconds_today",
    )

    def __init__(self):
        self.target_class = None
        self.target_group = None
        self.r = None
        self.g = None
        self.b = None
        self.attack = None
        self.sustain = None
        self.release = None
        self.chance = None
        self.tick = None
        self.days_since_2026 = None
        self.centiseconds_today = None


def parse_frame(buf):
    """Parse the bytes-like ``buf`` into a Frame.

    Raises FrameError on any structural problem; the caller drops the
    frame silently per the protocol manual.
    """
    if len(buf) < HEADER_SIZE:
        raise FrameError("frame shorter than header")
    if len(buf) > MAX_FRAME_SIZE:
        raise FrameError("frame longer than max_frame_size")

    # Magic check first - cheapest rejection path for non-NocturNation
    # ESP-NOW chatter sharing the channel. Bytes 0-1 must be "NN" or the
    # frame is from a different sender entirely and we drop without
    # touching any other field.
    if buf[0] != MAGIC_0 or buf[1] != MAGIC_1:
        raise FrameError("not a NocturNation frame (magic mismatch)")

    f = Frame()
    f.protocol_version = buf[2]
    f.source_id = buf[3]
    f.sequence_number = buf[4]
    f.hop_count = buf[5]
    f.message_type = buf[6]
    f.payload_len = buf[7]

    if f.protocol_version != PROTOCOL_VERSION:
        raise FrameError("unrecognised protocol version")

    if len(buf) != HEADER_SIZE + f.payload_len:
        raise FrameError("payload_len does not match frame length")

    expected_len = PAYLOAD_LENGTHS.get(f.message_type)
    if expected_len is not None and f.payload_len != expected_len:
        raise FrameError("payload_len does not match message_type")

    f.payload = bytes(buf[HEADER_SIZE:])

    if f.message_type == MessageType.LIGHT_COMMAND:
        p = f.payload
        f.target_class = p[0]
        f.target_group = p[1]
        f.r = p[2]
        f.g = p[3]
        f.b = p[4]
        f.attack = p[5]
        f.sustain = p[6]
        f.release = p[7]
        f.chance = p[8]
    elif f.message_type == MessageType.HEARTBEAT:
        # Spec v0.29 §3.3.1: tick (u32 LE) + days_since_2026 (u16 LE)
        # + centiseconds_today (u24 LE). All little-endian.
        p = f.payload
        f.tick = p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24)
        f.days_since_2026 = p[4] | (p[5] << 8)
        f.centiseconds_today = p[6] | (p[7] << 8) | (p[8] << 16)

    return f


_LIGHT_COMMAND_PAYLOAD_LEN = PAYLOAD_LENGTHS[MessageType.LIGHT_COMMAND]


def encode_light_command(
    source_id,
    sequence_number,
    target_class,
    target_group,
    r,
    g,
    b,
    attack,
    sustain,
    release,
    chance,
    hop_count=0,
):
    """Build the wire bytes for a LIGHT_COMMAND frame.

    Inverse of the LIGHT_COMMAND branch in ``parse_frame``: 8-byte
    header + 9-byte payload = 17 bytes. Every field is masked to a
    byte so an out-of-range argument can't corrupt the frame length.

    The Director originates frames at ``hop_count`` 0; relays increment
    it. ``sequence_number`` wraps at 256 and the caller owns the
    counter (see RenderDispatcher).
    """
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_COMMAND,
        _LIGHT_COMMAND_PAYLOAD_LEN,
        target_class & 0xFF,
        target_group & 0xFF,
        r & 0xFF,
        g & 0xFF,
        b & 0xFF,
        attack & 0xFF,
        sustain & 0xFF,
        release & 0xFF,
        chance & 0xFF,
    ))


def make_light_command_frame(
    target_class,
    target_group,
    r,
    g,
    b,
    attack,
    sustain,
    release,
    chance,
    source_id=0,
    sequence_number=0,
    hop_count=0,
):
    """Construct a LIGHT_COMMAND Frame directly, for local loopback.

    The Director renders its own broadcasts on the perimeter LEDs and
    LCD ("the Director is its own first Lume"). Rather than round-trip
    through ``encode_light_command`` + ``parse_frame`` on every render,
    this builds the Frame the renderers consume directly. The header
    fields default to zero because the loopback path doesn't inspect
    them - only the LIGHT_COMMAND payload attributes matter to the
    perimeter / LCD renderers.
    """
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id = source_id
    f.sequence_number = sequence_number
    f.hop_count = hop_count
    f.message_type = MessageType.LIGHT_COMMAND
    f.payload_len = _LIGHT_COMMAND_PAYLOAD_LEN
    f.payload = None
    f.target_class = target_class
    f.target_group = target_group
    f.r = r
    f.g = g
    f.b = b
    f.attack = attack
    f.sustain = sustain
    f.release = release
    f.chance = chance
    return f
