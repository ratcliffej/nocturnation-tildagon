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

# ESP-NOW supports up to 250-byte payloads; Epic 13 raised the
# NocturNation cap from 32 to 250 to fit the new display family.
# TEXT_DISPLAY's max payload is 200 bytes (6 prefix + 1 + 64 header
# + 1 + 128 body); BITMAP_PLANE chunks can push close to the radio
# ceiling. LIGHT_PULSE / LIGHT_WASH frames remain well under 32 so
# the bump costs nothing on their airtime - the ceiling only matters
# for what the parser will accept.
MAX_FRAME_SIZE = 250
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE

# Epic 13 TEXT_DISPLAY wire constants. Validated by parse_frame on
# receive; kept here rather than in _generated.py because the string-
# encoding rules are receiver-side interpretation and don't appear in
# the auto-generated YAML.
TEXT_DISPLAY_MAX_HEADER_LEN  = 64
TEXT_DISPLAY_MAX_BODY_LEN    = 128
TEXT_DISPLAY_FIXED_PREFIX    = 6   # target_group + r + g + b + ttl_ms(2 LE)
TEXT_DISPLAY_MIN_PAYLOAD_LEN = TEXT_DISPLAY_FIXED_PREFIX + 1 + 1  # both strings empty


class DeviceClass:
    ALL = 0x00              # addressing wildcard; never advertised by a receiver
    LIGHT = 0x01            # PixMob bracelets, LED wristbands
    SCREEN = 0x02           # Stick LCD
    MULTI_LED_SCREEN = 0x03  # Tildagon (this device)
    DISPLAY = 0x04           # Epic 13 - text/bitmap surface (Stick LCD,
                             # Tildagon round LCD). Routing for the
                             # display family is by message type rather
                             # than target_class, but the enum value is
                             # kept here for parity with the C++ side.
    # 0x05..0xFF reserved


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
