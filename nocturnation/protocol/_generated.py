"""AUTO-GENERATED from Docs/protocol/constants.yaml. Do not edit by hand.

To regenerate, run tools/regen_constants.sh from the firmware repo
root. The accompanying test re-runs the generator and fails CI if
this file drifts from the SOT.
"""

MAGIC_0 = 0x4E
MAGIC_1 = 0x4E

PROTOCOL_VERSION = 0x02

HEADER_SIZE = 8


class MessageType:
    HEARTBEAT        = 0x00
    LIGHT_PULSE      = 0x03
    LIGHT_WASH       = 0x06
    LIGHT_WASH_END   = 0x07
    LIGHT_WASH_PULSE = 0x08
    LIGHT_FX_RUN     = 0x09
    EXTENSION        = 0xFF


PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT       : 9,
    MessageType.LIGHT_PULSE     : 9,
    MessageType.LIGHT_WASH      : 16,
    MessageType.LIGHT_WASH_END  : 3,
    MessageType.LIGHT_WASH_PULSE: 9,
    MessageType.LIGHT_FX_RUN    : 13,
}
