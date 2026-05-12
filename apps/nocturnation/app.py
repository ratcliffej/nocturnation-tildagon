"""NocturNation Tildagon receiver app.

Block 1 milestone: minimal Tildagon OS app that draws the brand-mark and
exits cleanly on CANCEL. ESP-NOW receive, render, and configuration land
in Blocks 2-6 per the Epic 5 plan.

Reference: https://tildagon.badge.emfcamp.org/tildagon-apps/development/
"""

import app
from events.input import Buttons, BUTTON_TYPES


class NocturNationApp(app.App):
    """Tildagon OS app entry point.

    The badge's app framework calls ``__init__`` once at start-up, then
    polls ``update(delta)`` and ``draw(ctx)`` at roughly 20 Hz. The CANCEL
    button minimises the app back to the badge's app menu.
    """

    def __init__(self) -> None:
        self.button_states = Buttons(self)

    def update(self, delta: float) -> None:
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()

    def draw(self, ctx) -> None:
        # Black background covering the 240x240 round LCD.
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()

        # Brand mark, centred. Block 4 replaces this with the live pulse
        # renderer; for Block 1 the screen just confirms the app loaded.
        ctx.rgb(1, 1, 1)
        ctx.font_size = 24
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.move_to(0, -10).text("NocturNation")
        ctx.font_size = 12
        ctx.move_to(0, 20).text("Block 1 - hello world")


__app_export__ = NocturNationApp
