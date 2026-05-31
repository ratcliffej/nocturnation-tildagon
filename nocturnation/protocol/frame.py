"""ESP-NOW frame parser + encoder. See protocol-manual.md section 3 for
the byte spec.

Block 2 first cut: parse the header + LIGHT_PULSE payload. Other payload
types are validated for size but not unpacked; the parser returns the raw
payload bytes so receive-side handlers can deal with them as they land.

Epic 6B B3 adds the encode side: the Director needs to *build* a
LIGHT_PULSE frame to broadcast (the Tildagon was receive-only until
Director mode landed). `encode_light_pulse` produces the wire bytes;
`make_light_pulse_frame` builds a Frame object directly for the
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
    For LIGHT_PULSE frames the unpacked payload fields are attached as
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
        # LIGHT_PULSE / LIGHT_WASH_PULSE-specific
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
        # LIGHT_WASH-specific (Epic 6C Phase D / G2)
        "r1", "g1", "b1",     # start colour
        "r2", "g2", "b2",     # end colour (ignored at render when cycle_ms == 0)
        "wash_attack",        # 100 ms units (0-25.5 s)
        "wash_release",       # 100 ms units (default fade-out)
        "intensity",          # 0..255 wash brightness scalar
        "cycle_ms",           # u16 LE; 0 = no cycle, hold r1/g1/b1
        "ttl_seconds",        # u16 LE; 0 = infinite
        "pulse_response",     # 0 = drop PULSE while washing; 1 = additive overlay
        # LIGHT_WASH_END-specific
        "release_time",       # 100 ms units; overrides wash's own release
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
        # WASH-family slots default to None until a wash-family frame
        # populates them. Render code checks message_type before
        # reading these.
        self.r1 = None;  self.g1 = None;  self.b1 = None
        self.r2 = None;  self.g2 = None;  self.b2 = None
        self.wash_attack = None
        self.wash_release = None
        self.intensity = None
        self.cycle_ms = None
        self.ttl_seconds = None
        self.pulse_response = None
        self.release_time = None


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

    if f.message_type == MessageType.LIGHT_PULSE:
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
    elif f.message_type == MessageType.LIGHT_WASH:
        # Epic 6C Phase D 16-byte layout. See protocol manual §3.3.3.
        p = f.payload
        f.target_class   = p[0]
        f.target_group   = p[1]
        f.r1 = p[2];  f.g1 = p[3];  f.b1 = p[4]
        f.r2 = p[5];  f.g2 = p[6];  f.b2 = p[7]
        f.wash_attack    = p[8]
        f.wash_release   = p[9]
        f.intensity      = p[10]
        f.cycle_ms       = p[11] | (p[12] << 8)
        f.ttl_seconds    = p[13] | (p[14] << 8)
        f.pulse_response = p[15]
    elif f.message_type == MessageType.LIGHT_WASH_END:
        # Epic 6C Phase D 3-byte layout. See protocol manual §3.3.4.
        p = f.payload
        f.target_class = p[0]
        f.target_group = p[1]
        f.release_time = p[2]
    elif f.message_type == MessageType.LIGHT_WASH_PULSE:
        # Same wire layout as LIGHT_PULSE; dispatch semantics differ
        # (fires only on washing Lumes). See protocol manual §3.3.5.
        p = f.payload
        f.target_class = p[0]
        f.target_group = p[1]
        f.r = p[2]
        f.g = p[3]
        f.b = p[4]
        f.attack  = p[5]
        f.sustain = p[6]
        f.release = p[7]
        f.chance  = p[8]

    return f


_LIGHT_PULSE_PAYLOAD_LEN = PAYLOAD_LENGTHS[MessageType.LIGHT_PULSE]


def encode_light_pulse(
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
    """Build the wire bytes for a LIGHT_PULSE frame.

    Inverse of the LIGHT_PULSE branch in ``parse_frame``: 8-byte
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
        MessageType.LIGHT_PULSE,
        _LIGHT_PULSE_PAYLOAD_LEN,
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


def make_light_pulse_frame(
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
    """Construct a LIGHT_PULSE Frame directly, for local loopback.

    The Director renders its own broadcasts on the perimeter LEDs and
    LCD ("the Director is its own first Lume"). Rather than round-trip
    through ``encode_light_pulse`` + ``parse_frame`` on every render,
    this builds the Frame the renderers consume directly. The header
    fields default to zero because the loopback path doesn't inspect
    them - only the LIGHT_PULSE payload attributes matter to the
    perimeter / LCD renderers.
    """
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id = source_id
    f.sequence_number = sequence_number
    f.hop_count = hop_count
    f.message_type = MessageType.LIGHT_PULSE
    f.payload_len = _LIGHT_PULSE_PAYLOAD_LEN
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


_LIGHT_WASH_PAYLOAD_LEN       = PAYLOAD_LENGTHS[MessageType.LIGHT_WASH]
_LIGHT_WASH_END_PAYLOAD_LEN   = PAYLOAD_LENGTHS[MessageType.LIGHT_WASH_END]
_LIGHT_WASH_PULSE_PAYLOAD_LEN = PAYLOAD_LENGTHS[MessageType.LIGHT_WASH_PULSE]


def encode_light_wash(
    source_id,
    sequence_number,
    target_class,
    target_group,
    r1, g1, b1,
    r2, g2, b2,
    attack,          # 100 ms units
    release,         # 100 ms units (default fade-out)
    intensity,       # 0..255 brightness scalar
    cycle_ms,        # u16 LE; 0 = hold r1g1b1
    ttl_seconds,     # u16 LE; 0 = infinite
    pulse_response,  # 0 = drop PULSE while washing; 1 = additive overlay
    hop_count=0,
):
    """Build the wire bytes for a LIGHT_WASH frame (Epic 6C Phase D).

    16-byte payload; see protocol manual §3.3.3. Tildagon Director mode
    is pulse-only in v1 - this encoder exists for round-trip tests and
    future cross-platform parity, not for routine Director-side use.
    """
    cycle_ms     &= 0xFFFF
    ttl_seconds  &= 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_WASH,
        _LIGHT_WASH_PAYLOAD_LEN,
        target_class & 0xFF,
        target_group & 0xFF,
        r1 & 0xFF, g1 & 0xFF, b1 & 0xFF,
        r2 & 0xFF, g2 & 0xFF, b2 & 0xFF,
        attack & 0xFF,
        release & 0xFF,
        intensity & 0xFF,
        cycle_ms & 0xFF,
        (cycle_ms >> 8) & 0xFF,
        ttl_seconds & 0xFF,
        (ttl_seconds >> 8) & 0xFF,
        pulse_response & 0xFF,
    ))


def encode_light_wash_end(
    source_id,
    sequence_number,
    target_class,
    target_group,
    release_time,    # 100 ms units; overrides wash's own release
    hop_count=0,
):
    """Build the wire bytes for a LIGHT_WASH_END frame.

    3-byte payload. See protocol manual §3.3.4.
    """
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_WASH_END,
        _LIGHT_WASH_END_PAYLOAD_LEN,
        target_class & 0xFF,
        target_group & 0xFF,
        release_time & 0xFF,
    ))


def encode_light_wash_pulse(
    source_id,
    sequence_number,
    target_class,
    target_group,
    r, g, b,
    attack, sustain, release,
    chance,
    hop_count=0,
):
    """Build the wire bytes for a LIGHT_WASH_PULSE frame.

    9-byte payload, identical layout to LIGHT_PULSE. Dispatch semantics
    differ (fires only on washing Lumes). See protocol manual §3.3.5.
    """
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_WASH_PULSE,
        _LIGHT_WASH_PULSE_PAYLOAD_LEN,
        target_class & 0xFF,
        target_group & 0xFF,
        r & 0xFF, g & 0xFF, b & 0xFF,
        attack & 0xFF,
        sustain & 0xFF,
        release & 0xFF,
        chance & 0xFF,
    ))


_HEARTBEAT_PAYLOAD_LEN = PAYLOAD_LENGTHS[MessageType.HEARTBEAT]


def encode_heartbeat(
    source_id,
    sequence_number,
    tick=0,
    days_since_2026=0,
    centiseconds_today=0,
    hop_count=0,
):
    """Build the wire bytes for a HEARTBEAT frame (spec v0.29 §3.3.1).

    8-byte header + 9-byte payload: tick (u32 LE) + days_since_2026
    (u16 LE) + centiseconds_today (u24 LE). The Director beacons this
    at ~1 Hz so Lumes can discover its channel and keep their TOFU
    lock alive between LIGHT_PULSEs. The clock fields are best-effort
    (the Tildagon Director has no synced RTC); current Lumes treat any
    valid frame as liveness and don't act on these values - CLOCK_SYNC
    / TIME_SYNC were removed in the v0.29 protocol trim.
    """
    tick &= 0xFFFFFFFF
    days = days_since_2026 & 0xFFFF
    cs = centiseconds_today & 0xFFFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.HEARTBEAT,
        _HEARTBEAT_PAYLOAD_LEN,
        tick & 0xFF,
        (tick >> 8) & 0xFF,
        (tick >> 16) & 0xFF,
        (tick >> 24) & 0xFF,
        days & 0xFF,
        (days >> 8) & 0xFF,
        cs & 0xFF,
        (cs >> 8) & 0xFF,
        (cs >> 16) & 0xFF,
    ))
