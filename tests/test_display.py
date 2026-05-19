"""CtxDisplay tests.

The Show's drawing surface wraps the badge draw ctx. Tested with a
recording fake ctx; colours are 0..255 in and 0..1 out (what ctx.rgb
wants).
"""

from nocturnation.render import CtxDisplay, SCREEN_W, SCREEN_H


class _FakeCtx:
    CENTER = "center"
    MIDDLE = "middle"

    def __init__(self):
        self.calls = []
        self.font_size = None
        self.text_align = None
        self.text_baseline = None

    def rgb(self, r, g, b):
        self.calls.append(("rgb", round(r, 3), round(g, 3), round(b, 3)))
        return self

    def rectangle(self, x, y, w, h):
        self.calls.append(("rectangle", x, y, w, h))
        return self

    def fill(self):
        self.calls.append(("fill",))
        return self

    def move_to(self, x, y):
        self.calls.append(("move_to", x, y))
        return self

    def text(self, s):
        self.calls.append(("text", s))
        return self


class TestNoCtx:
    def test_not_ready_without_ctx(self):
        d = CtxDisplay()
        assert d.ready is False

    def test_calls_are_safe_without_ctx(self):
        d = CtxDisplay()
        d.clear(255, 0, 0)        # must not crash
        d.fill_rect(0, 0, 10, 10, 1, 2, 3)
        d.text(0, 0, "hi")

    def test_ready_after_set_ctx(self):
        d = CtxDisplay()
        d.set_ctx(_FakeCtx())
        assert d.ready is True


class TestClear:
    def test_clear_fills_full_screen(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).clear(255, 0, 0)
        assert ("rgb", 1.0, 0.0, 0.0) in ctx.calls
        assert ("rectangle", -SCREEN_W // 2, -SCREEN_H // 2, SCREEN_W, SCREEN_H) in ctx.calls
        assert ("fill",) in ctx.calls

    def test_clear_default_black(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).clear()
        assert ("rgb", 0.0, 0.0, 0.0) in ctx.calls


class TestFillRect:
    def test_fill_rect_coords_and_colour(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).fill_rect(10, 20, 100, 14, 0, 255, 0)
        assert ("rgb", 0.0, 1.0, 0.0) in ctx.calls
        assert ("rectangle", 10, 20, 100, 14) in ctx.calls
        assert ("fill",) in ctx.calls


class TestText:
    def test_text_sets_size_and_draws(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).text(0, -50, "NocturNation", size=24, r=255, g=255, b=255)
        assert ctx.font_size == 24
        assert ("rgb", 1.0, 1.0, 1.0) in ctx.calls
        assert ("move_to", 0, -50) in ctx.calls
        assert ("text", "NocturNation") in ctx.calls

    def test_text_centres_by_default(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).text(0, 0, "x")
        assert ctx.text_align == _FakeCtx.CENTER
        assert ctx.text_baseline == _FakeCtx.MIDDLE

    def test_text_no_centre_when_disabled(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).text(0, 0, "x", center=False)
        assert ctx.text_align is None  # left untouched


class TestColourClamping:
    def test_over_255_clamps_to_one(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).clear(300, 300, 300)
        assert ("rgb", 1.0, 1.0, 1.0) in ctx.calls

    def test_negative_clamps_to_zero(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).fill_rect(0, 0, 1, 1, -5, -5, -5)
        assert ("rgb", 0.0, 0.0, 0.0) in ctx.calls

    def test_mid_value_scales(self):
        ctx = _FakeCtx()
        CtxDisplay(ctx).clear(128, 64, 0)
        # 128/255 = 0.502, 64/255 = 0.251
        assert ("rgb", 0.502, 0.251, 0.0) in ctx.calls
