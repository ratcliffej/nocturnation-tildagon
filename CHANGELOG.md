# Changelog

Notable changes to the NocturNation Tildagon receiver app. Versioning
matches `tildagon.toml`'s integer `version` field, which the EMF app
store treats monotonically rather than as semver.

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
