"""DirectorHost - the object a ShowContext talks to.

ShowContext (nocturnation.shows.show_context) forwards a Show's
service calls to its `host`:

  dispatch_render_fx(target, ev) -> bool
  now_ms()                       -> int
  analyser_caps()                -> CapabilityMask
  imu_caps()                     -> CapabilityMask

DirectorHost satisfies that contract. In B3 it wraps a
RenderDispatcher and a clock; analyser_caps is always empty (the
Tildagon has no microphone) and imu_caps is whatever the IMU adapter
declares (empty until B4 lights it up). B6 expands this object into
the full DirectorMode runtime (active-show selection, overlays).
"""

from ..hal import CapabilityMask


class DirectorHost:
    """Host surface backing a ShowContext on the Tildagon Director.

    Construct with:
      dispatcher    - a RenderDispatcher.
      clock         - callable() -> int milliseconds. Defaults to a
                      zero clock (tests / pre-wiring); B6 injects
                      `time.ticks_ms`.
      imu_caps      - a CapabilityMask of IMU sub-caps the host
                      provides (B4 passes IMU_TAP / IMU_MOTION).
                      Defaults to empty.
    """

    __slots__ = ("_dispatcher", "_clock", "_imu_caps")

    def __init__(self, dispatcher, clock=None, imu_caps=None):
        self._dispatcher = dispatcher
        self._clock = clock if clock is not None else (lambda: 0)
        self._imu_caps = imu_caps if imu_caps is not None else CapabilityMask()

    # -- ShowContext host contract ----------------------------------------

    def dispatch_render_fx(self, target, ev):
        """Fan a render event out via the dispatcher. Returns True if
        the render did anything (broadcast or local loopback)."""
        result = self._dispatcher.dispatch(target, ev, self.now_ms())
        return bool(result)

    def now_ms(self):
        return self._clock()

    def analyser_caps(self):
        # Tildagon has no on-board analyser; always empty.
        return CapabilityMask()

    def imu_caps(self):
        return self._imu_caps

    # -- Accessors --------------------------------------------------------

    @property
    def dispatcher(self):
        return self._dispatcher

    def set_imu_caps(self, mask):
        self._imu_caps = mask
