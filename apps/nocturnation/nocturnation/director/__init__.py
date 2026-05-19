"""Director-mode runtime for the NocturNation Tildagon.

This package holds the Director-side machinery that turns a Show's
`ctx.render_fx()` calls into real output. Epic 6B builds it up
block by block:

  render_dispatch - B3: parse_target + RenderDispatcher (encode +
                    ESP-NOW broadcast + local perimeter/LCD loopback)
                    + DispatchResult.
  host            - B3: DirectorHost, the object ShowContext talks to
                    (dispatch_render_fx / now_ms / analyser_caps /
                    imu_caps). Expanded in later blocks as the IMU
                    adapter (B4) and FSM (B6) land.
  espnow_sender   - B3: thin hardware adapter that wraps an espnow
                    object into the send callable RenderDispatcher
                    needs. Not imported here (it touches the badge's
                    `espnow` module); B6 imports it when wiring app.py.
  imu             - B4: ImuAdapter, the accelerometer -> tap / motion
                    event adapter. Pure logic with the hardware read
                    injected; the default badge read (`imu.acc_read`)
                    is lazy-imported so host tests stay hardware-free.
  button_tap      - B5: ButtonTapSource, a button-as-tap fallback for
                    show development when the IMU isn't tuned. Emits
                    the same on_tap callback the ImuAdapter does.

The render fan-out mirrors the M5 firmware's
`dispatch_output_class_group`: one render_fx call goes to the wire
*and* to the Director's own surfaces, because the Director is its
own first Lume.
"""

from .render_dispatch import (
    RenderDispatcher,
    DispatchResult,
    parse_target,
)
from .host import DirectorHost
from .imu import (
    ImuAdapter,
    IMU_ADAPTER_CAPS,
    SENSITIVITY_LOW,
    SENSITIVITY_MEDIUM,
    SENSITIVITY_HIGH,
)
from .button_tap import ButtonTapSource, DEFAULT_TAP_STRENGTH

__all__ = [
    "RenderDispatcher",
    "DispatchResult",
    "parse_target",
    "DirectorHost",
    "ImuAdapter",
    "IMU_ADAPTER_CAPS",
    "SENSITIVITY_LOW",
    "SENSITIVITY_MEDIUM",
    "SENSITIVITY_HIGH",
    "ButtonTapSource",
    "DEFAULT_TAP_STRENGTH",
]
