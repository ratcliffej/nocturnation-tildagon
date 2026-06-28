"""LumeTextRenderer - Lume-side rendering of TEXT_DISPLAY frames on
the Tildagon's round LCD (Epic 13 B3).

Mirrors the StickC's LumeTextBinding in behaviour but adapted for the
240x240 round display:

  - Header on top, body below. Both centred horizontally.
  - Largest inscribed square of a 240 px circle is ~170x170 px;
    text is laid out inside that square so corners aren't clipped.
  - Header uses a larger font tier (HEADER_FONT_SIZE); body uses a
    smaller one. Both colours come from the wire frame's r/g/b.
  - TTL of 0 = sticky (held until CLEAR_SCREEN or a superseding
    TEXT_DISPLAY); non-zero TTL auto-clears after the elapsed window.
  - Header scrolls left when it exceeds the inscribed-square width.
    Body wraps to multiple lines and clips when overflow exceeds
    the available rows (vertical scroll is Phase 2 polish).
  - Repaint is layered ON TOP of the wash background by the app's
    draw() - the renderer never clears the screen itself.

The text-width approximation is character-count-based (chars *
~0.6 * font_size). Good enough for the marquee trigger threshold;
the ctx library doesn't expose a fast measure API on the badge.
"""


# Round LCD physical dimensions (mirror of CtxDisplay's SCREEN_W / H).
SCREEN_W = 240
SCREEN_H = 240

# Largest inscribed square of a 240 px circle has side 240/sqrt(2) ~=
# 169.7 px. Round half-side down to 84 so the renderer's right edge
# stays just inside the circle even after the font's glyph descent.
INSCRIBED_HALF = 84

# Font tiers. Sized to match the StickC pair (header tier ~24 px
# native, body tier ~16-18 px) so the same content reads as the same
# "tier" regardless of which Lume an operator looks at.
HEADER_FONT_SIZE = 22
BODY_FONT_SIZE   = 18

# Vertical layout. Header band anchors near the top of the inscribed
# square; body fills the rest. Values are origin-centred coordinates
# (the badge ctx is centre-origin), so y increases downward and the
# inscribed square spans roughly y = -INSCRIBED_HALF .. +INSCRIBED_HALF.
HEADER_TOP_Y       = -INSCRIBED_HALF + HEADER_FONT_SIZE // 2 + 2  # baseline-centred top row
HEADER_BAND_HEIGHT = HEADER_FONT_SIZE + 8
BODY_TOP_Y         = HEADER_TOP_Y + HEADER_BAND_HEIGHT
BODY_LINE_STRIDE   = BODY_FONT_SIZE + 4
MAX_BODY_LINES     = 4   # ~ (INSCRIBED_HALF - body_top) / stride, fits 4 lines

# Approximate per-character advance for the badge's default font.
# A roman glyph is roughly 0.55 * font_size wide on average; bias up
# to 0.62 to compensate for the wider letters that drive the marquee
# trigger ("M", "W") and to keep the wrap a touch conservative.
_CHAR_ADVANCE_RATIO = 0.62


def _approx_text_width(text, font_size):
    """Cheap pixel-width estimate for a string at a given font size."""
    if not text:
        return 0
    return int(len(text) * font_size * _CHAR_ADVANCE_RATIO)


# Scroll cadence for an over-width header. ~40 px/sec reads as steady
# but unhurried, matches the StickC marquee speed (2 px per 50 ms tick
# = 40 px/s). Single-copy with wrap: scroll_offset = 0 -> text left-
# edge aligned with the inscribed-square's left edge; positive offset
# moves text left; when offset exceeds text width + gap, wrap back to
# -inscribed_width so the text re-enters from the right.
SCROLL_PX_PER_SECOND = 40
MARQUEE_END_GAP_PX   = 40   # blank gap before the wrap-around re-entry


def _ticks_diff(a, b):
    """Wrap-safe difference for time.ticks_ms() returns. On CPython
    (host tests) ticks_ms() returns a plain int; on MicroPython it
    wraps every 2^30 ms. The MicroPython runtime provides
    time.ticks_diff for this; we call through it when available."""
    try:
        import time as _t
        return _t.ticks_diff(a, b)
    except (ImportError, AttributeError):
        return a - b


class LumeTextRenderer:
    """Stateful renderer for the TEXT_DISPLAY surface.

    Lifecycle, mirroring the StickC binding:
      - on_text_display(frame, now_ms): apply a new TEXT_DISPLAY,
        reset scroll, mark visible.
      - on_clear_screen(frame, now_ms): clear the text surface if the
        frame's clear_text flag is set. Bitmap clear is a no-op here -
        that's the bitmap renderer's job.
      - paint(display, now_ms): draw the current state to a CtxDisplay.
        Caller invokes from the app's draw() hook every frame.
      - has_content() / current_colour(): query helpers.

    The renderer owns no time source - the caller passes now_ms to
    every state-mutating method so the renderer is fully deterministic
    under host tests.
    """

    __slots__ = (
        "_header", "_body",
        "_r", "_g", "_b",
        "_visible",
        "_ttl_ms",
        "_content_started_ms",
        "_scroll_anchor_ms",
        "_header_scrolls",
        "_header_pixel_width",
    )

    def __init__(self):
        self._header = ""
        self._body = ""
        self._r = 255
        self._g = 255
        self._b = 255
        self._visible = False
        self._ttl_ms = 0
        self._content_started_ms = 0
        self._scroll_anchor_ms = 0
        self._header_scrolls = False
        self._header_pixel_width = 0

    # -- State mutation ---------------------------------------------

    def on_text_display(self, frame, now_ms):
        """Apply a parsed TEXT_DISPLAY frame.

        The frame must already have been through parse_frame so the
        ``header``, ``body``, ``text_r/g/b``, and ``ttl_ms`` attrs are
        populated. (Use ``Frame()`` constructed via parse_frame, or
        an equivalent fake in tests.)
        """
        self._header = frame.header or ""
        self._body = frame.body or ""
        self._r = frame.text_r if frame.text_r is not None else 255
        self._g = frame.text_g if frame.text_g is not None else 255
        self._b = frame.text_b if frame.text_b is not None else 255
        self._ttl_ms = frame.ttl_ms or 0
        self._visible = bool(self._header) or bool(self._body)
        self._content_started_ms = now_ms
        self._scroll_anchor_ms = now_ms
        self._header_pixel_width = _approx_text_width(self._header, HEADER_FONT_SIZE)
        self._header_scrolls = self._header_pixel_width > (2 * INSCRIBED_HALF)

    def on_clear_screen(self, frame, now_ms):
        """Apply a parsed CLEAR_SCREEN frame. Only acts when the frame's
        ``clear_text`` flag is set; the bitmap clear is the bitmap
        renderer's concern."""
        if not frame.clear_text:
            return
        self.clear()

    def clear(self):
        """Drop any held content unconditionally. Used by lifecycle
        transitions (mode enter/exit, settings toggle) where the
        previous Lume session's text should not bleed through."""
        self._header = ""
        self._body = ""
        self._visible = False
        self._header_scrolls = False
        self._header_pixel_width = 0

    # -- Query ------------------------------------------------------

    def has_content(self):
        return self._visible

    def current_colour(self):
        """Return (r, g, b) of the current text content. Useful only
        when has_content() is True."""
        return (self._r, self._g, self._b)

    # -- Rendering --------------------------------------------------

    def paint(self, display, now_ms):
        """Draw the current state onto a CtxDisplay.

        Caller invokes this from the app's draw() hook each frame.
        The renderer never clears the screen - the caller paints the
        wash background first, then calls this to layer text over it.
        """
        if not self._visible:
            return

        # TTL expiry. Sticky (ttl_ms_ == 0) skips the check entirely.
        if self._ttl_ms > 0:
            elapsed = _ticks_diff(now_ms, self._content_started_ms)
            if elapsed >= self._ttl_ms:
                self._visible = False
                self._header = ""
                self._body = ""
                self._header_scrolls = False
                return

        if self._header:
            self._paint_header(display, now_ms)
        if self._body:
            self._paint_body(display)

    def _paint_header(self, display, now_ms):
        if not self._header_scrolls:
            display.text(
                0, HEADER_TOP_Y, self._header,
                size=HEADER_FONT_SIZE, r=self._r, g=self._g, b=self._b,
                center=True,
            )
            return

        # Marquee mode: scroll_offset goes 0 -> text_w + gap, then wraps
        # back to -inscribed_width so the text re-enters from the right.
        # Single-copy on screen at a time; the trailing gap prevents
        # the head colliding with the tail mid-wrap on long titles.
        elapsed_ms = _ticks_diff(now_ms, self._scroll_anchor_ms)
        if elapsed_ms < 0:
            elapsed_ms = 0
        scroll_offset_px = (elapsed_ms * SCROLL_PX_PER_SECOND) // 1000
        wrap_at = self._header_pixel_width + MARQUEE_END_GAP_PX + (2 * INSCRIBED_HALF)
        scroll_offset_px = scroll_offset_px % wrap_at
        # Anchor coordinate: text left edge at +INSCRIBED_HALF when
        # scroll_offset_px == 0 -> text fully off-screen to the right;
        # scroll_offset_px advancing slides text leftward across the
        # screen. We pass an x-anchor for centred draw; the badge ctx
        # centres text on the given point, so we set the anchor to
        # the text's intended centre at this offset.
        text_centre_x = INSCRIBED_HALF + (self._header_pixel_width // 2) - scroll_offset_px
        display.text(
            text_centre_x, HEADER_TOP_Y, self._header,
            size=HEADER_FONT_SIZE, r=self._r, g=self._g, b=self._b,
            center=True,
        )

    def _paint_body(self, display):
        lines = self._wrap_body_lines(self._body)
        if not lines:
            return
        n = min(len(lines), MAX_BODY_LINES)
        # Anchor: body sits below the header when one is present;
        # when the header is empty the body block is centred vertically
        # on the screen (origin row) so a single lyric doesn't appear
        # awkwardly stuck to the top. The block's total height is
        # n * stride - one gap (the last line has no trailing gap).
        block_height = n * BODY_LINE_STRIDE - (BODY_LINE_STRIDE - BODY_FONT_SIZE)
        if self._header:
            top_y = BODY_TOP_Y
        else:
            top_y = -block_height // 2 + BODY_FONT_SIZE // 2
        for i in range(n):
            y = top_y + i * BODY_LINE_STRIDE
            display.text(
                0, y, lines[i],
                size=BODY_FONT_SIZE, r=self._r, g=self._g, b=self._b,
                center=True,
            )

    def _wrap_body_lines(self, body):
        """Word-wrap body text into lines that fit the inscribed-
        square width. Words longer than a single line get chopped at
        the line boundary.

        Newline characters in the body (introduced via `\\n` escape
        in the cue file) are treated as forced line breaks: each
        newline-separated segment is word-wrapped independently.
        Empty segments (consecutive newlines) become blank lines."""
        if not body:
            return []
        max_w = 2 * INSCRIBED_HALF
        # Char advance at the body font; rough char-count budget.
        char_w = max(1, int(BODY_FONT_SIZE * _CHAR_ADVANCE_RATIO))
        max_chars = max(1, max_w // char_w)
        lines = []
        # Split on forced-newline boundaries first, then word-wrap
        # each segment. Empty segments produce blank lines so the
        # author can use `\n\n` to create vertical space.
        for segment in body.split("\n"):
            if not segment.strip():
                lines.append("")
                if len(lines) >= MAX_BODY_LINES:
                    return lines[:MAX_BODY_LINES]
                continue
            wrapped = self._wrap_segment(segment, max_chars)
            for line in wrapped:
                lines.append(line)
                if len(lines) >= MAX_BODY_LINES:
                    return lines[:MAX_BODY_LINES]
        return lines

    def _wrap_segment(self, segment, max_chars):
        """Word-wrap a single segment of body text (no newlines).
        Returns a list of lines, each within max_chars."""
        lines = []
        words = segment.split()
        if not words:
            return []
        current = ""
        for w in words:
            # Chop overlong single words.
            while len(w) > max_chars:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(w[:max_chars])
                w = w[max_chars:]
                if len(lines) >= MAX_BODY_LINES:
                    return lines[:MAX_BODY_LINES]
            candidate = (current + " " + w) if current else w
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = w
                if len(lines) >= MAX_BODY_LINES:
                    return lines[:MAX_BODY_LINES]
        if current:
            lines.append(current)
        return lines[:MAX_BODY_LINES]
