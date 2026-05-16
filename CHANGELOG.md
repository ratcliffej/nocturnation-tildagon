# Changelog

Notable changes to the NocturNation Tildagon receiver app. Versioning
matches `tildagon.toml`'s integer `version` field, which the EMF app
store treats monotonically rather than as semver.

## 2026-05-16 — Auto-scan now includes channel 6 (design intent)

`SCAN_ORDER` in `nocturnation.channel_scan` extended from `(11, 1)`
to `(11, 1, 6)` per spec v0.29 §5.3. Channel 6 was previously
operator-locked-only; it now appears as the third (last-priority)
auto-scan target so a default-config Lume can find a Director on
any of the three configured channels without operator intervention.

**Tildagon-specific caveat:** per Epic 5 Q6 the badge's STA_IF
networking layer only honours the first `wlan.config(channel=N)`
call after bringing the radio up; subsequent calls raise
`RuntimeError 0xffffffff`. So the runtime is single-channel in
practice - the Tildagon scans the first channel, falls back to it
on the second call's failure, and asks the operator to align the
Director Stick manually. The 3-channel `SCAN_ORDER` is the design
intent for when Q6 is resolved; the immediate code change is a
constant update + test update only, no behavioural change on
hardware.

## 2026-05-16 — Protocol v2: magic prefix for ESP-NOW disambiguation

Wire-incompatible bump from protocol version `0x01` to `0x02`.

- Inbound frames now require a two-byte magic prefix `0x4E 0x4E`
  (ASCII "NN") at offset 0..1 before any other validation;
  `parse_frame` rejects with `FrameError` if absent.
- `HEADER_SIZE` grew from 6 to 8; all other field offsets shift +2.
  Constants `MAGIC_0`, `MAGIC_1` added to the protocol module.

Motivation: NocturNation shares the 2.4 GHz band with anything else
running ESP-NOW vendor action frames on the same channel - a real
concern at event-density deployments like EMF Camp. The previous
`protocol_version`-only check passed roughly one in a few million
random ESP-NOW frames as if they were NocturNation, which read on
the badge as occasional stray flashes in busy RF environments. The
two-byte magic drops the false-positive rate by three orders of
magnitude.

v1 and v2 receivers cannot interoperate. Tildagon + M5 Director
firmware must be on the same version. The companion change on the
M5 firmware side is in `ratcliffej/nocturnation-m5`.

## 2026-05-16 — Spec v0.29 protocol trim + Lume power optimisation

Aligns the Tildagon receiver with the spec v0.29 §4.3 trimmed
protocol — two active wire-format message types only (HEARTBEAT
and LIGHT_COMMAND) — and removes the local DROP / BREAKDOWN
synthetic-fire rendering that consumed the now-deleted MUSIC_EVENT
frames.

Wire-format changes:
- HEARTBEAT payload grows from 0 to 9 bytes: `tick: u32` +
  `days_since_2026: u16` + `centiseconds_today: u24`, all
  little-endian per spec v0.29 §3.3.1. Tier 0/1/2 Lumes consume
  only `tick`; the Tildagon's `Frame` now exposes all three fields
  on inbound HEARTBEAT frames for any future consumer.
- BEAT_DETECTED (0x01), MODE_CHANGE (0x02), CLOCK_SYNC (0x04),
  TIME_SYNC (0x05), MUSIC_EVENT (0x06) removed from the
  `MessageType` enum and `PAYLOAD_LENGTHS` table. Numeric IDs are
  RESERVED (do not reuse) per spec §4.3 rationale.
- Inbound frames carrying a removed or unassigned `message_type`
  are silently dropped by `parse_frame` raising `FrameError` (the
  `PAYLOAD_LENGTHS` lookup returns `None`, but the protocol-version
  / payload-length validation still runs and a reserved-ID frame
  will typically fail the latter; either way `_receive_loop`
  catches and discards).

Receiver-side changes:
- `nocturnation/music_event.py` deleted. The DROP whiteout and
  BREAKDOWN blue fade are no longer rendered locally; the Director
  no longer emits the MUSIC_EVENT frames that triggered them.
- `app.py` `_observe_frame` simplified: HEARTBEAT and any
  reserved-id frame just bump the frame counter for NO-SIGNAL
  liveness; only LIGHT_COMMAND drives renderer dispatch.
- `MusicEventType` enum removed from the protocol package's public
  exports.

Power optimisation:
- The Tildagon's async receive loop already yields cleanly to the
  scheduler via `await asyncio.sleep_ms(5)`; structurally equivalent
  to the M5 firmware's new main-loop `delay(1)` yield. No code
  change needed in this Epic.

Tests:
- `tests/test_music_event.py` deleted (13 tests for the removed
  synthetic-fire module).
- `tests/test_frame.py` HEARTBEAT vector rewritten for the new
  9-byte payload; new `TestHeartbeatPayload::test_heartbeat_unpacks_tick_and_date_fields`
  asserts the LE byte-order unpacking. MUSIC_EVENT vector / tests
  removed. The remaining `test_wrong_payload_len_for_known_type_rejected`
  now exercises HEARTBEAT (which has a payload to mis-claim
  against; in the pre-trim world it was zero-payload, so the
  test had to construct a different malformed frame).

107 / 107 host-side pytest tests pass on CPython 3.10+.

## 2026-05-16 — Director / Lume vocabulary rename

Comments, log strings, README, CHANGELOG, and the `tildagon.toml`
app-store description migrated from the legacy *master* / *slave*
role vocabulary to **Director** (upstream node) and **Lume**
(downstream device). Shipped as branch
`rename/director-lume-vocabulary` with the seven-block shape
parallel to the M5 firmware repo; Blocks 1, 2, 4, and 5 are
empty no-op commits because the Tildagon app has no role-class
hierarchy, no role-encoding identifiers, no role-bearing UI
strings, and no role-named tests. App name "NocturNation" is
unchanged. 120/120 host-side pytest tests stay green.

## Version 1 (Epic 5 Blocks 1-6, May 2026)

First app-store-submittable release. Implements the full receive
contract from the NocturNation protocol manual and the photosensitivity
safety constraints from architecture spec §15.

### Block 1: Platform familiarisation

- Minimal Tildagon OS app conforming to `app.App` + `__app_export__`.
- Hardware iteration loop wrapped in `deploy.sh` (`mpremote exec`
  recursive wipe + `mpremote cp -r apps/nocturnation :apps/` +
  `mpremote reset`) so the per-deploy gotchas don't recur.

### Block 2: ESP-NOW receive

- Sixteen-deep dedup ring on `(source_id, sequence_number)` per
  protocol manual §2.3 (absorbs the Director's 3× redundant TX).
- `hop_count > 3` drop per §2.3.
- Channel auto-scan state machine per §5.3 (channel 11 first, then 1).
  Known limitation captured as Epic 5 Q6: `wlan.config(channel=N)`
  rejects a second channel-change attempt with `RuntimeError 0xffffffff`
  on STA_IF; the app falls back to receiving on the first channel that
  succeeded and tells the operator to align the Director Stick to it
  manually.

### Block 3: Perimeter LED rendering

- Per-LED ASR envelope renderer across all 12 LEDs (indexed 1..12 per
  Tildagon hardware). Chance gate rolled per-LED so a sparse
  `CHANCE_50` fire lights about half the ring; `CHANCE_100` lights all.
- Calm Mode (default on) caps at 50 % peak brightness and 2 Hz
  dispatch per architecture spec §15. Full mode lifts to 100 % / 4 Hz.
- `PatternDisable` emitted at app start (re-emitted on regained focus)
  so the badge's system patterndisplay service doesn't fight the
  renderer.

### Block 4: LCD pulse rendering

- Single full-screen ASR colour wash on the 240×240 LCD.
- Calm Mode disables LCD pulsing entirely per architecture spec §15.3
  ("screen flashing disabled"). Full mode caps peak at 60 % brightness
  - an uncapped face-distance flash is uncomfortably bright.

### Block 5: Persistent settings + in-app menu

- `Settings` (Calm Mode, group 0..3, channel auto/1/11) persisted to
  `/nocturnation_settings.json` outside the apps dir so deploys don't
  clobber operator preferences.
- In-app menu via `app_components.Menu`: button C opens, button F
  exits, selecting a line cycles its value.
- Class+group routing on inbound LIGHT_COMMAND per protocol manual §4.2
  and Epic 5 Q1/Q2: `target_class` 0/3 → both surfaces; 1 → perimeter;
  2 → LCD. `target_group` 0 is broadcast; otherwise must match.

### Block 6: NO SIGNAL, backgrounded operation, MUSIC_EVENT

- `SignalTracker` overlays `NO SIGNAL` in red after a 3 s frame gap per
  protocol manual §6.2. Initial state counts as lost until the first
  Director frame arrives.
- Perimeter LED tick moved into `background_task` so the ring keeps
  animating when the operator switches to another badge app
  (architecture spec §7.3). LCD goes idle automatically when
  backgrounded.
- `MUSIC_EVENT` (message_type 0x06) drives local synthetic fires:
  DROP → bright whiteout, BREAKDOWN → slow dim cool blue. Build is
  reserved and ignored.

### Testing

120 host-side pytest tests covering the protocol layer (frame parser,
dedup ring, channel scan, receive pipeline), both renderers (per-LED
envelope, brightness cap, frequency cap, primer/zero-duration edges),
settings model (coercion, JSON round-trip, corrupt-file fallback),
signal tracker, and MUSIC_EVENT synthesis. All passing on CPython
3.10+. Protocol modules are CPython + MicroPython compatible.

### Hardware bench-verified (2026-05-13)

On a Tildagon paired with an M5 StickC Director at EMF 2024 hardware,
channel 11:

- Block 1 hello-world app loads from the launcher.
- Block 2 ESP-NOW receive: Director kicks increment the frame counter
  and log the RGB triplet to serial.
- Block 3 perimeter ring pulses in sync with Director beats after the
  `PatternDisable` fix; no flicker.
- Calm Mode bench-confirmed to gate Rainbow test fires from the Director
  to discrete 2 Hz steps (correct behaviour per §15.1).
- Block 6 NO SIGNAL: powering off the Director mid-show surfaces the
  red NO SIGNAL overlay within 3 s; powering it back on clears the
  overlay and resumes rendering.
- Block 6 backgrounded behaviour: minimising the app and switching
  to another badge app keeps the perimeter ring animating; returning
  resumes cleanly without a re-init flicker.
- Block 6 MUSIC_EVENT: DROP fires the bright whiteout; BREAKDOWN
  fires the slow blue fade.
- Block 7 M5↔Tildagon interop session: Director Stick + Tildagon in
  radio range with DynamicShow running against real music; Tildagon
  perimeter ring visibly reacts in coordination.
- Block 7 battery drain: idle vs receiver-app over a 2 h window
  characterised; receiver-app drain is operator-acceptable for EMF
  show-night duty.
