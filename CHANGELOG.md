# Changelog

Notable changes to the NocturNation Tildagon receiver app. Versioning
matches `tildagon.toml`'s integer `version` field, which the EMF app
store treats monotonically rather than as semver.

## 2026-07-12 — v1.0.0: Calm Mode default OFF, tagline + version footer on searching screen, Conductor section-cycle gesture

Polish release ahead of EMF. Operator-visible changes:

- **Searching-for-signal screen** now displays the tagline
  `Open-source crowd lighting` in place of the internal `scanning`
  status text, and no longer shows the running frame count / FSM state
  label. Punters glancing at an unlocked badge see brand + channel /
  lock status, nothing else. Operator diagnostics (frames, FSM state)
  remain available on the Debug Overlay (`Settings > Debug`). A small
  version footer (`vN.N.N`) is drawn below the button hints, read
  dynamically from `metadata.json` at module import so the number
  tracks the manifest without a duplicate constant to keep in sync.
- **Conductor Show: long-press LEFT / RIGHT cycles Section** without
  opening the Settings overlay. Short-press keeps its palette-cycle
  role; the 500 ms hold threshold arbitrates. `SECTION_NEXT` /
  `SECTION_PREV` added to `InputAction`; `DirectorButtonMapper` gained
  release-fire + hold-duration detection on LEFT / RIGHT (UP / DOWN
  stay rising-edge). The Conductor's on-screen button hint updated to
  `< >: palette  hold: section`.
- **Calm Mode default flipped OFF** (`Settings.calm_mode` default
  `True` → `False`). A fresh install renders the authored show at
  full authored brightness on both the perimeter LEDs and the LCD
  wash; the operator opts INTO Calm Mode via the in-app menu for
  photosensitivity-sensitive contexts. Existing settings files with
  `calm_mode: true` explicitly saved still load `True` — only fresh
  installs and files missing the key see the new default.
- **Version 0.9.0 → 1.0.0** (`metadata.json`); app-store manifest
  `tildagon.toml` monotonic version `9` → `10` so the EMF App Store
  picks it up as a new release.

- **Director-mode Q6 workaround.** The badge's STA_IF layer only
  honours the first `wlan.config(channel=N)` call after `active(True)`;
  subsequent channel-sets raise `RuntimeError 0xffffffff`. If a user
  ran Lume first (whose auto-scan sets ch 11 as its first channel)
  then switched to Director, the `wlan.config(channel=1)` call
  silently failed and the badge broadcast on ch 11 — invisible to any
  Lume because StickC's `tofu_lock.cpp` filters community-range source
  IDs on ch 11 (that's the "misconfigured Director on wrong channel"
  guard). Director mode now releases and re-acquires the radio at
  session start so `channel=1` is the fresh first-config call, and
  verifies via `wlan.config("channel")` read-back that the physical
  channel actually landed on 1 — aborting to idle with a visible
  status message if it didn't. Fixes the "Atom can't see Tildagon
  Director after Lume mode" bug reported 2026-07-12.
- **Director TX heartbeat pip.** Small 8×8 pip drawn top-right of the
  LCD in Director mode. Bright green for 200 ms after each successful
  `esp.send()`, dim green for up to 2 s (~healthy 1 Hz heartbeat
  cadence), red thereafter. Gives the operator a "the radio is
  actually broadcasting" cue without hooking up USB serial.

All 561 host tests pass (0 skipped).

## 2026-07-02 — Terminology: drop "shell N" for "hop N" in current code

The Epic 17 design used "shell N" and "hop N" for what turned out to
be the same concept — a device that repeats at hop N is a "hop N
repeater"; there was no separate meaning "shell" was carrying that
"hop" didn't already. Live code (`nocturnation/repeater.py`,
`tests/test_repeater.py`) and operator-facing docs (README, user
manual) are now consistent on "hop N". Historical CHANGELOG entries
below are left as-is — they're a record of what shipped when, and
the shell/hop split was part of the design vocabulary of that day.
Test names renamed (e.g. `test_shell_2_candidate_not_cancelled_by_shell_1_peer`
→ `test_hop_2_candidate_not_cancelled_by_hop_1_peer`). No behavioural
change; 561 host tests pass.

## 2026-07-01 — Epic 17 fix: no outer-shell election from the coverage core

Bench-found: a Tildagon sitting beside a StickC shell-1 repeater, both
in direct earshot of the Director, parked in `ACTIVE(shell-2)` — `tx:`
crawling, `px:` stuck at 0 — and never stepped down to LISTEN. The
StickC's continuous hop=1 covers the badge's hop=0 watches (suppressing
shell-1 election as intended) but nothing covers the hop=1 watches, so
the badge elected itself shell-2 to relay hop=1→hop=2. In a co-located
cluster there's no shell-2 audience to serve (nothing to relay) and no
shell-2 peer to hand off to (never stands down).

The FSM now arms a peer-watch only at the LOWEST hop it hears a given
`(src, seq)` at. A device that also receives a frame closer to the
source is in the coverage core, not at an edge, so it no longer seeds
an outer-shell election. Legitimate cascade is preserved: a badge that
hears a frame ONLY at hop=1 (walked out of the Director's direct range)
still elects shell-2. Even in the out-of-order case (relayed copy beats
the direct copy) the worst outcome is a shell-1 election, which is
self-correcting via peer detection — never the unresolvable shell-2.

- `nocturnation/repeater.py` — `_note_hop_should_watch` records the
  lowest hop per recent `(src, seq)` and gates `_add_watch`;
  `_cancel_watches_above` retires higher-hop watches when a lower-hop
  copy arrives out of order. New `RECENT_HOP_MEMORY_SIZE` ring bound.
- `tests/test_repeater.py` — core-cascade tests reframed to the
  corrected behaviour; added core-stays-LISTEN, edge-still-elects, and
  out-of-order regression cases.

## 2026-07-01 — Epic 17: dynamic repeater (B0-B3)

Audience-Tildagon dynamic-repeater FSM. A Tildagon in Lume mode with
Repeat=AUTO silently becomes an ESP-NOW relay when it observes no
peer covering its vicinity, and steps down when a peer takes over.
Enables self-organising cascade coverage up to 3 shells deep through
the audience, mitigating the EMF Stage D body-absorption problem where
crowd density can shadow the far half of the audience from the
Director. Default AUTO — engineered StickC repeaters (Epic 15) send
continuous hop=1, which naturally suppresses audience election, so
AUTO is safe as a default even in fully-covered deployments.

- `nocturnation/repeater.py` — `DynamicRepeater` FSM: LISTENING /
  CANDIDATE / ACTIVE / COOLDOWN. Per-output-hop peer detection via
  `(src, seq, hop_txed)` ring buffer. Aggressive LISTENING → CANDIDATE
  trigger (any single uncovered frame) balanced by 2-9 frame CANDIDATE
  settling period that absorbs ~2 % transient peer-relay loss.
- `app.py` — `_repeater_observer` hook in `_observe_frame` fires for
  every admitted frame (first-seen + duplicate). `_start_repeater` /
  `_stop_repeater` instantiate + tear down around `_receive_loop`;
  FSM tick runs in the poll loop alongside `_evaluate_fallback`.
- `nocturnation/settings.py` — `repeat: AUTO / OFF` field, persisted
  in `/nocturnation_settings.json`. Config menu entry between Debug
  and Rescan.
- LCD indicators: state char (`-` `?` `*` `~`) appended to the
  standard HUD's frames line; dedicated `R:<state> tx:<n> px:<n>`
  row on the debug overlay for bench observation.
- `tools/espnow_loopback_probe.py` — bench-only script that verifies
  ESP-NOW broadcast does NOT echo back to sender. LOOPBACK: NO
  confirmed 2026-07-01 across 10 iterations; the FSM's peer detection
  relies on this assumption.
- 27 new host tests (`tests/test_repeater.py` + `tests/test_settings.py`
  additions) cover FSM state transitions, peer detection, watch
  expiry, ring bounds, LCD indicator, and Repeat setting.
- Docs: Epic 17 spec at `Docs/epics/epic-17-dynamic-repeater.md`
  (working copy; Notion sync at Epic close).

Bench fixes 2026-07-01:

  1. CANDIDATE cancel logic tracked only "any covered watch",
     causing LISTENING ↔ CANDIDATE oscillation when a shell-1 peer
     (e.g. an engineered StickC repeater) covered shell-1 watches
     while the FSM was actually electing for shell-2. Fix tracks
     `_output_hop` on CANDIDATE entry and cancels only on peer
     traffic at that specific hop.
  2. ACTIVE relayed ANY input frame with hop<3 regardless of the
     shell it was elected for. A shell-2 elected device would still
     TX hop=1 for hop=0 inputs, competing with an engineered shell-1
     repeater and causing ACTIVE→COOLDOWN→LISTENING→CANDIDATE
     re-election churn. Fix: ACTIVE/COOLDOWN relay ONLY where
     `hop + 1 == _output_hop`. A shell-2 role produces hop=2 only,
     ignoring hop=0 inputs entirely. `_output_hop` is preserved
     through the CANDIDATE → ACTIVE → COOLDOWN chain; reset only
     on transition back to LISTENING.

Five regression tests added across TestBenchScenario20260701 and
TestActiveRelay for role-specific relay + shell-1/shell-2 ignore
of out-of-role inputs.

UI clarity 2026-07-01: replaced single-char state indicators with
explicit labels (LISTEN / LISTEN? / REPEAT / CDOWN) on both the
standard HUD and the debug overlay. Debug overlay's `Tot: N` line
dropped in favour of a prominent state label at font 20 with
`tx: N px: N` counters below - the state is the primary field
diagnostic during a bench walk-out and deserves the space.

  3. ACTIVE/COOLDOWN devices got stuck when their upstream disappeared.
     A Tildagon in ACTIVE(shell-2) receiving hop=1 from a StickC-shell-1
     repeater would keep its ACTIVE state even after the StickC was
     turned off - the FSM's transition-out-of-ACTIVE path only fired
     on peer collision, not on "no more input at my role's hop". Fix:
     ACTIVE_IDLE_TIMEOUT_MS = 3000 ms. If no relay has fired in that
     window, step down to LISTENING and let re-election pick a new
     role (typically shell-1 in this scenario). Applied in tick(), so
     the transition fires without needing a new frame arrival. Three
     regression tests added in TestIdleTimeout.

Not yet done: B4 field bench test with bodies (needs 3 Tildagons +
volunteers in a park), B5 tuning pass. Target ship: 2026-07-12
for EMF 2026-07-16 (4-day slack).

## 2026-05-24 — Repo restructure for app-store packaging

Moved the app payload (`app.py`, `metadata.json`, `uQR.py`, and the
`nocturnation/` and `shows/` packages) from `apps/nocturnation/` to the
**repo root**. The EMF app-store installer requires `app.py` at the top of
the release tarball and does not recurse into subdirectories, so the
previous nested layout could not be installed from the store. `app.py` now
derives its own directory from the launcher module name and adds it to
`sys.path`, so the Show library's absolute imports resolve whether the app
is installed to `/apps/nocturnation` (via `deploy.sh`) or
`/apps/nocturnation_tildagon` (via the app store). `deploy.sh`, the pytest
path, and the docs are updated to match. No behavioural change; all 399
host tests pass.

## 2026-05-21 — Epic 6B: Tildagon Director + Show framework

The badge becomes a NocturNation **Director** (not just a Lume): an
operator can run a show that broadcasts `LIGHT_COMMAND` frames driven
by IMU tap-to-beat, alongside the existing receive-only Lume mode. A
MicroPython Show plug-in framework mirrors the M5 firmware's `Show`
surface so the same authoring model applies on both hosts
(`docs/developing-shows.md` in the M5 repo).

Show framework + Director runtime:

- `nocturnation/hal/` — `Capability` enum + `CapabilityMask` (1:1 with
  the C++ `hal::Capability`), incl. the Epic 6B `IMU_TAP` / `IMU_MOTION`
  sub-capabilities.
- `nocturnation/plugins/` — `Plugin` base, `PropertyDef` /
  `PropertyType` / `PowerProfile`, and `PropertyBag` (JSON-backed,
  `/nocturnation_plugins.json`, one section per plug-in id).
- `nocturnation/shows/` — `Show` base (analyser + IMU + input + render
  hooks), `ShowContext` (render_fx / properties / caps / display),
  `InputAction`, and a folder-per-show registry (`discover_shows()`
  walks `apps/nocturnation/shows/<id>/`).
- `nocturnation/director/` — `RenderDispatcher` (one `render_fx` ->
  ESP-NOW broadcast + local perimeter/LCD loopback + 1 Hz HEARTBEAT
  beacon + 3x redundancy), `DirectorHost`, `ImuAdapter` (gravity-EMA
  high-pass tap + motion, sensitivity-scaled), `ButtonTapSource`
  (button-as-tap fallback), `DirectorButtonMapper`, and
  `DirectorController` (active-show lifecycle / input routing /
  per-Show sensitivity / tick).
- Reference Shows: `simple_tap` (tap-to-beat, palette incl. Rainbow)
  and `motion_wave` (axis-coloured motion), under
  `apps/nocturnation/shows/`.

App + lifecycle:

- **Idle start menu** on launch (Lume / Director / Settings / Help /
  Quit) with WiFi up. A mode is started explicitly; only then is the
  radio taken.
- **WiFi/ESP-NOW coexistence**: the app calls the badge `wifi.stop()`
  when entering an active mode (an unassociated STA makes the ESP32
  firmware channel-sweep, which breaks ESP-NOW), and restores WiFi
  (`wifi.connect()`) on returning to idle / Quit. So WiFi is only down
  while a Lume/Director session is running (foreground or background;
  the lights are the visual cue).
- **Channel** setting now pins a single channel (1/11) rather than the
  auto-scan that mis-locked on stray frames.
- **Quit** releases the radio, restores WiFi, hands the LEDs back, and
  cleanly terminates (relaunch starts fresh).
- **Help** screen renders a QR code (vendored `uQR.py`, MIT, from
  `JASchilz/uQR`) to a configurable `Settings.help_url` (default
  `http://www.nocturnation.net`).
- Settings gained `mode`, `active_show`, `help_url`.
- Director transmits on **channel 1 only** (Epic 5.5: the Tildagon must
  not broadcast on the channel-11 Performance band).
- `deploy.sh` strips host `__pycache__` before copying so only `.py`
  source reaches the badge.

Bench-verified M5 <-> Tildagon interop (Director taps light a Plus2
Lume's bracelet via IR, and the Tildagon Lume receives M5 broadcasts).
Host suite 399 tests. Wire format unchanged; `protocol_version` stays
`0x02`.

## 2026-05-17 — Epic 5.5: channel 11 access control (source_id partition + TOFU)

Lume-side implementation of the channel 11 access control mechanism
landing in the companion M5 firmware repo (`ratcliffej/nocturnation-m5`).
**No wire-format change**; `protocol_version` stays at `0x02`. The
Tildagon now applies a Trust-On-First-Use (TOFU) lock to the first
valid frame from a non-broadcast source_id, dropping subsequent frames
from any other source for the duration of the session.

Behaviour:

- New `nocturnation/tofu.py` module with `TofuLock` class.
  `admit(frame, channel, now_ms)` runs between dedup and observation;
  `tick(now_ms)` each loop iteration expires the lock on extended
  silence; `clear()` is the operator-initiated rescan hook.
- New `nocturnation/protocol/source_id.py` module with the partition
  constants (`SourceId.COMMUNITY_MIN/MAX/PERFORMANCE_MIN/MAX/BROADCAST`)
  and helper predicates `is_community_range` / `is_performance_range`.
- Cross-range filter: on channel 11, only Performance-range source_ids
  (`0x40-0xFE`) are eligible to be locked; community-range ids on
  channel 11 are silently dropped without locking. Channels 1 and 6
  accept any non-broadcast source_id. Broadcast (`0xFF`) is never
  eligible.
- Lock timeout: 10 s (`DEFAULT_TIMEOUT_MS`, mirrors the M5 firmware's
  `kRescanMs`). When the lock expires the next valid frame establishes
  a fresh lock.
- UI: LCD status line now shows lock state using the same `C:nn` /
  `P:nn` convention as the M5 firmware:

  | Label | Meaning |
  |---|---|
  | `ch N scan` | scanner hunting for any Director |
  | `ch N listen` | channel locked, no TOFU peer yet (post-rescan / post-timeout) |
  | `ch N C:nn` | TOFU locked to a community-range source |
  | `ch N P:nn` | TOFU locked to a Performance-range source |
  | `ch N ?:nn` | defensive: out-of-range source (shouldn't happen) |

  Audience members can verify they're locked to the right Director
  by visual comparison with the Director's screen.
- Settings menu: new "Rescan" item between "Channel" and "Back".
  Selecting it calls `tofu.clear()` so the next valid frame on the
  current channel establishes a fresh lock. The Tildagon's radio
  doesn't support reliable channel re-scan post-boot (Epic 5 Q6
  caveat), so this is a TOFU-only reset; the wifi channel stays
  where it was.

Spec deviation captured inline in the M5 repo's protocol-manual.md
§3.4 + §7.1: TOFU locks on any valid frame, not specifically the
first HEARTBEAT. The HEARTBEAT-only formulation from the initial
v0.29 draft didn't compose with skip-if-recent heartbeat suppression
during active music - a Lume joining mid-song would otherwise sit
idle for the duration of the song.

157/157 host-side pytest tests pass on CPython 3.10+ (130 -> 157,
+27 from the source_id partition tests in B2 and the TOFU tests in
B6+B7). Protocol modules remain CPython + MicroPython compatible.

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
