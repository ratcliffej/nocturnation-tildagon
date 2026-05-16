"""ESP-NOW frame parser. See protocol-manual.md section 3 for the byte spec.

Block 2 first cut: parse the header + LIGHT_COMMAND payload. Other payload
types are validated for size but not unpacked; the parser returns the raw
payload bytes so receive-side handlers can deal with them as they land.
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
