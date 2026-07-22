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
    TEXT_DISPLAY_MAX_HEADER_LEN,
    TEXT_DISPLAY_MAX_BODY_LEN,
    TEXT_DISPLAY_FIXED_PREFIX,
    TEXT_DISPLAY_MIN_PAYLOAD_LEN,
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
        # v0x03: Director's now_ms() at emit. Present on LIGHT_PULSE,
        # LIGHT_WASH, and LIGHT_WASH_PULSE. Lumes schedule the render
        # for (send_tick + kFleetRenderDelayMs) in director-time so
        # all badges converge on a common wall-clock fire instant.
        "send_tick",
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
        # TEXT_DISPLAY-specific (Epic 13). Display family has no
        # target_class on the wire (message type IS the class signal);
        # `target_group` here is the dedicated display-side group ID.
        "text_target_group",
        "text_r", "text_g", "text_b",
        "ttl_ms",             # u16 LE; 0 = sticky until CLEAR_SCREEN
        "header",             # decoded str (UTF-8); empty if header_len == 0
        "body",               # decoded str (UTF-8); empty if body_len == 0
        # CLEAR_SCREEN-specific (Epic 13)
        "clear_target_group",
        "clear_text",         # bool: clear the text surface
        "clear_bitmap",       # bool: clear the bitmap surface
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
        self.send_tick = None   # v0x03
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
        # TEXT_DISPLAY (Epic 13). Display-family attrs default to None
        # so a Frame instance representing a non-text message type
        # doesn't carry stale state. ClearScreen reuses target_group
        # via its own attr because the two payloads can co-exist at
        # the receive layer (e.g. when render code peeks).
        self.text_target_group = None
        self.text_r = None
        self.text_g = None
        self.text_b = None
        self.ttl_ms = None
        self.header = None
        self.body = None
        self.clear_target_group = None
        self.clear_text = None
        self.clear_bitmap = None


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
        f.send_tick = p[9] | (p[10] << 8) | (p[11] << 16) | (p[12] << 24)   # v0x03
    elif f.message_type == MessageType.HEARTBEAT:
        # Spec v0.29 §3.3.1: tick (u32 LE) + days_since_2026 (u16 LE)
        # + centiseconds_today (u24 LE). All little-endian.
        p = f.payload
        f.tick = p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24)
        f.days_since_2026 = p[4] | (p[5] << 8)
        f.centiseconds_today = p[6] | (p[7] << 8) | (p[8] << 16)
    elif f.message_type == MessageType.LIGHT_WASH:
        # v0x03 20-byte layout (v0x02 was 16 bytes; +4 for send_tick).
        # See protocol manual §3.3.3 and wire-spec-v0x03-pulse-sync-design.md.
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
        f.send_tick      = p[16] | (p[17] << 8) | (p[18] << 16) | (p[19] << 24)   # v0x03
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
        f.send_tick = p[9] | (p[10] << 8) | (p[11] << 16) | (p[12] << 24)   # v0x03
    elif f.message_type == MessageType.TEXT_DISPLAY:
        # Epic 13 variable-length layout. See protocol manual §3.3.6.
        #
        #   0   target_group    1 B
        #   1   r               1 B
        #   2   g               1 B
        #   3   b               1 B
        #   4-5 ttl_ms          2 B   u16 LE; 0 = sticky
        #   6   header_len      1 B   0..64
        #   7   header_bytes    header_len B   UTF-8
        #   ... body_len        1 B   0..128
        #   ... body_bytes      body_len B     UTF-8
        p = f.payload
        if len(p) < TEXT_DISPLAY_MIN_PAYLOAD_LEN:
            raise FrameError("TEXT_DISPLAY payload too short")
        f.text_target_group = p[0]
        f.text_r = p[1]
        f.text_g = p[2]
        f.text_b = p[3]
        f.ttl_ms = p[4] | (p[5] << 8)
        header_len = p[6]
        if header_len > TEXT_DISPLAY_MAX_HEADER_LEN:
            raise FrameError("TEXT_DISPLAY header_len exceeds cap")
        if len(p) < TEXT_DISPLAY_FIXED_PREFIX + 1 + header_len + 1:
            raise FrameError("TEXT_DISPLAY payload truncated mid-header")
        header_end = TEXT_DISPLAY_FIXED_PREFIX + 1 + header_len
        body_len = p[header_end]
        if body_len > TEXT_DISPLAY_MAX_BODY_LEN:
            raise FrameError("TEXT_DISPLAY body_len exceeds cap")
        if len(p) != header_end + 1 + body_len:
            raise FrameError("TEXT_DISPLAY trailing bytes mismatch body_len")
        header_bytes = p[TEXT_DISPLAY_FIXED_PREFIX + 1 : header_end]
        body_bytes   = p[header_end + 1 : header_end + 1 + body_len]
        # UTF-8 decode with replacement; a malformed string from a
        # buggy sender shouldn't take the receiver down. The orchestrator
        # validates UTF-8 before sending, so this is belt-and-braces.
        # NB: catch ValueError only - MicroPython has no
        # UnicodeDecodeError builtin (a bad decode raises UnicodeError,
        # whose base is ValueError), and naming it here would itself
        # raise NameError on the badge, defeating the guard.
        try:
            f.header = bytes(header_bytes).decode("utf-8")
        except ValueError:
            f.header = ""
        try:
            f.body = bytes(body_bytes).decode("utf-8")
        except ValueError:
            f.body = ""
    elif f.message_type == MessageType.CLEAR_SCREEN:
        # Epic 13 3-byte layout.
        #
        #   0   target_group    1 B
        #   1   clear_text      1 B   0 = leave, 1 = clear
        #   2   clear_bitmap    1 B   0 = leave, 1 = clear
        p = f.payload
        f.clear_target_group = p[0]
        f.clear_text   = bool(p[1])
        f.clear_bitmap = bool(p[2])

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
    send_tick=0,   # v0x03: director's now_ms() at emit; u32 LE
):
    """Build the wire bytes for a LIGHT_PULSE frame.

    Inverse of the LIGHT_PULSE branch in ``parse_frame``: 8-byte
    header + 13-byte payload = 21 bytes (v0x03; was 17 in v0x02).
    Every field is masked to a byte so an out-of-range argument
    can't corrupt the frame length.

    The Director originates frames at ``hop_count`` 0; relays increment
    it. ``sequence_number`` wraps at 256 and the caller owns the
    counter (see RenderDispatcher). ``send_tick`` is the Director's
    ``time.ticks_ms()`` at emit; Lumes schedule their render at
    ``send_tick + kFleetRenderDelayMs`` in director-time for cross-
    fleet unison. See wire-spec-v0x03-pulse-sync-design.md.
    """
    send_tick &= 0xFFFFFFFF
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
        send_tick & 0xFF,
        (send_tick >> 8) & 0xFF,
        (send_tick >> 16) & 0xFF,
        (send_tick >> 24) & 0xFF,
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
    send_tick=0,   # v0x03
):
    """Construct a LIGHT_PULSE Frame directly, for local loopback.

    The Director renders its own broadcasts on the perimeter LEDs and
    LCD ("the Director is its own first Lume"). Rather than round-trip
    through ``encode_light_pulse`` + ``parse_frame`` on every render,
    this builds the Frame the renderers consume directly. The header
    fields default to zero because the loopback path doesn't inspect
    them - only the LIGHT_PULSE payload attributes matter to the
    perimeter / LCD renderers. ``send_tick`` is v0x03 field for
    cross-Lume sync; loopback path can ignore or use it.
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
    f.send_tick = send_tick
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
    send_tick=0,     # v0x03: director's now_ms() at emit; u32 LE
):
    """Build the wire bytes for a LIGHT_WASH frame (Epic 6C Phase D).

    20-byte payload (v0x03; was 16 in v0x02, gained send_tick).
    See protocol manual §3.3.3 + wire-spec-v0x03-pulse-sync-design.md.
    """
    cycle_ms     &= 0xFFFF
    ttl_seconds  &= 0xFFFF
    send_tick    &= 0xFFFFFFFF
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
        send_tick & 0xFF,
        (send_tick >> 8) & 0xFF,
        (send_tick >> 16) & 0xFF,
        (send_tick >> 24) & 0xFF,
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
    send_tick=0,     # v0x03: director's now_ms() at emit; u32 LE
):
    """Build the wire bytes for a LIGHT_WASH_PULSE frame.

    13-byte payload (v0x03; was 9 in v0x02). Identical layout to
    LIGHT_PULSE. Dispatch semantics differ (fires only on washing
    Lumes). See protocol manual §3.3.5 + wire-spec-v0x03-pulse-sync-design.md.
    """
    send_tick &= 0xFFFFFFFF
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
        send_tick & 0xFF,
        (send_tick >> 8) & 0xFF,
        (send_tick >> 16) & 0xFF,
        (send_tick >> 24) & 0xFF,
    ))


def make_light_wash_frame(
    target_class,
    target_group,
    r1, g1, b1,
    r2, g2, b2,
    attack,
    release,
    intensity,
    cycle_ms,
    ttl_seconds,
    pulse_response,
    source_id=0,
    sequence_number=0,
    hop_count=0,
    send_tick=0,   # v0x03
):
    """Construct a LIGHT_WASH Frame directly, for local loopback.

    Mirrors make_light_pulse_frame: builds the Frame the perimeter and
    LCD renderers consume directly, skipping the encode + parse round
    trip on every render. Loopback paths don't inspect the header
    fields - only the LIGHT_WASH payload attributes matter.
    """
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id = source_id
    f.sequence_number = sequence_number
    f.hop_count = hop_count
    f.message_type = MessageType.LIGHT_WASH
    f.payload_len = _LIGHT_WASH_PAYLOAD_LEN
    f.payload = None
    f.target_class = target_class
    f.target_group = target_group
    f.r1 = r1; f.g1 = g1; f.b1 = b1
    f.r2 = r2; f.g2 = g2; f.b2 = b2
    f.wash_attack = attack
    f.wash_release = release
    f.intensity = intensity
    f.cycle_ms = cycle_ms
    f.ttl_seconds = ttl_seconds
    f.pulse_response = pulse_response
    f.send_tick = send_tick   # v0x03
    return f


def make_light_wash_end_frame(
    target_class,
    target_group,
    release_time,
    source_id=0,
    sequence_number=0,
    hop_count=0,
):
    """Construct a LIGHT_WASH_END Frame directly, for local loopback."""
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id = source_id
    f.sequence_number = sequence_number
    f.hop_count = hop_count
    f.message_type = MessageType.LIGHT_WASH_END
    f.payload_len = _LIGHT_WASH_END_PAYLOAD_LEN
    f.payload = None
    f.target_class = target_class
    f.target_group = target_group
    f.release_time = release_time
    return f


def make_light_wash_pulse_frame(
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
    send_tick=0,   # v0x03
):
    """Construct a LIGHT_WASH_PULSE Frame directly, for local loopback.

    Identical payload to LIGHT_PULSE; the renderers route by frame
    message_type (LIGHT_WASH_PULSE only fires on washing Lumes,
    bypassing pulse_response).
    """
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id = source_id
    f.sequence_number = sequence_number
    f.hop_count = hop_count
    f.message_type = MessageType.LIGHT_WASH_PULSE
    f.payload_len = _LIGHT_WASH_PULSE_PAYLOAD_LEN
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
    f.send_tick = send_tick   # v0x03
    return f


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


_CLEAR_SCREEN_PAYLOAD_LEN = PAYLOAD_LENGTHS[MessageType.CLEAR_SCREEN]


def encode_text_display(
    source_id,
    sequence_number,
    target_group,
    r,
    g,
    b,
    ttl_ms,
    header,
    body,
    hop_count=0,
):
    """Build the wire bytes for a TEXT_DISPLAY frame (Epic 13).

    8-byte header + variable-length payload (TEXT_DISPLAY_MIN_PAYLOAD_LEN
    when both strings empty, up to ~200 bytes with the maxima). Strings
    are encoded UTF-8 and length-checked against the wire caps.
    """
    if isinstance(header, str):
        header_bytes = header.encode("utf-8")
    else:
        header_bytes = bytes(header)
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = bytes(body)
    if len(header_bytes) > TEXT_DISPLAY_MAX_HEADER_LEN:
        raise FrameError("TEXT_DISPLAY header exceeds cap")
    if len(body_bytes) > TEXT_DISPLAY_MAX_BODY_LEN:
        raise FrameError("TEXT_DISPLAY body exceeds cap")
    payload_len = TEXT_DISPLAY_FIXED_PREFIX + 1 + len(header_bytes) + 1 + len(body_bytes)
    if payload_len > 255:
        raise FrameError("TEXT_DISPLAY payload overflows u8 payload_len")
    ttl = ttl_ms & 0xFFFF
    out = bytearray()
    out.extend((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.TEXT_DISPLAY,
        payload_len & 0xFF,
        target_group & 0xFF,
        r & 0xFF,
        g & 0xFF,
        b & 0xFF,
        ttl & 0xFF,
        (ttl >> 8) & 0xFF,
        len(header_bytes) & 0xFF,
    ))
    out.extend(header_bytes)
    out.append(len(body_bytes) & 0xFF)
    out.extend(body_bytes)
    return bytes(out)


def encode_clear_screen(
    source_id,
    sequence_number,
    target_group,
    clear_text=True,
    clear_bitmap=True,
    hop_count=0,
):
    """Build the wire bytes for a CLEAR_SCREEN frame (Epic 13).

    8-byte header + 3-byte payload (target_group, clear_text flag,
    clear_bitmap flag). The two flags are independent so the orchestrator
    can clear just the text layer (e.g. lyric off-screen between verses)
    without disturbing a sticky bitmap, and vice versa.
    """
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        source_id & 0xFF,
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.CLEAR_SCREEN,
        _CLEAR_SCREEN_PAYLOAD_LEN,
        target_group & 0xFF,
        1 if clear_text else 0,
        1 if clear_bitmap else 0,
    ))
