"""Protocol constants. Mirror of the values defined in the protocol manual.

Implemented as plain integer constants rather than IntEnum so the module runs
unchanged under MicroPython, which doesn't ship enum in its trimmed stdlib.

The header / magic / version / message-type / payload-length constants are
generated from Docs/protocol/constants.yaml via Docs/tools/gen_protocol_constants.py
and re-exported here unchanged. To regenerate, run tools/regen_constants.sh.
The DeviceClass / Time / Chance tables and the MAX_FRAME_SIZE derivation
remain manually authored (they have language-specific shape that is not
worth templating).
"""

from ._generated import (
    MAGIC_0,
    MAGIC_1,
    PROTOCOL_VERSION,
    HEADER_SIZE,
    MessageType,
    PAYLOAD_LENGTHS,
)

# ESP-NOW supports up to 250-byte payloads; NocturNation caps frames at 32
# (the largest active payload is LIGHT_WASH at 16 bytes, so 32 leaves
# room for future message types without burning radio time on padding).
MAX_FRAME_SIZE = 32
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE


class DeviceClass:
    ALL = 0x00              # addressing wildcard; never advertised by a receiver
    LIGHT = 0x01            # PixMob bracelets, LED wristbands
    SCREEN = 0x02           # Stick LCD
    MULTI_LED_SCREEN = 0x03  # Tildagon (this device)
    # 0x04..0xFF reserved


# PixMob Time enum values; the LIGHT_PULSE attack/sustain/release bytes
# index into this table. Names mirror the StickC firmware for parity.
class Time:
    T_0_MS = 0
    T_32_MS = 1
    T_96_MS = 2
    T_192_MS = 3
    T_480_MS = 4
    T_960_MS = 5
    T_2400_MS = 6
    T_3840_MS = 7


class Chance:
    CHANCE_100 = 0
    CHANCE_88 = 1
    CHANCE_67 = 2
    CHANCE_50 = 3
    CHANCE_32 = 4
    CHANCE_16 = 5
    CHANCE_10 = 6
    CHANCE_4 = 7
