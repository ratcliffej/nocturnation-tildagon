"""Show base class.

Cross-platform mirror of `nocturnation::shows::Show` on the M5
firmware (see include/shows/show.h in nocturnation-m5). Every hook
has a no-op default - a concrete Show overrides only what it needs.

Tildagon hosts fire the IMU hooks (`on_tap_detected`,
`on_motion_event`); the analyser hooks (`on_beat_detected` etc.)
fire when forwarded from the IMU adapter (tap-to-beat alias) or
from a future DMX-in source. The render hook fires at the host's
draw cadence when no overlay is open.

See docs/developing-shows.md in the nocturnation-m5 repo for the
authoring guide. The hook signatures here match that doc 1:1 so a
Show author can move between hosts with the same mental model.
"""

from ..plugins import Plugin, PluginKind


class Show(Plugin):
    """Abstract base for Director-side performance plug-ins.

    Concrete subclasses must implement at minimum `id()`,
    `display_name()`, and `context()`. The host (`DirectorMode`)
    fans events to the active Show by calling the `on_*` hooks
    below.
    """

    def kind(self):
        return PluginKind.SHOW

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def enter(self, ctx):
        """Called when the Show becomes the active Show."""
        pass

    def exit(self, ctx):
        """Called when the Show is being deactivated."""
        pass

    # -------------------------------------------------------------------
    # Analyser hooks
    #
    # Tildagon has no microphone, so these only fire when the host
    # routes a non-mic source into them. Most commonly:
    # `on_beat_detected` fires alongside `on_tap_detected` so
    # beat-driven Shows are host-agnostic.
    # -------------------------------------------------------------------

    def on_audio_frame(self, ctx, ev):
        pass

    def on_spectrum_frame(self, ctx, ev):
        pass

    def on_beat_detected(self, ctx, strength):
        pass

    def on_snare_detected(self, ctx, strength):
        pass

    def on_hihat_detected(self, ctx, strength):
        pass

    def on_music_descriptor(self, ctx, centroid, energy, density):
        pass

    def on_section_change(self, ctx, section):
        pass

    # -------------------------------------------------------------------
    # IMU hooks (Epic 6B - the Tildagon Director's primary input)
    #
    # strength: 0..255 normalised by the IMU adapter.
    # axis: 0=X, 1=Y, 2=Z, 3=magnitude.
    # magnitude: 0..255 normalised.
    # -------------------------------------------------------------------

    def on_tap_detected(self, ctx, strength):
        pass

    def on_motion_event(self, ctx, axis, magnitude):
        pass

    # -------------------------------------------------------------------
    # Input + render
    # -------------------------------------------------------------------

    def on_input_action(self, ctx, action):
        """`action` is one of the InputAction values reaching the Show:
        Confirm, Cycle, CyclePrev. Picker / Settings / Pause are
        intercepted upstream by DirectorMode."""
        pass

    def on_render(self, ctx):
        """Called at the host's draw cadence (~20 Hz) when no overlay
        is open. The Show owns the screen: it should clear + paint
        its full canvas each call."""
        pass

    # -------------------------------------------------------------------
    # Property change + tick
    # -------------------------------------------------------------------

    def on_property_changed(self, ctx, key):
        """Fired after the Settings overlay writes a new value to the
        property bag for `key`. Override to re-sync cached state."""
        pass

    def tick(self, ctx, now_ms):
        """Fired at `power().tick_hz` cadence. Defaults to never
        (event-driven Shows only). Set tick_hz > 0 in `power()` to
        enable."""
        pass

    # -------------------------------------------------------------------
    # Context accessor
    # -------------------------------------------------------------------
    #
    # On the Tildagon the DirectorController owns the active Show's
    # ShowContext and binds it here on activation, so a concrete Show
    # does not need to construct or return its own (unlike the M5
    # TU-static-singleton pattern). The bound context is also passed
    # as the first argument to every hook, so most Shows never call
    # `context()` directly - it exists for API parity with M5.

    _bound_context = None  # class default; bind_context sets a per-instance value

    def bind_context(self, ctx):
        """Framework hook: the DirectorController calls this when the
        Show becomes active, so `context()` returns a live context."""
        self._bound_context = ctx

    def context(self):
        """Return the framework-bound ShowContext, or None if the Show
        is not currently active."""
        return self._bound_context
