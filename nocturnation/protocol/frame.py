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
        # v4 LED-level addressing (Epic 18). Populated for LIGHT_PULSE /
        # LIGHT_WASH / LIGHT_WASH_END / LIGHT_WASH_PULSE. Renderer honours
        # them only on Lumes with the AddressableLeds capability; single-
        # LED Lumes (PixMob) drop modes 1+ at the gate. Default 0 (=All)
        # reproduces v3-era whole-strip behaviour.
        "led_mode",
        "led_modifier1",
        "led_modifier2",
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
        # v4 LED-level addressing (Epic 18). Default None; parse_frame
        # populates them for the four LIGHT_* message types.
        self.led_mode = None
        self.led_modifier1 = None
        self.led_modifier2 = None
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
    # v3: source_id widened to LE u16 at bytes 3-4; every field after
    # it shifted by 1. Old-firmware devices reject at the version check
    # below (they read byte 3 as the whole source_id and byte 6 as
    # message_type, both of which we bump).
    f.source_id = buf[3] | (buf[4] << 8)
    f.sequence_number = buf[5]
    f.hop_count = buf[6]
    f.message_type = buf[7]
    f.payload_len = buf[8]

    if f.protocol_version != PROTOCOL_VERSION:
        raise FrameError("unrecognised protocol version")

    if len(buf) != HEADER_SIZE + f.payload_len:
        raise FrameError("payload_len does not match frame length")

    expected_len = PAYLOAD_LENGTHS.get(f.message_type)
    if expected_len is not None and f.payload_len != expected_len:
        raise FrameError("payload_len does not match message_type")

    f.payload = bytes(buf[HEADER_SIZE:])

    if f.message_type == MessageType.LIGHT_PULSE:
        # v3: target_group widened to LE u16 at payload[1..2].
        # v4: +3 trailing bytes at payload[10..12] for LED-level addressing.
        p = f.payload
        f.target_class = p[0]
        f.target_group = p[1] | (p[2] << 8)
        f.r = p[3]
        f.g = p[4]
        f.b = p[5]
        f.attack = p[6]
        f.sustain = p[7]
        f.release = p[8]
        f.chance = p[9]
        f.led_mode      = p[10]   # v4
        f.led_modifier1 = p[11]   # v4
        f.led_modifier2 = p[12]   # v4
    elif f.message_type == MessageType.HEARTBEAT:
        # Spec v0.29 §3.3.1: tick (u32 LE) + days_since_2026 (u16 LE)
        # + centiseconds_today (u24 LE). All little-endian.
        p = f.payload
        f.tick = p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24)
        f.days_since_2026 = p[4] | (p[5] << 8)
        f.centiseconds_today = p[6] | (p[7] << 8) | (p[8] << 16)
    elif f.message_type == MessageType.LIGHT_WASH:
        # v3 17-byte layout: +1 byte for the u16 target_group widening.
        # v4 20-byte layout: +3 trailing bytes for LED-level addressing.
        p = f.payload
        f.target_class   = p[0]
        f.target_group   = p[1] | (p[2] << 8)
        f.r1 = p[3];  f.g1 = p[4];  f.b1 = p[5]
        f.r2 = p[6];  f.g2 = p[7];  f.b2 = p[8]
        f.wash_attack    = p[9]
        f.wash_release   = p[10]
        f.intensity      = p[11]
        f.cycle_ms       = p[12] | (p[13] << 8)
        f.ttl_seconds    = p[14] | (p[15] << 8)
        f.pulse_response = p[16]
        f.led_mode       = p[17]   # v4
        f.led_modifier1  = p[18]   # v4
        f.led_modifier2  = p[19]   # v4
    elif f.message_type == MessageType.LIGHT_WASH_END:
        # v3 4-byte layout: +1 byte for the u16 target_group widening.
        # v4 7-byte layout: +3 trailing bytes for LED-level addressing.
        p = f.payload
        f.target_class  = p[0]
        f.target_group  = p[1] | (p[2] << 8)
        f.release_time  = p[3]
        f.led_mode      = p[4]     # v4
        f.led_modifier1 = p[5]     # v4
        f.led_modifier2 = p[6]     # v4
    elif f.message_type == MessageType.LIGHT_WASH_PULSE:
        # Same wire layout as LIGHT_PULSE; dispatch semantics differ
        # (fires only on washing Lumes). See protocol manual §3.3.5.
        # v4: +3 trailing bytes for LED-level addressing at payload[10..12].
        p = f.payload
        f.target_class = p[0]
        f.target_group = p[1] | (p[2] << 8)
        f.r = p[3]
        f.g = p[4]
        f.b = p[5]
        f.attack        = p[6]
        f.sustain       = p[7]
        f.release       = p[8]
        f.chance        = p[9]
        f.led_mode      = p[10]    # v4
        f.led_modifier1 = p[11]    # v4
        f.led_modifier2 = p[12]    # v4
    elif f.message_type == MessageType.TEXT_DISPLAY:
        # v3 variable-length layout. See protocol manual §3.3.6.
        #
        #   0-1 target_group    2 B  u16 LE
        #   2   r               1 B
        #   3   g               1 B
        #   4   b               1 B
        #   5-6 ttl_ms          2 B  u16 LE; 0 = sticky
        #   7   header_len      1 B  0..64
        #   8   header_bytes    header_len B   UTF-8
        #   ... body_len        1 B  0..128
        #   ... body_bytes      body_len B     UTF-8
        p = f.payload
        if len(p) < TEXT_DISPLAY_MIN_PAYLOAD_LEN:
            raise FrameError("TEXT_DISPLAY payload too short")
        f.text_target_group = p[0] | (p[1] << 8)   # v3: LE u16
        f.text_r = p[2]
        f.text_g = p[3]
        f.text_b = p[4]
        f.ttl_ms = p[5] | (p[6] << 8)
        header_len = p[7]
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
        # v3 4-byte layout: +1 byte for the u16 target_group widening.
        #
        #   0-1 target_group    2 B  u16 LE
        #   2   clear_text      1 B  0 = leave, 1 = clear
        #   3   clear_bitmap    1 B  0 = leave, 1 = clear
        p = f.payload
        f.clear_target_group = p[0] | (p[1] << 8)   # v3: LE u16
        f.clear_text   = bool(p[2])
        f.clear_bitmap = bool(p[3])

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
    led_mode=0,        # v4 (Epic 18); 0 = LedMode.ALL (whole strip)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
):
    """Build the wire bytes for a LIGHT_PULSE frame.

    Inverse of the LIGHT_PULSE branch in ``parse_frame``: v4 9-byte
    header + 13-byte payload = 22 bytes. source_id and target_group
    are both LE u16 (v3 widening from u8). led_mode/led_modifier1/
    led_modifier2 default to 0 for parity with v3-era Directors that
    don't yet emit LED-level addressing.

    The Director originates frames at ``hop_count`` 0; relays increment
    it. ``sequence_number`` wraps at 256 and the caller owns the
    counter (see RenderDispatcher).
    """
    src = source_id  & 0xFFFF
    tgt = target_group & 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src  & 0xFF, (src  >> 8) & 0xFF,     # v3: source_id LE u16
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_PULSE,
        _LIGHT_PULSE_PAYLOAD_LEN,
        target_class & 0xFF,
        tgt  & 0xFF, (tgt  >> 8) & 0xFF,     # v3: target_group LE u16
        r & 0xFF,
        g & 0xFF,
        b & 0xFF,
        attack & 0xFF,
        sustain & 0xFF,
        release & 0xFF,
        chance & 0xFF,
        led_mode      & 0xFF,   # v4
        led_modifier1 & 0xFF,   # v4
        led_modifier2 & 0xFF,   # v4
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
    led_mode=0,        # v4 (Epic 18)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
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
    f.led_mode      = led_mode
    f.led_modifier1 = led_modifier1
    f.led_modifier2 = led_modifier2
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
    led_mode=0,        # v4 (Epic 18)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
):
    """Build the wire bytes for a LIGHT_WASH frame (Epic 6C Phase D).

    v4 20-byte payload (v3 17 + 3 for LED addressing); see protocol
    manual §3.3.3. Tildagon Director mode is pulse-only in v1 - this
    encoder exists for round-trip tests and future cross-platform
    parity, not for routine Director-side use.
    """
    cycle_ms     &= 0xFFFF
    ttl_seconds  &= 0xFFFF
    src = source_id & 0xFFFF
    tgt = target_group & 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src & 0xFF, (src >> 8) & 0xFF,       # v3: source_id LE u16
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_WASH,
        _LIGHT_WASH_PAYLOAD_LEN,
        target_class & 0xFF,
        tgt & 0xFF, (tgt >> 8) & 0xFF,       # v3: target_group LE u16
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
        led_mode      & 0xFF,   # v4
        led_modifier1 & 0xFF,   # v4
        led_modifier2 & 0xFF,   # v4
    ))


def encode_light_wash_end(
    source_id,
    sequence_number,
    target_class,
    target_group,
    release_time,    # 100 ms units; overrides wash's own release
    hop_count=0,
    led_mode=0,        # v4 (Epic 18); mode-0 ends whole strip's wash
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
):
    """Build the wire bytes for a LIGHT_WASH_END frame.

    v4 7-byte payload (v3 4 + 3 for LED addressing). See protocol
    manual §3.3.4. LedMode-0 ends the whole strip's wash; mode-1 ends
    one pixel; mode-2 ends the masked pixels.
    """
    src = source_id & 0xFFFF
    tgt = target_group & 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src & 0xFF, (src >> 8) & 0xFF,       # v3: source_id LE u16
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_WASH_END,
        _LIGHT_WASH_END_PAYLOAD_LEN,
        target_class & 0xFF,
        tgt & 0xFF, (tgt >> 8) & 0xFF,       # v3: target_group LE u16
        release_time & 0xFF,
        led_mode      & 0xFF,   # v4
        led_modifier1 & 0xFF,   # v4
        led_modifier2 & 0xFF,   # v4
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
    led_mode=0,        # v4 (Epic 18)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
):
    """Build the wire bytes for a LIGHT_WASH_PULSE frame.

    v4 13-byte payload (v3 10 + 3 for LED addressing), identical
    layout to LIGHT_PULSE. Dispatch semantics differ (fires only on
    washing Lumes). See protocol manual §3.3.5.
    """
    src = source_id & 0xFFFF
    tgt = target_group & 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src & 0xFF, (src >> 8) & 0xFF,       # v3: source_id LE u16
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.LIGHT_WASH_PULSE,
        _LIGHT_WASH_PULSE_PAYLOAD_LEN,
        target_class & 0xFF,
        tgt & 0xFF, (tgt >> 8) & 0xFF,       # v3: target_group LE u16
        r & 0xFF, g & 0xFF, b & 0xFF,
        attack & 0xFF,
        sustain & 0xFF,
        release & 0xFF,
        chance & 0xFF,
        led_mode      & 0xFF,   # v4
        led_modifier1 & 0xFF,   # v4
        led_modifier2 & 0xFF,   # v4
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
    led_mode=0,        # v4 (Epic 18)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
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
    f.led_mode      = led_mode
    f.led_modifier1 = led_modifier1
    f.led_modifier2 = led_modifier2
    return f


def make_light_wash_end_frame(
    target_class,
    target_group,
    release_time,
    source_id=0,
    sequence_number=0,
    hop_count=0,
    led_mode=0,        # v4 (Epic 18)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
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
    f.led_mode      = led_mode
    f.led_modifier1 = led_modifier1
    f.led_modifier2 = led_modifier2
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
    led_mode=0,        # v4 (Epic 18)
    led_modifier1=0,   # v4
    led_modifier2=0,   # v4
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
    f.led_mode      = led_mode
    f.led_modifier1 = led_modifier1
    f.led_modifier2 = led_modifier2
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
    src = source_id & 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src & 0xFF, (src >> 8) & 0xFF,       # v3: source_id LE u16
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
    src = source_id & 0xFFFF
    tgt = target_group & 0xFFFF
    out = bytearray()
    out.extend((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src & 0xFF, (src >> 8) & 0xFF,       # v3: source_id LE u16
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.TEXT_DISPLAY,
        payload_len & 0xFF,
        tgt & 0xFF, (tgt >> 8) & 0xFF,       # v3: target_group LE u16
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
    src = source_id & 0xFFFF
    tgt = target_group & 0xFFFF
    return bytes((
        MAGIC_0,
        MAGIC_1,
        PROTOCOL_VERSION,
        src & 0xFF, (src >> 8) & 0xFF,       # v3: source_id LE u16
        sequence_number & 0xFF,
        hop_count & 0xFF,
        MessageType.CLEAR_SCREEN,
        _CLEAR_SCREEN_PAYLOAD_LEN,
        tgt & 0xFF, (tgt >> 8) & 0xFF,       # v3: target_group LE u16
        1 if clear_text else 0,
        1 if clear_bitmap else 0,
    ))
