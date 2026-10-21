"""NocturNation BLE GATT service - Tildagon Lume role (Epic 20 B5).

MicroPython counterpart to the C++ src/ble/ble_service.{h,cpp} in
nocturnation-stickc. Advertises the same NocturNation service UUID
and hosts the same characteristic set defined in
Docs/manuals/ble-service.md so any BLE client (nRF Connect, a future
NocturNation app, a laptop CLI over `bleak`) can pair to a Tildagon
identically to how it pairs to a StickC or Atom.

MicroPython gotchas (see feedback-micropython-stdlib-gotchas):
  - Runs against `aioble` (declared in the Tildagon manifest).
  - Uses uasyncio; host tests do NOT import this module - it would
    fail on `import aioble` outside a Tildagon device.
  - No f-strings that reference locals through the format machinery -
    plain %s / %d.
"""

# NB: `import aioble` deliberately not at module scope. This module is
# imported by app.py at startup on the Tildagon, but the host pytest
# suite imports app.py too (via other modules), and aioble isn't in
# CPython. The lazy _import_aioble() below is called only when
# BleService.start() runs on-device.

from .protocol import property_bag_tlv as pb


# Service and characteristic UUIDs (must match src/ble/ble_service.h).
SERVICE_UUID          = "2086dee9-a671-44b1-ae81-123829bd4c88"
CHAR_DEVICE_INFO      = "37b33d23-b02d-4705-be47-b19c57a981ee"
CHAR_CONFIG           = "381b3227-28f7-4036-9ff0-cb76a6e43430"
CHAR_STATUS           = "52a398a9-2c10-4291-885e-2a98a8f4d54e"
CHAR_PAIRING_CONTROL  = "72678cdb-1b65-417c-8744-751eff153bf0"
CHAR_SHOW_PASSTHROUGH = "17cc926e-f45e-41d8-ab61-1cb71d744c35"

# Service version + role/host enums (must match src/ble/ble_service.h).
SERVICE_VERSION = 0x01
ROLE_LUME       = 0x02
HOST_TILDAGON   = 0x06
WIRE_VERSION    = 0x04

# Pairing state machine (must match ble::PairingState numerically).
STATE_CLOSED    = 0
STATE_OPEN      = 1
STATE_COMMITTED = 2
STATE_SLEEPING  = 3
STATE_ABORTED   = 4

# Status codes returned by writes (spec section 3.2).
STATUS_OK                     = 0x00
STATUS_NOT_IN_PAIRING_WINDOW  = 0x81
STATUS_MALFORMED_TLV          = 0x82
STATUS_VALUE_OUT_OF_RANGE     = 0x83

# Firmware version reported over BLE. Kept manually in sync with
# metadata.json; a mismatch is harmless (the value is diagnostic).
FW_MAJOR = 1
FW_MINOR = 0
FW_PATCH = 21


def _import_aioble():
    """Import aioble lazily so host tests don't trip on the missing
    module in CPython. Callers must be running on-device."""
    import aioble  # noqa: F401
    import bluetooth  # noqa: F401
    return aioble, bluetooth


def compose_advertising_name(bt_mac, friendly_name):
    """Return the advertising name for a Tildagon Lume.

    friendly_name (non-empty str) overrides the fallback. bt_mac is a
    6-byte bytes-like; the fallback uses the last 3 bytes.
    """
    if friendly_name:
        # Spec allows up to 20 chars; clip on the reader side too.
        return friendly_name[:20]
    if bt_mac is None or len(bt_mac) < 6:
        return "NCTN-Lume-000000"
    return "NCTN-Lume-%02X%02X%02X" % (bt_mac[3], bt_mac[4], bt_mac[5])


def build_device_info(bt_mac, uptime_s):
    """Return the 24-byte device_info payload.

    Layout (little-endian, packed) per spec section 3.1:
      offset 0  service_version
      offset 1  role
      offset 2  host
      offset 3  fw_ver_major
      offset 4  fw_ver_minor
      offset 5  fw_ver_patch
      offset 6  wire_ver
      offset 7  reserved (zero)
      offset 8  bt_mac[6] big-endian
      offset 14 uptime_s u32 LE
      offset 18 reserved (zeros)
    """
    if bt_mac is None:
        bt_mac = b"\x00" * 6
    else:
        # MicroPython bytes has no .rjust() method (CPython does); pad
        # manually so this works on both host tests and the badge.
        # See [[feedback-micropython-stdlib-gotchas]].
        bt_mac = bytes(bt_mac)[:6]
        if len(bt_mac) < 6:
            bt_mac = b"\x00" * (6 - len(bt_mac)) + bt_mac
    out = bytearray(24)
    out[0] = SERVICE_VERSION
    out[1] = ROLE_LUME
    out[2] = HOST_TILDAGON
    out[3] = FW_MAJOR
    out[4] = FW_MINOR
    out[5] = FW_PATCH
    out[6] = WIRE_VERSION
    out[7] = 0
    out[8:14] = bt_mac
    out[14] = uptime_s & 0xFF
    out[15] = (uptime_s >> 8) & 0xFF
    out[16] = (uptime_s >> 16) & 0xFF
    out[17] = (uptime_s >> 24) & 0xFF
    # 18..23 reserved (already zero).
    return bytes(out)


def build_status(running_mode, lock_state, source_id, battery_pct, signal_pct):
    """Return the 12-byte status payload per spec section 3.3."""
    out = bytearray(12)
    out[0] = running_mode & 0xFF
    out[1] = lock_state & 0xFF
    out[2] = source_id & 0xFF
    out[3] = (source_id >> 8) & 0xFF
    out[4] = battery_pct & 0xFF
    out[5] = signal_pct & 0xFF
    # airtime_drops[6..9] stays 0 on Lumes.
    # reserved[10..11] stays 0.
    return bytes(out)


def build_lume_config_bag(settings, brightness_pct, chain_size, group_size):
    """Serialise the current Lume-role config into a property bag.

    settings is a Settings instance (nocturnation/settings.py).
    brightness_pct / chain_size / group_size come from the renderers
    since Settings itself doesn't own them - they're derived state we
    surface for the app.
    """
    entries = [
        (pb.KEY_GROUP,            pb.TYPE_U8,  pb.u8(settings.group)),
        (pb.KEY_LED_POWER,        pb.TYPE_U8,  pb.u8(brightness_pct)),
        (pb.KEY_CHANNEL_PREF,     pb.TYPE_U8,  pb.u8(_channel_pref_to_u8(settings.channel))),
        (pb.KEY_STRIP_CHAIN,      pb.TYPE_U16, pb.u16(chain_size)),
        (pb.KEY_STRIP_GROUP_SIZE, pb.TYPE_U8,  pb.u8(group_size)),
        (pb.KEY_PAIR_WIN_S,       pb.TYPE_U8,  pb.u8(getattr(settings, "pair_win_s", 30))),
    ]
    fn = getattr(settings, "friendly_name", "")
    if fn:
        entries.append((pb.KEY_FRIENDLY_NAME, pb.TYPE_UTF8, pb.utf8(fn)))
    return pb.encode(entries)


def _channel_pref_to_u8(channel):
    """Map Settings.channel (str "auto" / "1" / "11") to the BLE u8
    representation (0 = auto, 1/6/11 = pinned). Spec section 5.1."""
    if channel == "1":
        return 1
    if channel == "11":
        return 11
    return 0


def _channel_pref_from_u8(value):
    """Inverse of the above; unknown values fall back to auto."""
    if value == 1:
        return "1"
    if value == 11:
        return "11"
    return "auto"


def apply_config_bag(payload, settings):
    """Decode an incoming config TLV and apply recognised entries to
    settings. Returns a status code from the STATUS_* family.

    Unrecognised keys are silently accepted (spec section 4 forward-
    compat). Value-type or range mismatches on a recognised key set
    the out-of-range flag but don't reject the whole bag - matches the
    C++ ConfigCallbacks::onWrite behaviour.
    """
    try:
        entries = list(pb.decode(payload))
    except pb.TlvError:
        return STATUS_MALFORMED_TLV

    any_out_of_range = False
    for key, value_type, value in entries:
        try:
            if key == pb.KEY_GROUP and value_type == pb.TYPE_U8:
                settings.group = pb.u8_from(value)
            elif key == pb.KEY_LED_POWER and value_type == pb.TYPE_U8:
                # Persist as-is; the renderers clamp at render time.
                # Currently Tildagon perimeter is uniformly 100 %; the
                # written value is stored for consistency with the
                # cross-fleet property bag but has no rendering effect
                # in v0x01.
                settings.led_power = pb.u8_from(value)
            elif key == pb.KEY_CHANNEL_PREF and value_type == pb.TYPE_U8:
                settings.channel = _channel_pref_from_u8(pb.u8_from(value))
            elif key == pb.KEY_STRIP_CHAIN and value_type == pb.TYPE_U16:
                # Tildagon has a fixed 12-pixel perimeter, so this key
                # is accepted-and-remembered for cross-host consistency
                # (the phone writes to all Lumes with the same bag) but
                # doesn't change rendering here.
                settings.strip_chain = pb.u16_from(value)
            elif key == pb.KEY_STRIP_GROUP_SIZE and value_type == pb.TYPE_U8:
                settings.strip_group_size = pb.u8_from(value)
            elif key == pb.KEY_PAIR_WIN_S and value_type == pb.TYPE_U8:
                settings.pair_win_s = pb.u8_from(value)
            elif key == pb.KEY_FRIENDLY_NAME and value_type == pb.TYPE_UTF8:
                settings.friendly_name = pb.utf8_from(value)[:20]
            else:
                # Unrecognised key OR recognised key with wrong type.
                # Silent ignore covers forward-compat; type mismatch
                # flags out-of-range.
                if key in (pb.KEY_GROUP, pb.KEY_LED_POWER,
                          pb.KEY_CHANNEL_PREF, pb.KEY_STRIP_CHAIN,
                          pb.KEY_STRIP_GROUP_SIZE, pb.KEY_PAIR_WIN_S,
                          pb.KEY_FRIENDLY_NAME):
                    any_out_of_range = True
        except (ValueError, TypeError):
            any_out_of_range = True

    return STATUS_VALUE_OUT_OF_RANGE if any_out_of_range else STATUS_OK


class BleService:
    """Wraps the aioble service + advertising lifecycle.

    Firmware calls start(settings, brightness_pct, chain_size, group_size)
    to bring BLE up + advertise; stop() to tear down. tick() returns
    the current pairing state; app.py polls it from its update() loop
    to drive UI state.

    Threading: aioble runs on the uasyncio event loop. On-device
    integration schedules the service loop as an asyncio task.
    """

    def __init__(self):
        self._active = False
        self._state = STATE_CLOSED
        self._settings = None
        self._start_ms = 0
        self._window_ms = 30000
        self._bt_mac = b"\x00" * 6
        self._task = None
        # True while a BLE client is connected. tick() pauses the
        # pairing-window countdown while this is set, so a client that
        # takes longer than 30 s to read + write config doesn't get
        # kicked out mid-session. Flipped on in _serve() when a
        # connection yields; flipped off when _service_client returns
        # (disconnect, abort, or stop).
        self._client_connected = False

    # ---------- Lifecycle ------------------------------------------------

    def is_active(self):
        return self._active

    def state(self):
        return self._state

    def seconds_remaining(self, now_ms):
        if not self._active or self._state != STATE_OPEN:
            return 0
        elapsed = now_ms - self._start_ms
        if elapsed >= self._window_ms:
            return 0
        return (self._window_ms - elapsed) // 1000

    def tick(self, now_ms):
        """Called by app.py's update() loop; drives the auto-timeout to
        STATE_ABORTED. Returns the current state so the caller can
        react without a second call.

        While a client is connected, the countdown pauses - the window
        is to protect against a Lume being left announcing with no one
        there, not against a slow client. The countdown restarts if
        the client disconnects (clears _client_connected).
        """
        if self._active and self._state == STATE_OPEN:
            if self._client_connected:
                # Reset the start reference so the window re-begins
                # when the client eventually disconnects.
                self._start_ms = now_ms
            elif now_ms - self._start_ms >= self._window_ms:
                self._state = STATE_ABORTED
        return self._state

    def start(self, settings, brightness_pct, chain_size, group_size,
              pair_win_s=30, now_ms=0):
        """Bring BLE up + start advertising. Idempotent.

        On-device only: this method calls into aioble. Host tests that
        exercise the tick / apply paths should NOT call start().
        """
        if self._active:
            return True
        self._settings = settings
        self._window_ms = int(pair_win_s) * 1000
        self._start_ms = now_ms
        self._state = STATE_OPEN
        self._client_connected = False
        try:
            # Read the actual Bluetooth MAC so the advertising name is
            # unique instead of the all-zero default. MicroPython ESP32
            # exposes it via bluetooth.BLE().config('mac'); the call
            # also implicitly activates the BLE radio which aioble will
            # pick up when it starts advertising.
            try:
                _, bluetooth = _import_aioble()
                ble = bluetooth.BLE()
                ble.active(True)
                mac = ble.config('mac')
                # config('mac') returns (addr_type, mac_bytes).
                if isinstance(mac, tuple) and len(mac) == 2:
                    self._bt_mac = bytes(mac[1])
                else:
                    self._bt_mac = bytes(mac)
                print("[ble] bt_mac=%s" % ":".join("%02X" % b for b in self._bt_mac))
            except Exception as exc:
                print("[ble] bt_mac read failed: %s" % exc)
            self._register_service(brightness_pct, chain_size, group_size)
            self._active = True
            return True
        except Exception as exc:
            print("[ble] start failed: %s" % exc)
            self._state = STATE_CLOSED
            return False

    def stop(self):
        """Tear down BLE + return to idle. Idempotent."""
        if not self._active:
            return
        try:
            self._unregister_service()
        except Exception as exc:
            print("[ble] stop error: %s" % exc)
        self._active = False
        self._state = STATE_CLOSED

    def notify_pairing_control(self, action):
        """Feed the pairing_control write into the state machine.
        Public so callback dispatchers can reach it without friend
        declarations."""
        if self._state != STATE_OPEN:
            return
        if action == 0x01:
            self._state = STATE_COMMITTED
        elif action == 0x02:
            self._state = STATE_SLEEPING
        elif action == 0x03:
            self._state = STATE_ABORTED
        # Unknown values silently ignored (spec section 3.4).

    # ---------- aioble binding (on-device only) --------------------------

    def _register_service(self, brightness_pct, chain_size, group_size):
        """Register the GATT service with aioble.

        Intentionally minimal in v0x01: reads populate the fixed
        characteristics; writes to config_ are decoded via
        apply_config_bag(); writes to pairing_control route through
        notify_pairing_control. Notify on status is deferred to a
        follow-on.
        """
        aioble, _ = _import_aioble()
        # UUIDs must be constructed from bytes on aioble; convert the
        # canonical string form.
        from bluetooth import UUID
        svc_uuid = UUID(SERVICE_UUID)
        svc = aioble.Service(svc_uuid)
        self._chr_device_info = aioble.Characteristic(
            svc, UUID(CHAR_DEVICE_INFO), read=True,
            initial=build_device_info(self._bt_mac, 0))
        self._chr_config = aioble.Characteristic(
            svc, UUID(CHAR_CONFIG), read=True, write=True, capture=True,
            initial=build_lume_config_bag(
                self._settings, brightness_pct, chain_size, group_size))
        self._chr_status = aioble.Characteristic(
            svc, UUID(CHAR_STATUS), read=True,
            initial=build_status(0x11, 0x00, 0xFFFF, 0xFF, 0xFF))
        self._chr_pair_ctrl = aioble.Characteristic(
            svc, UUID(CHAR_PAIRING_CONTROL), write=True, capture=True)
        self._chr_show_pt = aioble.Characteristic(
            svc, UUID(CHAR_SHOW_PASSTHROUGH), read=True, write=True,
            capture=True,
            initial=pb.encode([("reserved", pb.TYPE_U8, pb.u8(0x01))]))
        aioble.register_services(svc)

        adv_name = compose_advertising_name(
            self._bt_mac,
            getattr(self._settings, "friendly_name", ""))
        import uasyncio as asyncio
        self._task = asyncio.create_task(self._serve(adv_name))

    def _unregister_service(self):
        # aioble doesn't expose a full "unregister" - stopping
        # advertising is what actually matters for the operator UX.
        # The service objects stay in memory until process restart;
        # a subsequent start() reuses them (init in Characteristic()
        # will fail on double-register, so start() is guarded by
        # self._active).
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _serve(self, adv_name):
        """Advertising + client-write drain loop. Runs until stop() or
        the pairing window closes."""
        aioble, _ = _import_aioble()
        try:
            while self._active and self._state == STATE_OPEN:
                async with await aioble.advertise(
                    250_000,
                    name=adv_name,
                    services=[self._service_uuid_bytes()],
                ) as connection:
                    print("[ble] client connected: %s" % connection.device)
                    # Pause the pairing-window countdown while a client
                    # is connected (tick() watches this flag). Cleared
                    # in the finally so an abrupt disconnect still
                    # releases the window.
                    self._client_connected = True
                    try:
                        await self._service_client(connection)
                    finally:
                        self._client_connected = False
                        print("[ble] client disconnected")
        except Exception as exc:
            print("[ble] serve error: %s" % exc)

    def _service_uuid_bytes(self):
        from bluetooth import UUID
        return UUID(SERVICE_UUID)

    async def _service_client(self, connection):
        """Read pending characteristic writes while the client is
        connected."""
        import uasyncio as asyncio
        try:
            while self._active and self._state == STATE_OPEN:
                # aioble's `capture=True` characteristics deliver writes
                # via .written(); poll both in a select-shaped loop.
                try:
                    _, cfg_data = await asyncio.wait_for(
                        self._chr_config.written(), 0.1)
                    if cfg_data is not None:
                        status = apply_config_bag(cfg_data, self._settings)
                        print("[ble] config write status=0x%02X" % status)
                except asyncio.TimeoutError:
                    pass
                try:
                    _, ctrl_data = await asyncio.wait_for(
                        self._chr_pair_ctrl.written(), 0.01)
                    if ctrl_data and len(ctrl_data) >= 1:
                        self.notify_pairing_control(ctrl_data[0])
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            print("[ble] client loop error: %s" % exc)
