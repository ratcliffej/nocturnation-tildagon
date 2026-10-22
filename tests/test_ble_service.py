"""BleService tests (Epic 20 B5).

Covers the parts that don't touch aioble: advertising-name composition,
fixed-format device_info / status payload builders, config-bag
serialise + apply round-trip, and the pairing-control state machine.

aioble is not importable on CPython so start() / _serve() / _service_
client() are exercised on-device only. Their integration is validated
at bench time via nRF Connect against a real Tildagon.
"""

import types

from nocturnation import ble_service as bs
from nocturnation.protocol import property_bag_tlv as pb


def _stub_settings(**overrides):
    """Build a minimal Settings-shaped object without importing the
    real Settings class (avoids the JSON load side-effects)."""
    s = types.SimpleNamespace(
        group=0,
        channel="auto",
        active_show="",
        mode="lume",
        help_url="",
        debug_mode=False,
        repeat="OFF",
        friendly_name="",
        pair_win_s=30,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestAdvertisingName:
    def test_fallback_uses_mac_suffix(self):
        mac = b"\xAA\xBB\xCC\x3F\x7A\x2B"
        assert bs.compose_advertising_name(mac, "") == "NCTN-Lume-3F7A2B"

    def test_friendly_name_overrides(self):
        assert bs.compose_advertising_name(
            b"\x00\x00\x00\x11\x22\x33",
            "Front Left Puppet") == "Front Left Puppet"

    def test_friendly_name_clipped_at_20(self):
        assert bs.compose_advertising_name(
            b"\x00" * 6,
            "abcdefghijklmnopqrstuvwxyz") == "abcdefghijklmnopqrst"

    def test_missing_mac_falls_back_to_zeros(self):
        assert bs.compose_advertising_name(None, "") == "NCTN-Lume-000000"
        assert bs.compose_advertising_name(b"\x00\x00\x00", "") == "NCTN-Lume-000000"


class TestDeviceInfoLayout:
    def test_byte_layout_matches_spec(self):
        mac = b"\x11\x22\x33\x44\x55\x66"
        out = bs.build_device_info(mac, 12345)
        assert len(out) == 24
        assert out[0] == bs.SERVICE_VERSION
        assert out[1] == bs.ROLE_LUME
        assert out[2] == bs.HOST_TILDAGON
        # fw_ver at 3..5, wire_ver at 6, reserved 7
        assert out[6] == bs.WIRE_VERSION
        assert out[7] == 0
        # bt_mac at 8..13
        assert bytes(out[8:14]) == mac
        # uptime u32 LE at 14..17
        assert out[14] == (12345 & 0xFF)
        assert out[15] == ((12345 >> 8) & 0xFF)
        # reserved 18..23
        assert bytes(out[18:24]) == b"\x00" * 6

    def test_short_mac_is_left_zero_padded(self):
        # Documents the manual-pad behaviour that replaced bytes.rjust()
        # (MicroPython doesn't implement .rjust on bytes - see
        # feedback-micropython-stdlib-gotchas memory). The pad goes on
        # the left so the trailing bytes of the MAC land in the
        # right-aligned slot.
        out = bs.build_device_info(b"\xAA\xBB", 0)
        assert bytes(out[8:14]) == b"\x00\x00\x00\x00\xAA\xBB"

    def test_long_mac_is_truncated_from_the_right(self):
        # 8-byte input - last two bytes dropped, first six written as-is.
        out = bs.build_device_info(b"\x11\x22\x33\x44\x55\x66\x77\x88", 0)
        assert bytes(out[8:14]) == b"\x11\x22\x33\x44\x55\x66"

    def test_none_mac_renders_zeros(self):
        out = bs.build_device_info(None, 0)
        assert bytes(out[8:14]) == b"\x00" * 6


class TestBagSizeCeiling:
    """build_lume_config_bag enforces a hard ceiling on the encoded
    output so a future key addition that tips the bag past the GATT
    single-write window fails loudly at service-register time instead
    of silently dropping over the air.
    """

    def _settings(self, friendly_name=""):
        class _S:
            pass
        s = _S()
        s.group = 3
        s.channel = "11"
        s.pair_win_s = 30
        s.friendly_name = friendly_name
        return s

    def test_normal_bag_well_under_ceiling(self):
        # Typical bag with no friendly_name is ~75 B.
        out = bs.build_lume_config_bag(self._settings(), 100, 12, 12)
        assert 0 < len(out) < bs._MAX_CONFIG_BAG_LEN

    def test_long_friendly_name_still_fits(self):
        # Max-length friendly_name (20 chars) brings the bag to
        # ~98 B. Well under the ceiling.
        out = bs.build_lume_config_bag(self._settings("x" * 20), 100, 12, 12)
        assert len(out) < bs._MAX_CONFIG_BAG_LEN

    def test_oversized_bag_raises(self):
        # Monkeypatch the encoder to produce an over-ceiling bag so
        # we can verify the guard fires without having to synthesise
        # 400+ bytes of real settings.
        from nocturnation.protocol import property_bag_tlv as pb
        real_encode = pb.encode
        try:
            pb.encode = lambda entries: b"X" * (bs._MAX_CONFIG_BAG_LEN + 1)
            try:
                bs.build_lume_config_bag(self._settings(), 100, 12, 12)
                raise AssertionError("expected ValueError")
            except ValueError as exc:
                assert "exceeds" in str(exc) or "exceed" in str(exc)
        finally:
            pb.encode = real_encode


class TestStatusLayout:
    def test_byte_layout(self):
        out = bs.build_status(0x11, 0x01, 0x004A, 80, 92)
        assert len(out) == 12
        assert out[0] == 0x11
        assert out[1] == 0x01
        # source_id u16 LE
        assert out[2] == 0x4A
        assert out[3] == 0x00
        assert out[4] == 80
        assert out[5] == 92
        # drops + reserved zeroed
        assert bytes(out[6:]) == b"\x00" * 6


class TestConfigBagRoundTrip:
    def test_serialise_includes_lume_keys(self):
        s = _stub_settings(group=3, channel="1", pair_win_s=45,
                           friendly_name="Rear Right")
        bag = bs.build_lume_config_bag(s, brightness_pct=40,
                                       chain_size=29, group_size=6)
        entries = {k: (t, v) for (k, t, v) in pb.decode(bag)}
        assert pb.u8_from(entries[pb.KEY_GROUP][1]) == 3
        assert pb.u8_from(entries[pb.KEY_LED_POWER][1]) == 40
        assert pb.u8_from(entries[pb.KEY_CHANNEL_PREF][1]) == 1
        assert pb.u16_from(entries[pb.KEY_STRIP_CHAIN][1]) == 29
        assert pb.u8_from(entries[pb.KEY_STRIP_GROUP_SIZE][1]) == 6
        assert pb.u8_from(entries[pb.KEY_PAIR_WIN_S][1]) == 45
        assert pb.utf8_from(entries[pb.KEY_FRIENDLY_NAME][1]) == "Rear Right"

    def test_serialise_omits_empty_friendly_name(self):
        s = _stub_settings(friendly_name="")
        bag = bs.build_lume_config_bag(s, brightness_pct=100,
                                       chain_size=12, group_size=12)
        keys = [k for (k, _, _) in pb.decode(bag)]
        assert pb.KEY_FRIENDLY_NAME not in keys

    def test_apply_updates_recognised_keys(self):
        s = _stub_settings()
        payload = pb.encode([
            (pb.KEY_GROUP, pb.TYPE_U8, pb.u8(2)),
            (pb.KEY_CHANNEL_PREF, pb.TYPE_U8, pb.u8(11)),
            (pb.KEY_FRIENDLY_NAME, pb.TYPE_UTF8, pb.utf8("Puppet #12")),
        ])
        assert bs.apply_config_bag(payload, s) == bs.STATUS_OK
        assert s.group == 2
        assert s.channel == "11"
        assert s.friendly_name == "Puppet #12"

    def test_apply_ignores_unknown_keys(self):
        s = _stub_settings(group=5)
        payload = pb.encode([
            (pb.KEY_GROUP, pb.TYPE_U8, pb.u8(1)),
            ("future_key", pb.TYPE_U8, pb.u8(99)),
        ])
        assert bs.apply_config_bag(payload, s) == bs.STATUS_OK
        assert s.group == 1  # recognised key applied
        assert not hasattr(s, "future_key")  # unknown key silently dropped

    def test_apply_returns_malformed_on_bad_tlv(self):
        s = _stub_settings()
        # entry_count=1, key_len=99 - overrun
        assert bs.apply_config_bag(b"\x01\x63xyz", s) == bs.STATUS_MALFORMED_TLV

    def test_apply_flags_type_mismatch_out_of_range(self):
        s = _stub_settings()
        # group is spec u8; write with utf8 type.
        payload = pb.encode([(pb.KEY_GROUP, pb.TYPE_UTF8, pb.utf8("nope"))])
        assert bs.apply_config_bag(payload, s) == bs.STATUS_VALUE_OUT_OF_RANGE


class TestPairingStateMachine:
    def _svc(self):
        svc = bs.BleService()
        svc._active = True
        svc._state = bs.STATE_OPEN
        svc._start_ms = 0
        svc._window_ms = 30_000
        return svc

    def test_tick_before_timeout_stays_open(self):
        svc = self._svc()
        assert svc.tick(15_000) == bs.STATE_OPEN

    def test_tick_at_timeout_transitions_to_aborted(self):
        svc = self._svc()
        assert svc.tick(30_000) == bs.STATE_ABORTED

    def test_tick_pauses_countdown_while_client_connected(self):
        """A connected client extends the pairing window indefinitely.
        Without this, a slow pairing session (operator reading + writing
        config over the GATT char) gets booted off mid-flow.
        """
        svc = self._svc()
        svc._client_connected = True
        # Well past the 30 s window; stays OPEN because client is live.
        assert svc.tick(60_000) == bs.STATE_OPEN
        assert svc.tick(120_000) == bs.STATE_OPEN

    def test_tick_restarts_window_after_disconnect(self):
        """When the client disconnects, the countdown restarts from the
        current tick - giving a predictable "stay open N seconds after
        the last client leaves" UX.
        """
        svc = self._svc()
        svc._client_connected = True
        svc.tick(60_000)         # pauses, resets start_ms to 60_000
        svc._client_connected = False
        # Not yet past the window from 60_000; still OPEN.
        assert svc.tick(80_000) == bs.STATE_OPEN
        # 90_000 - 60_000 = 30_000, hits the window.
        assert svc.tick(90_000) == bs.STATE_ABORTED

    def test_commit_action_transitions_to_committed(self):
        svc = self._svc()
        svc.notify_pairing_control(0x01)
        assert svc.state() == bs.STATE_COMMITTED

    def test_sleep_action_transitions_to_sleeping(self):
        svc = self._svc()
        svc.notify_pairing_control(0x02)
        assert svc.state() == bs.STATE_SLEEPING

    def test_abort_action_transitions_to_aborted(self):
        svc = self._svc()
        svc.notify_pairing_control(0x03)
        assert svc.state() == bs.STATE_ABORTED

    def test_unknown_action_silently_ignored(self):
        svc = self._svc()
        svc.notify_pairing_control(0xFF)
        assert svc.state() == bs.STATE_OPEN

    def test_action_outside_open_ignored(self):
        svc = self._svc()
        svc._state = bs.STATE_CLOSED
        svc.notify_pairing_control(0x01)
        assert svc.state() == bs.STATE_CLOSED

    def test_seconds_remaining_when_open(self):
        svc = self._svc()
        assert svc.seconds_remaining(0) == 30
        assert svc.seconds_remaining(15_000) == 15
        assert svc.seconds_remaining(30_000) == 0

    def test_seconds_remaining_when_closed_is_zero(self):
        svc = self._svc()
        svc._state = bs.STATE_CLOSED
        assert svc.seconds_remaining(0) == 0
