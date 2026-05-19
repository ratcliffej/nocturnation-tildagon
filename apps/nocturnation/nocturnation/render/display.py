"""CtxDisplay - a Show's drawing surface on the Tildagon LCD.

A Show owns the Director screen via `on_render(ctx)`. On the M5
firmware a Show draws by calling `DAL::fire_display_*`; on the
Tildagon it draws through `ctx.display()`, which returns one of these
adapters wrapping the badge's draw context.

The badge `ctx` is only valid during `draw()`, so the app rebinds it
on each frame with `set_ctx()` before calling the Show's
`on_render`. The Show then calls `display.clear()`, `display.text()`,
`display.fill_rect()`.

Coordinate system mirrors the badge: origin at screen centre, x/y in
-120..120, screen 240x240 (round). Colours are 0..255 ints (matching
RgbPulse and the LIGHT_COMMAND wire), converted to the 0..1 floats
`ctx.rgb` expects.

Kept as pure logic with the ctx injected so the contract is host-
testable with a recording fake ctx.
"""

# Round LCD: 240x240, origin-centred. The full-screen clear rectangle
# spans the bounding box; corners fall outside the circle and are
# simply not visible.
SCREEN_W = 240
SCREEN_H = 240
_HALF_W = SCREEN_W // 2
_HALF_H = SCREEN_H // 2


def _to01(v):
    """Clamp a 0..255 channel to a 0..1 float for ctx.rgb."""
    if v < 0:
        v = 0
    elif v > 255:
        v = 255
    return v / 255.0


class CtxDisplay:
    """Drawing surface wrapping a badge draw context.

    Construct once and rebind the live ctx each frame:

        display = CtxDisplay()
        ...
        display.set_ctx(ctx)         # in app.draw()
        active_show.on_render(show_ctx)

    A Show never constructs this - it reaches it via `ctx.display()`.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx=None):
        self._ctx = ctx

    def set_ctx(self, ctx):
        """Rebind the live draw context for this frame."""
        self._ctx = ctx

    @property
    def ready(self):
        return self._ctx is not None

    def clear(self, r=0, g=0, b=0):
        """Fill the whole screen with one colour (default black)."""
        if self._ctx is None:
            return
        self._ctx.rgb(_to01(r), _to01(g), _to01(b)).rectangle(
            -_HALF_W, -_HALF_H, SCREEN_W, SCREEN_H
        ).fill()

    def fill_rect(self, x, y, w, h, r, g, b):
        """Fill a rectangle. Coordinates are origin-centred."""
        if self._ctx is None:
            return
        self._ctx.rgb(_to01(r), _to01(g), _to01(b)).rectangle(x, y, w, h).fill()

    def text(self, x, y, s, size=18, r=255, g=255, b=255, center=True):
        """Draw text at (x, y). Centred on the point by default (the
        natural choice on a round screen). Set center=False for
        left-baseline text."""
        if self._ctx is None:
            return
        ctx = self._ctx
        ctx.font_size = size
        if center:
            ctx.text_align = ctx.CENTER
            ctx.text_baseline = ctx.MIDDLE
        ctx.rgb(_to01(r), _to01(g), _to01(b)).move_to(x, y).text(s)
