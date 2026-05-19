"""InputAction - the semantic input vocabulary reaching a Show.

Cross-platform mirror of `hal::InputAction` on the M5 firmware (see
docs/developing-shows.md "Button handling"). The Director host maps
physical buttons to these semantic actions; some are intercepted by
the host and never reach the Show.

  PICKER     - open the Show picker          (host intercepts)
  SETTINGS   - open the per-Show settings    (host intercepts)
  PAUSE      - toggle ctx.paused()           (host toggles)
  CONFIRM    - reaches the Show
  CYCLE      - reaches the Show
  CYCLE_PREV - reaches the Show

A Show overrides `on_input_action(ctx, action)` and compares `action`
against these constants, e.g.

    from nocturnation.shows import InputAction

    def on_input_action(self, ctx, action):
        if action == InputAction.CYCLE:
            ...

Implemented as a class-as-namespace of ints (no enum module on
MicroPython), matching the Capability / DeviceClass pattern elsewhere.
"""


class InputAction:
    PICKER = 0
    SETTINGS = 1
    PAUSE = 2
    CONFIRM = 3
    CYCLE = 4
    CYCLE_PREV = 5
