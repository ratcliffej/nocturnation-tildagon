"""NocturNation ESP-NOW protocol implementation (Block 2).

See ``docs/manuals/protocol-manual.md`` in the nocturnation-m5 repo for the
normative spec this module implements against.
"""

from .constants import (
    PROTOCOL_VERSION,
    MAX_FRAME_SIZE,
    HEADER_SIZE,
    MessageType,
    DeviceClass,
    MusicEventType,
)
from .frame import Frame, parse_frame, FrameError

__all__ = [
    "PROTOCOL_VERSION",
    "MAX_FRAME_SIZE",
    "HEADER_SIZE",
    "MessageType",
    "DeviceClass",
    "MusicEventType",
    "Frame",
    "parse_frame",
    "FrameError",
]
