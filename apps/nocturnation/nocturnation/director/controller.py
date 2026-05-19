"""DirectorController - orchestration core for Director mode.

Holds exactly one active Show and wires the input sources (IMU
adapter, button-tap fallback) to it, manages Show selection +
persistence, and exposes the input-action routing the app's button
handler drives.

This is the testable heart of Director mode. The hardware-facing
shell - the app.py mode switch, the picker / settings overlays, and
the Show-owns-screen draw integration - lives in B6b and calls into
this controller.

Event flow:

    IMU tap  ─┐
              ├─> _on_tap(strength) ─> active.on_tap_detected(ctx, s)
    button   ─┘                        active.on_beat_detected(ctx, s)

    IMU motion ─> _on_motion(axis, mag) ─> active.on_motion_event(...)

    button -> InputAction -> on_input_action():
        PAUSE              -> toggle ctx.paused()        (handled here)
        PICKER / SETTINGS  -> return a result code       (app opens UI)
        CONFIRM/CYCLE/...  -> active.on_input_action(...)  (reaches Show)

Tap fires *both* on_tap_detected and on_beat_detected so a beat-driven
Show is host-agnostic: the same hook fires on M5 from the mic-driven
beat detector and on the Tildagon from a tap.
"""

from ..shows import ShowContext, InputAction
from ..plugins import PropertyBag


# Results of on_input_action: the app acts on the OPEN_* codes to
# show an overlay; HANDLED means the controller dealt with it.
RESULT_HANDLED = 0
RESULT_OPEN_PICKER = 1
RESULT_OPEN_SETTINGS = 2

# Per-Show property key the IMU adapter's sensitivity tracks. A Show
# that declares it gets per-Show IMU tuning; a Show without it leaves
# the adapter at its current sensitivity.
_SENSITIVITY_KEY = "sensitivity"


class DirectorController:
    """Wire input sources to one active Show; manage Show selection.

    Construct with:
      host        - a DirectorHost (the ShowContext service surface).
      registry    - a ShowRegistry of discovered Shows.
      imu         - an ImuAdapter, or None (button-only / tests).
      button_tap  - a ButtonTapSource, or None.
      initial_show_id - the persisted active-show id to restore on
                    enter(); "" / unknown falls back to the first
                    registered Show.
      on_active_show_changed - callable(show_id) invoked when the
                    operator selects a Show, for persistence.
      property_bag_path - override the PropertyBag JSON path (tests).
    """

    __slots__ = (
        "_host",
        "_registry",
        "_imu",
        "_button_tap",
        "_initial_show_id",
        "_on_active_show_changed",
        "_bag_path",
        "_active_show",
        "_active_ctx",
        "_entered",
        "_last_tick_ms",
    )

    def __init__(self, host, registry, imu=None, button_tap=None,
                 initial_show_id="", on_active_show_changed=None,
                 property_bag_path=None):
        self._host = host
        self._registry = registry
        self._imu = imu
        self._button_tap = button_tap
        self._initial_show_id = initial_show_id
        self._on_active_show_changed = on_active_show_changed
        self._bag_path = property_bag_path
        self._active_show = None
        self._active_ctx = None
        self._entered = False
        self._last_tick_ms = None

        # Wire input callbacks once. They reference the *current* active
        # Show via self._active_show at call time, so a Show change
        # needs no re-wiring.
        if self._imu is not None:
            self._imu.set_callbacks(on_tap=self._on_tap, on_motion=self._on_motion)
        if self._button_tap is not None:
            self._button_tap.set_callback(self._on_tap)

    # -- Lifecycle --------------------------------------------------------

    def enter(self):
        """Enter Director mode: reset inputs and activate the restored
        (or default) Show. Idempotent enough to call on every mode
        entry."""
        self._entered = True
        if self._imu is not None:
            self._imu.reset()
        if self._button_tap is not None:
            self._button_tap.reset()

        show = None
        if self._initial_show_id:
            show = self._registry.find(self._initial_show_id)
        if show is None:
            shows = self._registry.all()
            show = shows[0] if shows else None
        if show is not None:
            # Don't persist the restore/default: it's already the stored
            # value (or there's nothing to record yet).
            self._activate(show, persist=False)

    def exit(self):
        """Leave Director mode: exit the active Show. The Show reference
        is kept so a later enter() can re-resolve cleanly."""
        if self._active_show is not None and self._active_ctx is not None:
            self._active_show.exit(self._active_ctx)
        self._entered = False

    # -- Show selection ---------------------------------------------------

    def select_show(self, show_id):
        """Activate the Show with `show_id`. Returns True if found and
        activated (or already active), False if no such Show."""
        show = self._registry.find(show_id)
        if show is None:
            return False
        if show is self._active_show:
            return True
        self._activate(show, persist=True)
        return True

    def _activate(self, show, persist):
        if self._active_show is not None and self._active_ctx is not None:
            self._active_show.exit(self._active_ctx)

        if self._bag_path is None:
            bag = PropertyBag(show)
        else:
            bag = PropertyBag(show, path=self._bag_path)
        ctx = ShowContext(show, bag, host=self._host)

        self._active_show = show
        self._active_ctx = ctx
        self._last_tick_ms = None
        show.bind_context(ctx)
        ctx.mark_entered(self._host.now_ms())
        self._apply_sensitivity(ctx)
        show.enter(ctx)

        if persist and self._on_active_show_changed is not None:
            self._on_active_show_changed(show.id())

    def _apply_sensitivity(self, ctx):
        """Push the active Show's `sensitivity` property to the IMU
        adapter, if both exist. A Show without the property leaves the
        adapter's current sensitivity untouched."""
        if self._imu is None:
            return
        try:
            level = ctx.get_property(_SENSITIVITY_KEY)
        except KeyError:
            return
        self._imu.set_sensitivity(level)

    # -- Input polling + routing ------------------------------------------

    def poll_inputs(self, now_ms, button_pressed=None):
        """Poll the input sources once. `button_pressed` is the current
        physical button state for the button-tap fallback (None skips
        it). The IMU adapter reads its own injected accelerometer."""
        if self._imu is not None:
            self._imu.poll(now_ms)
        if self._button_tap is not None and button_pressed is not None:
            self._button_tap.poll(button_pressed, now_ms)

    def poll_button(self, pressed, now_ms):
        """Drive only the button-tap fallback. The app polls the IMU
        from its always-running background loop and the button from its
        foreground update(), so they're driven separately."""
        if self._button_tap is not None:
            self._button_tap.poll(pressed, now_ms)

    def apply_sensitivity(self):
        """Re-push the active Show's sensitivity property to the IMU
        adapter. Called after the settings overlay changes a value so
        a sensitivity edit takes effect without re-activating the
        Show."""
        if self._active_ctx is not None:
            self._apply_sensitivity(self._active_ctx)

    def _on_tap(self, strength):
        if self._active_show is None:
            return
        # Fire both: on_tap_detected (IMU-native) and on_beat_detected
        # (the host-agnostic beat hook a Show can share with M5).
        self._active_show.on_tap_detected(self._active_ctx, strength)
        self._active_show.on_beat_detected(self._active_ctx, strength)

    def _on_motion(self, axis, magnitude):
        if self._active_show is None:
            return
        self._active_show.on_motion_event(self._active_ctx, axis, magnitude)

    def on_input_action(self, action):
        """Route a semantic InputAction. Returns a RESULT_* code: the
        app opens the picker / settings overlay on the OPEN_* codes."""
        if action == InputAction.PAUSE:
            if self._active_ctx is not None:
                self._active_ctx.set_paused(not self._active_ctx.paused())
            return RESULT_HANDLED
        if action == InputAction.PICKER:
            return RESULT_OPEN_PICKER
        if action == InputAction.SETTINGS:
            return RESULT_OPEN_SETTINGS
        # CONFIRM / CYCLE / CYCLE_PREV reach the Show.
        if self._active_show is not None:
            self._active_show.on_input_action(self._active_ctx, action)
        return RESULT_HANDLED

    # -- Tick -------------------------------------------------------------

    def tick(self, now_ms):
        """Drive the active Show's tick() at its declared tick_hz. A
        Show with tick_hz == 0 (the default) is purely event-driven and
        never ticked."""
        if self._active_show is None:
            return
        hz = self._active_show.power().tick_hz
        if hz <= 0:
            return
        interval = 1000 // hz
        if self._last_tick_ms is None or (now_ms - self._last_tick_ms) >= interval:
            self._last_tick_ms = now_ms
            self._active_show.tick(self._active_ctx, now_ms)

    # -- Accessors --------------------------------------------------------

    @property
    def active_show(self):
        return self._active_show

    @property
    def active_context(self):
        return self._active_ctx

    @property
    def registry(self):
        return self._registry

    def active_show_id(self):
        return self._active_show.id() if self._active_show is not None else None

    def show_ids(self):
        """Registration-ordered Show ids, for the picker UI (B6b)."""
        return [s.id() for s in self._registry]
