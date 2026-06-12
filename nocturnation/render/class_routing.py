"""Class-to-surface routing tables.

Single source of truth for which target_class values drive each local
surface. Mirrors the receive-side routing the Lume uses (see
app.py _observe_frame) and the Director-side loopback routing in
render_dispatch. Previously these tuples lived in both files and
drifted-by-rebase risk was real.

Per Epic 5 Q1: the Tildagon advertises as MultiLedScreen (0x03) but
renders Light-class commands (0x01) on the perimeter too because the
LED ring is a wristband-analogue. Screen (0x02) targets the LCD only;
All (0x00) targets both surfaces.
"""

from ..protocol.constants import DeviceClass

PERIMETER_CLASSES = (
    DeviceClass.ALL,
    DeviceClass.LIGHT,
    DeviceClass.MULTI_LED_SCREEN,
)

LCD_CLASSES = (
    DeviceClass.ALL,
    DeviceClass.SCREEN,
    DeviceClass.MULTI_LED_SCREEN,
)
