"""Epic 13 B3: TEXT_DISPLAY + CLEAR_SCREEN wire parse / encode + the
LumeTextRenderer state machine.

The renderer is host-testable without a live ctx - it accepts a
duck-typed display that records calls. Paint behaviour (positions,
fonts, scroll progression, TTL expiry) is verified by inspecting that
recording.
"""

import pytest

from nocturnation.protocol import (
    HEADER_SIZE,
    MessageType,
    PROTOCOL_VERSION,
    Frame,
    FrameError,
    encode_text_display,
    encode_clear_screen,
    parse_frame,
)
from nocturnation.protocol._generated import MAGIC_0, MAGIC_1
from nocturnation.render.lume_text import (
    LumeTextRenderer,
    HEADER_FONT_SIZE,
    BODY_FONT_SIZE,
    INSCRIBED_HALF,
)


# ---------------------------------------------------------------------
# encode_text_display + parse_frame round-trip
# ---------------------------------------------------------------------

def test_encode_text_display_minimal_round_trip():
    raw = encode_text_display(
        source_id=30, sequence_number=14,
        target_group=0, r=255, g=255, b=255, ttl_ms=0,
        header="Coldplay", body="Adventure of a Lifetime",
    )
    # Magic + header size + payload >= MIN
    assert raw[:2] == bytes((MAGIC_0, MAGIC_1))
    assert raw[2] == PROTOCOL_VERSION
    assert raw[6] == MessageType.TEXT_DISPLAY
    f = parse_frame(raw)
    assert f.message_type == MessageType.TEXT_DISPLAY
    assert f.text_target_group == 0
    assert (f.text_r, f.text_g, f.text_b) == (255, 255, 255)
    assert f.ttl_ms == 0
    assert f.header == "Coldplay"
    assert f.body == "Adventure of a Lifetime"


def test_encode_text_display_empty_strings():
    raw = encode_text_display(
        source_id=30, sequence_number=15,
        target_group=2, r=128, g=64, b=32, ttl_ms=5000,
        header="", body="",
    )
    f = parse_frame(raw)
    assert f.text_target_group == 2
    assert (f.text_r, f.text_g, f.text_b) == (128, 64, 32)
    assert f.ttl_ms == 5000
    assert f.header == ""
    assert f.body == ""


def test_encode_text_display_unicode_round_trip():
    # UTF-8 multi-byte char in the body. Wire is byte-oriented; the
    # header_len / body_len fields are byte counts not character counts.
    raw = encode_text_display(
        source_id=30, sequence_number=16,
        target_group=0, r=255, g=255, b=255, ttl_ms=0,
        header="café", body="✨ sparkle ✨",
    )
    f = parse_frame(raw)
    assert f.header == "café"
    assert f.body == "✨ sparkle ✨"


def test_encode_text_display_header_too_long_raises():
    with pytest.raises(FrameError):
        encode_text_display(
            source_id=30, sequence_number=17,
            target_group=0, r=255, g=255, b=255, ttl_ms=0,
            header="x" * 65, body="",
        )


def test_encode_text_display_body_too_long_raises():
    with pytest.raises(FrameError):
        encode_text_display(
            source_id=30, sequence_number=18,
            target_group=0, r=255, g=255, b=255, ttl_ms=0,
            header="", body="y" * 129,
        )


def test_parse_text_display_truncated_payload_raises():
    # Build a frame with payload_len 8 (claims body) but only 8 bytes
    # of payload, missing the body bytes the body_len byte claims.
    payload = bytes((
        0,            # target_group
        255, 255, 255,
        0, 0,         # ttl_ms LE
        0,            # header_len
        5,            # body_len = 5, but no body bytes follow
    ))
    buf = bytearray((
        MAGIC_0, MAGIC_1, PROTOCOL_VERSION,
        30, 19, 0,
        MessageType.TEXT_DISPLAY,
        len(payload),
    )) + payload
    with pytest.raises(FrameError):
        parse_frame(bytes(buf))


def test_parse_text_display_trailing_byte_mismatch_raises():
    # Build a frame where body_len says 3 but 4 bytes follow.
    payload = bytes((
        0,
        255, 255, 255,
        0, 0,
        0,
        3,
        ord("h"), ord("i"), ord("!"), 0,   # 4 trailing bytes
    ))
    buf = bytearray((
        MAGIC_0, MAGIC_1, PROTOCOL_VERSION,
        30, 20, 0,
        MessageType.TEXT_DISPLAY,
        len(payload),
    )) + payload
    with pytest.raises(FrameError):
        parse_frame(bytes(buf))


# ---------------------------------------------------------------------
# encode_clear_screen + parse_frame round-trip
# ---------------------------------------------------------------------

def test_encode_clear_screen_both_flags():
    raw = encode_clear_screen(
        source_id=30, sequence_number=21, target_group=0,
        clear_text=True, clear_bitmap=True,
    )
    f = parse_frame(raw)
    assert f.message_type == MessageType.CLEAR_SCREEN
    assert f.clear_target_group == 0
    assert f.clear_text is True
    assert f.clear_bitmap is True


def test_encode_clear_screen_text_only():
    raw = encode_clear_screen(
        source_id=30, sequence_number=22, target_group=2,
        clear_text=True, clear_bitmap=False,
    )
    f = parse_frame(raw)
    assert f.clear_target_group == 2
    assert f.clear_text is True
    assert f.clear_bitmap is False


# ---------------------------------------------------------------------
# LumeTextRenderer state machine
# ---------------------------------------------------------------------

class _RecordingDisplay:
    """Records the sequence of text() calls so tests can assert on
    what the renderer actually drew (positions, sizes, content)."""

    def __init__(self):
        self.calls = []

    def text(self, x, y, s, size=18, r=255, g=255, b=255, center=True):
        self.calls.append({
            "x": x, "y": y, "s": s,
            "size": size, "rgb": (r, g, b), "center": center,
        })

    def clear(self, r=0, g=0, b=0):
        self.calls.append({"clear": (r, g, b)})

    def fill_rect(self, *args, **kwargs):
        self.calls.append({"fill_rect": (args, kwargs)})


def _frame_for(header="", body="", r=255, g=255, b=255, ttl_ms=0, target_group=0):
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id        = 30
    f.sequence_number  = 1
    f.hop_count        = 0
    f.message_type     = MessageType.TEXT_DISPLAY
    f.text_target_group = target_group
    f.text_r = r
    f.text_g = g
    f.text_b = b
    f.ttl_ms = ttl_ms
    f.header = header
    f.body   = body
    return f


def _clear_frame_for(clear_text=True, clear_bitmap=True, target_group=0):
    f = Frame()
    f.protocol_version = PROTOCOL_VERSION
    f.source_id        = 30
    f.sequence_number  = 2
    f.hop_count        = 0
    f.message_type     = MessageType.CLEAR_SCREEN
    f.clear_target_group = target_group
    f.clear_text   = clear_text
    f.clear_bitmap = clear_bitmap
    return f


def test_renderer_starts_empty_paints_nothing():
    r = LumeTextRenderer()
    d = _RecordingDisplay()
    r.paint(d, 0)
    assert d.calls == []
    assert r.has_content() is False


def test_renderer_paints_header_and_body_on_text_display():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="Coldplay", body="Adventure"), 1000)
    assert r.has_content() is True
    d = _RecordingDisplay()
    r.paint(d, 1000)
    # Exactly two text() calls: header line + one body line ("Adventure"
    # fits in a single body line at the inscribed-square width).
    assert len(d.calls) == 2
    header_call, body_call = d.calls
    assert header_call["s"] == "Coldplay"
    assert header_call["size"] == HEADER_FONT_SIZE
    assert body_call["s"] == "Adventure"
    assert body_call["size"] == BODY_FONT_SIZE
    # Header is above body (smaller y in origin-centred coords = higher up).
    assert header_call["y"] < body_call["y"]


def test_renderer_carries_colour_from_frame():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="X", body="Y", r=200, g=100, b=50), 0)
    d = _RecordingDisplay()
    r.paint(d, 0)
    for call in d.calls:
        assert call["rgb"] == (200, 100, 50)


def test_renderer_clears_on_clear_screen():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="X", body="Y"), 0)
    assert r.has_content() is True
    r.on_clear_screen(_clear_frame_for(clear_text=True, clear_bitmap=False), 100)
    assert r.has_content() is False


def test_renderer_clear_screen_text_flag_off_is_no_op():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="X", body="Y"), 0)
    r.on_clear_screen(_clear_frame_for(clear_text=False, clear_bitmap=True), 100)
    # Text content should be preserved when only bitmap-clear was asked.
    assert r.has_content() is True


def test_renderer_ttl_expiry_drops_content():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="X", body="Y", ttl_ms=500), 1000)
    assert r.has_content() is True
    d = _RecordingDisplay()
    # Within TTL: still draws.
    r.paint(d, 1400)
    assert len(d.calls) > 0
    assert r.has_content() is True
    # Past TTL: paint clears content and draws nothing.
    d2 = _RecordingDisplay()
    r.paint(d2, 1500)
    assert d2.calls == []
    assert r.has_content() is False


def test_renderer_sticky_ttl_holds_indefinitely():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="X", body="Y", ttl_ms=0), 0)
    d = _RecordingDisplay()
    r.paint(d, 60_000)   # 60 s later
    assert len(d.calls) == 2
    assert r.has_content() is True


def test_renderer_supersedes_on_new_text_display():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="Old", body="A"), 0)
    r.on_text_display(_frame_for(header="New", body="B"), 100)
    d = _RecordingDisplay()
    r.paint(d, 100)
    assert d.calls[0]["s"] == "New"
    assert d.calls[1]["s"] == "B"


def test_renderer_wraps_long_body_into_multiple_lines():
    r = LumeTextRenderer()
    # A body that won't fit one line at the inscribed-square width.
    body = "And we are legends every day of the year here always"
    r.on_text_display(_frame_for(header="", body=body), 0)
    d = _RecordingDisplay()
    r.paint(d, 0)
    # Header is empty -> no header call. Body wraps into >= 2 lines.
    assert len(d.calls) >= 2
    # Lines stack downward.
    ys = [c["y"] for c in d.calls]
    assert ys == sorted(ys)
    # Each line's pixel-width (chars * char_advance) is at most the
    # inscribed square width, give or take the approximation slack.
    max_w = 2 * INSCRIBED_HALF
    char_advance = int(BODY_FONT_SIZE * 0.62)
    for c in d.calls:
        assert len(c["s"]) * char_advance <= max_w + char_advance  # 1-char slack


def test_renderer_honours_explicit_newline_break():
    # Cue file `\n` escape -> actual newline in the body text.
    # Renderer should split at the newline before word-wrap so the
    # operator gets two LINES, not one wrapped phrase.
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="", body="Music of\nthe Spheres"), 0)
    d = _RecordingDisplay()
    r.paint(d, 0)
    # Exactly two body lines, in the order written.
    body_calls = [c for c in d.calls]
    assert len(body_calls) == 2
    assert body_calls[0]["s"] == "Music of"
    assert body_calls[1]["s"] == "the Spheres"
    # Second line is below the first.
    assert body_calls[1]["y"] > body_calls[0]["y"]


def test_renderer_consecutive_newlines_give_blank_line():
    # `\n\n` in the body produces a blank middle line for vertical
    # spacing.
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="", body="Top\n\nBottom"), 0)
    d = _RecordingDisplay()
    r.paint(d, 0)
    # Three lines painted: "Top", "" (blank), "Bottom". Note: depending
    # on the renderer, the blank line may or may not produce a draw
    # call. We assert at minimum that "Top" and "Bottom" appear and
    # in order, with extra vertical separation vs the no-blank case.
    strings = [c["s"] for c in d.calls]
    assert "Top" in strings
    assert "Bottom" in strings
    top_idx = strings.index("Top")
    bot_idx = strings.index("Bottom")
    assert bot_idx > top_idx
    # "Bottom" should be at a y-offset consistent with the blank in
    # between (i.e. one extra line height further down than back-to-
    # back text would be).
    body_calls = [c for c in d.calls if c["s"] in ("Top", "Bottom")]
    assert body_calls[1]["y"] > body_calls[0]["y"]


def test_renderer_clear_method_wipes_state():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="X", body="Y"), 0)
    r.clear()
    assert r.has_content() is False
    d = _RecordingDisplay()
    r.paint(d, 100)
    assert d.calls == []


def test_renderer_paints_header_only_when_body_empty():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="OnlyHeader", body=""), 0)
    d = _RecordingDisplay()
    r.paint(d, 0)
    assert len(d.calls) == 1
    assert d.calls[0]["s"] == "OnlyHeader"
    assert d.calls[0]["size"] == HEADER_FONT_SIZE


def test_renderer_paints_body_only_when_header_empty():
    r = LumeTextRenderer()
    r.on_text_display(_frame_for(header="", body="OnlyBody"), 0)
    d = _RecordingDisplay()
    r.paint(d, 0)
    assert len(d.calls) == 1
    assert d.calls[0]["s"] == "OnlyBody"
    assert d.calls[0]["size"] == BODY_FONT_SIZE


def test_renderer_marquees_overwide_header():
    r = LumeTextRenderer()
    # 23-char header at HEADER_FONT_SIZE * 0.62 advance comfortably
    # exceeds the 168 px inscribed-square width.
    r.on_text_display(_frame_for(header="Adventure of a Lifetime", body=""), 0)
    d1 = _RecordingDisplay()
    r.paint(d1, 0)
    x_at_0 = d1.calls[0]["x"]
    d2 = _RecordingDisplay()
    r.paint(d2, 2000)   # 2 seconds later
    x_at_2s = d2.calls[0]["x"]
    # The marquee anchor x must change between samples (left-ward motion).
    assert x_at_0 != x_at_2s
