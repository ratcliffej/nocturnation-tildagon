# Synthetic testapp for the cartridge installer E2E. Overrides
# update() to return True so draw() fires (base App.update returns
# False and would suppress render). Handles CANCEL for clean exit.
import app
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus
from system.scheduler.events import RequestForegroundPushEvent


class TestApp(app.App):
    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self._foregrounded = False

    def update(self, delta):
        if not self._foregrounded:
            eventbus.emit(RequestForegroundPushEvent(self))
            self._foregrounded = True
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()
        return True

    def draw(self, ctx):
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 22
        ctx.move_to(0, -10).text("Cartridge")
        ctx.move_to(0, 15).text("test app")
        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6)
        ctx.move_to(0, 70).text("F to exit")


__app_export__ = TestApp
