"""ShowContext - services surface exposed to the active Show.

Mirrors `nocturnation::shows::ShowContext` on the M5 firmware (see
include/shows/show_context.h in nocturnation-m5). Thin forwarding
surface: Shows never reach the host's perimeter / LCD renderers or
the ESP-NOW driver directly - everything flows through here so the
Show stays portable across hosts.

In B2 the `render_fx` and `analyser_caps` paths are deliberate
stubs: B3 wires `render_fx` to the actual ESP-NOW broadcast +
local LED + local LCD fan-out, and B4 starts populating
`imu_caps` from the IMU adapter. The surface is final from B2
onwards so concrete Shows written against it remain stable.
"""

from ..hal import CapabilityMask


class ShowContext:
    """Per-Show services surface.

    Constructed by the concrete Show's factory:

        ctx = ShowContext(show, property_bag, host=...)

    The `host` is whatever DirectorMode hands the framework - in
    B6 onwards a `DirectorHost` object carrying the render
    fan-out, analyser cap mask, IMU cap mask, and tick clock. In
    B2 the host is None and the methods that depend on it
    short-circuit safely so the framework can be exercised in
    host tests without standing the whole Director up.
    """

    __slots__ = (
        "_show",
        "_bag",
        "_host",
        "_paused",
        "_entered_at_ms",
        "_display",
    )

    def __init__(self, show, property_bag, host=None, display=None):
        self._show          = show
        self._bag           = property_bag
        self._host          = host
        self._paused        = False
        self._entered_at_ms = 0
        # Drawing surface for on_render(). The DirectorController/app
        # owns a CtxDisplay and rebinds the live ctx each frame; here
        # it's None until wired (host tests don't draw).
        self._display       = display

    # -- Output -----------------------------------------------------------

    def render_fx(self, target, ev):
        """Fan out a render event with `target` = `"<class>:<group>"`.

        Returns True if the dispatch was accepted, False if no host
        is wired (e.g. in unit tests). B3 wires the real dispatch.
        """
        if self._host is None:
            return False
        return self._host.dispatch_render_fx(target, ev)

    def render_wash(self, target, ev):
        """Fan out a LIGHT_WASH event. `ev` is an RgbWash. Cross-platform
        mirror of `ShowContext::render_wash` on M5 (Epic 6C)."""
        if self._host is None or not hasattr(self._host, "dispatch_render_wash"):
            return False
        return self._host.dispatch_render_wash(target, ev)

    def render_wash_end(self, target, release_time):
        """Cancel an active wash on `target` with `release_time` (in
        100 ms units) overriding the wash's own release. Cross-platform
        mirror of `ShowContext::render_wash_end` on M5."""
        if self._host is None or not hasattr(self._host, "dispatch_render_wash_end"):
            return False
        return self._host.dispatch_render_wash_end(target, release_time)

    def render_wash_pulse(self, target, ev):
        """Fan out a LIGHT_WASH_PULSE event. `ev` is an RgbPulse (same
        payload as LIGHT_PULSE; routed only to washing Lumes on the
        receive side). Cross-platform mirror of
        `ShowContext::render_wash_pulse` on M5."""
        if self._host is None or not hasattr(self._host, "dispatch_render_wash_pulse"):
            return False
        return self._host.dispatch_render_wash_pulse(target, ev)

    # -- Property bag -----------------------------------------------------

    def get_property(self, key):
        return self._bag.get(key)

    def set_property(self, key, value):
        """Set and persist `value` for `key`; notify the Show. Returns
        the value actually stored (after schema clamping)."""
        stored = self._bag.set(key, value)
        self._show.on_property_changed(self, key)
        return stored

    # -- Capability query -------------------------------------------------

    def analyser_caps(self):
        """CapabilityMask of analyser sub-caps the host has. Tildagon
        returns an empty mask (no on-board analyser). The host on
        M5 returns the lit subset of AnalyserBeatDetection etc.

        Shows use this to gate optional codepaths (e.g. skip the
        snare-driven branch if AnalyserMultiBandOnset isn't lit)."""
        if self._host is None or not hasattr(self._host, "analyser_caps"):
            return CapabilityMask()
        return self._host.analyser_caps()

    def imu_caps(self):
        """CapabilityMask of IMU sub-caps the host has (Epic 6B).
        Tildagon returns IMU_TAP + IMU_MOTION once B4 lights up the
        adapter. M5 returns empty until M5 grows an IMU backend."""
        if self._host is None or not hasattr(self._host, "imu_caps"):
            return CapabilityMask()
        return self._host.imu_caps()

    # -- Display ----------------------------------------------------------

    def display(self):
        """Return the Show's drawing surface (a CtxDisplay) for use in
        on_render(). Returns None if no display is wired (host tests).
        On the Tildagon the app rebinds the live draw ctx onto this
        surface each frame before calling on_render."""
        return self._display

    def set_display(self, display):
        self._display = display

    # -- Framework-managed state ------------------------------------------

    def paused(self):
        return self._paused

    def set_paused(self, p):
        self._paused = bool(p)

    # -- Time helpers -----------------------------------------------------

    def now_ms(self):
        if self._host is None or not hasattr(self._host, "now_ms"):
            return 0
        return self._host.now_ms()

    def since_enter_ms(self):
        return max(0, self.now_ms() - self._entered_at_ms)

    def mark_entered(self, now_ms):
        self._entered_at_ms = now_ms

    # -- Identity ---------------------------------------------------------

    def show(self):
        return self._show

    def property_bag(self):
        return self._bag
