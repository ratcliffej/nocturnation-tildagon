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
)
from .dedup import DedupRing
from .frame import Frame, parse_frame, FrameError
from .source_id import SourceId, is_community_range, is_performance_range

__all__ = [
    "PROTOCOL_VERSION",
    "MAX_FRAME_SIZE",
    "HEADER_SIZE",
    "MessageType",
    "DeviceClass",
    "DedupRing",
    "Frame",
    "parse_frame",
    "FrameError",
    "SourceId",
    "is_community_range",
    "is_performance_range",
]
