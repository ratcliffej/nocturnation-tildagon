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
from .frame import (
    Frame,
    parse_frame,
    FrameError,
    encode_light_pulse,
    encode_light_wash,
    encode_light_wash_end,
    encode_light_wash_pulse,
    encode_heartbeat,
    make_light_pulse_frame,
    make_light_wash_frame,
    make_light_wash_end_frame,
    make_light_wash_pulse_frame,
)
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
    "encode_light_pulse",
    "encode_light_wash",
    "encode_light_wash_end",
    "encode_light_wash_pulse",
    "encode_heartbeat",
    "make_light_pulse_frame",
    "make_light_wash_frame",
    "make_light_wash_end_frame",
    "make_light_wash_pulse_frame",
    "SourceId",
    "is_community_range",
    "is_performance_range",
]
