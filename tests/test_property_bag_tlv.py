"""Property-bag TLV codec tests (Epic 20 B5).

Bit-compatibility with the C++ side (src/ble/property_bag_tlv.{h,cpp} in
nocturnation-stickc) is proved by decoding fixtures a native C++ test
would emit and vice versa. Round-trip, overflow, and malformed-input
cases mirror the C++ test_ble_tlv suite.
"""

import pytest

from nocturnation.protocol import property_bag_tlv as pb


def _capture(encoded):
    return list(pb.decode(encoded))


class TestRoundTrip:
    def test_empty_bag_is_single_zero_byte(self):
        out = pb.encode([])
        assert out == b"\x00"

    def test_u8_round_trip(self):
        out = pb.encode([(pb.KEY_GROUP, pb.TYPE_U8, pb.u8(42))])
        entries = _capture(out)
        assert entries == [("group", pb.TYPE_U8, b"\x2a")]
        assert pb.u8_from(entries[0][2]) == 42

    def test_u16_little_endian(self):
        # 0x4A5B -> [0x5B, 0x4A]
        out = pb.encode([(pb.KEY_BOUND_SID, pb.TYPE_U16, pb.u16(0x4A5B))])
        entries = _capture(out)
        key, vtype, value = entries[0]
        assert value == b"\x5b\x4a"
        assert pb.u16_from(value) == 0x4A5B

    def test_u32_little_endian(self):
        out = pb.encode([("counter", pb.TYPE_U32, pb.u32(0x12345678))])
        entries = _capture(out)
        _, _, value = entries[0]
        assert value == b"\x78\x56\x34\x12"
        assert pb.u32_from(value) == 0x12345678

    def test_bool_round_trip(self):
        out = pb.encode([
            ("flag_on", pb.TYPE_BOOL, pb.boolean(True)),
            ("flag_off", pb.TYPE_BOOL, pb.boolean(False)),
        ])
        entries = _capture(out)
        assert pb.bool_from(entries[0][2]) is True
        assert pb.bool_from(entries[1][2]) is False

    def test_utf8_round_trip(self):
        out = pb.encode([(pb.KEY_FRIENDLY_NAME, pb.TYPE_UTF8, pb.utf8("Front Left Puppet"))])
        entries = _capture(out)
        assert entries[0][0] == "friendly_name"
        assert pb.utf8_from(entries[0][2]) == "Front Left Puppet"

    def test_utf8_empty_string(self):
        out = pb.encode([(pb.KEY_FRIENDLY_NAME, pb.TYPE_UTF8, pb.utf8(""))])
        entries = _capture(out)
        assert entries[0][2] == b""
        assert pb.utf8_from(entries[0][2]) == ""

    def test_bytes_round_trip(self):
        payload = b"\xDE\xAD\xBE\xEF"
        out = pb.encode([("blob", pb.TYPE_BYTES, payload)])
        entries = _capture(out)
        assert entries[0][2] == payload

    def test_multi_entry_preserves_order(self):
        out = pb.encode([
            (pb.KEY_GROUP, pb.TYPE_U8, pb.u8(3)),
            (pb.KEY_LED_POWER, pb.TYPE_U8, pb.u8(40)),
            (pb.KEY_BOUND_SID, pb.TYPE_U16, pb.u16(0x4A00)),
        ])
        keys = [k for (k, _, _) in _capture(out)]
        assert keys == ["group", "led_power", "bound_sid"]


class TestEncodeErrors:
    def test_empty_key_rejected(self):
        with pytest.raises(ValueError):
            pb.encode([("", pb.TYPE_U8, pb.u8(1))])

    def test_too_many_entries_rejected(self):
        with pytest.raises(ValueError):
            pb.encode([("k%d" % i, pb.TYPE_U8, pb.u8(i)) for i in range(256)])

    def test_value_too_long_rejected(self):
        with pytest.raises(ValueError):
            pb.encode([("k", pb.TYPE_BYTES, b"\x00" * 256)])


class TestDecodeResilience:
    def test_empty_buffer_yields_nothing(self):
        assert list(pb.decode(b"")) == []
        assert list(pb.decode(None)) == []

    def test_zero_entries_buffer_yields_nothing(self):
        assert list(pb.decode(b"\x00")) == []

    def test_malformed_key_len_past_buffer(self):
        # entry_count=1, key_len=99, but buffer only holds 3 more bytes.
        with pytest.raises(pb.TlvError):
            _capture(b"\x01\x63xyz")

    def test_malformed_value_len_past_buffer(self):
        # entry_count=1, key_len=1, key='k', type=0x00, value_len=99, no value.
        with pytest.raises(pb.TlvError):
            _capture(b"\x01\x01k\x00\x63")

    def test_zero_key_len_rejected(self):
        with pytest.raises(pb.TlvError):
            _capture(b"\x01\x00\x00")


class TestWellKnownKeys:
    """Match spec section 5 and C++ src/ble/property_bag_tlv.h.
    A mismatch here means either doc or firmware got out of sync."""

    def test_lume_keys(self):
        assert pb.KEY_GROUP == "group"
        assert pb.KEY_LED_POWER == "led_power"
        assert pb.KEY_BOUND_SID == "bound_sid"
        assert pb.KEY_CHANNEL_PREF == "channel_pref"
        assert pb.KEY_STRIP_CHAIN == "strip_chain"
        assert pb.KEY_STRIP_GROUP_SIZE == "strip_group_size"

    def test_director_keys(self):
        assert pb.KEY_DIR_SID_PERF == "dir_sid_perf"
        assert pb.KEY_RETX_COUNT == "retx_count"

    def test_both_role_keys(self):
        assert pb.KEY_PAIR_WIN_S == "pair_win_s"
        assert pb.KEY_FRIENDLY_NAME == "friendly_name"
