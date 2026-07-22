"""AUTO-GENERATED from Docs/protocol/constants.yaml. Do not edit by hand.

To regenerate, run tools/regen_constants.sh from the firmware repo
root. The accompanying test re-runs the generator and fails CI if
this file drifts from the SOT.

Epic 13: 0x09 was LIGHT_FX_RUN until the music-orchestrator move to
universe-driven FX (Epic 10). With on-Lume FX libraries retired (Lume
simplicity thesis), 0x09 is now TEXT_DISPLAY. The display family
0x0A..0x0C added.
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
    TEXT_DISPLAY     = 0x09
    BITMAP_HEADER    = 0x0A
    BITMAP_PLANE     = 0x0B
    CLEAR_SCREEN     = 0x0C
    EXTENSION        = 0xFF


# Fixed-size payloads only. Variable-length payloads (TEXT_DISPLAY,
# BITMAP_PLANE) are validated by their own range checks during parse.
PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT       : 9,
    MessageType.LIGHT_PULSE     : 9,
    MessageType.LIGHT_WASH      : 16,
    MessageType.LIGHT_WASH_END  : 3,
    MessageType.LIGHT_WASH_PULSE: 9,
    MessageType.BITMAP_HEADER   : 37,
    MessageType.CLEAR_SCREEN    : 3,
}
