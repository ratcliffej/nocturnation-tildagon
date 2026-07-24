# Tildagon receiver: historical rationale

Design decisions and constraints that would otherwise clutter the code as
long comment blocks. Line references are approximate and won't stay
valid across edits, but the file names and the neighbouring identifiers
(constant, function, class) should be enough to relocate.

The code keeps a one-line WHY hint at each site; this file carries the
fuller reasoning for future me.

## app.py

### sys.path shim for the Tildagon launcher
**File**: `app.py` (top-of-module `_sys.path.append(_APP_DIR)` block)
**Context**: This app's own directory is not added to `sys.path` by the
Tildagon launcher. Modules use relative imports (`from .nocturnation.X`);
the Show library imports absolutely (`__import__("shows")` and
`from nocturnation.X import Y`).
**Rationale**: Shows must load identically under host pytest (where
`nocturnation` / `shows` are importable via `pyproject.toml`) and on the
badge. Deriving `_APP_DIR` from the launcher module name keeps the same
source working whether deployed via `deploy.sh` at `/apps/nocturnation`
or installed from the EMF app store at `/apps/nocturnation_tildagon`
(the store derives the dir from the repo name). Both places must have
`app.py` at the top of the tarball because the installer requires it.

### System-app killer / restorer
**File**: `app.py` (`_KILLABLE_SYSTEM_APP_CLASSES`, `_stop_other_system_apps`,
`_restart_other_system_apps`)
**Context**: The badge OS runs several scheduler tasks (hexpansion probe,
back-LEDs, pattern display, notifications, boop, launcher, power
manager, frontboard event pumps) that steal CPU + LCD + LED time from an
app that ticks at ~50 Hz and drives ESP-NOW simultaneously.
**Rationale**: Stop them on `__init__` and restart on `_quit` so a
punter's badge is left as we found it once they back out. Modelled on a
pattern shared by an EMF-app author for MusicJam. Frontboards must be
stopped first because they own the LCD backing store the other apps draw
onto; the scheduler releases their tasks then, so subsequent stops don't
fight for the backboard. `espnow_service` and `PowerEventHandler` are
deliberately NOT in the kill list: espnow_service backs our radio, and
the power event handler surfaces charge-state events we may want to see.
Every import is optional so host pytest still passes and older / newer
badge firmwares degrade gracefully.

### ESP-NOW IRQ-context RX timestamping + fast-relay
**File**: `app.py` (`_espnow_irq_handler`, `_espnow_last_arrival_ms`
group, `_pending_msgs` group)
**Context**: MicroPython `espnow.irq(handler)` fires shortly after a
frame arrives at the radio - much earlier than the asyncio receive-loop
poll can drain it, because the poll cadence baseline is ~85 ms (badge OS
/ LCD / other coroutines eat that time).
**Rationale**: Stamping arrival time in the IRQ handler and using it as
envelope `start_ms` decouples the visible pulse instant from
async-scheduler stalls. Two badges get matching `start_ms` values (radio
propagation < 1 ms) even when their poll loops hit the arrival at
different phases. IRQ context is restricted: no allocation, no
exceptions, tight code. `time.ticks_ms()` returns a smallint that
doesn't allocate on the heap, and writing to a pre-existing global int
is safe. If two frames arrive between two polls we only keep the LATEST
arrival stamp (fine for Test Pulse at 2 Hz).

The fast-relay path lets the IRQ handler TX before the async loop even
sees the frame. Buffers (`_pending_msgs`, `_relay_send_buffer`) are
module globals so the mp_sched-scheduled handler can access them without
touching a Python object attribute (which may involve a dict lookup
allocation). `_relay_send_buffer` is 32 B to match protocol
`kMaxFrameSize`; reused for every relay TX so the hot path stays
heap-free.

### LIGHT_PULSE fast-paint hot path (IRQ-side)
**File**: `app.py` (`_fast_paint_cb`, `_espnow_irq_handler` invocation,
`_fast_paint_pulse` method, `_fast_paint_last_key`, `_observe_frame`
LIGHT_PULSE `fp_handled` gate)
**Context**: Bench (post-EMF, docs in bench branches `bench-paint-delta`
+ `bench-irq-paint`) showed the async recv loop cost ~35 ms mean between
`arrival_ms` (peers_table / ESP-IDF stamp) and LEDs actually going on -
against the StickC's ~2 ms. paint_ms itself (I²C write + renderer tick)
was ~3 ms, so 32 ms of the delta was pure async-scheduler overhead:
`asyncio.sleep_ms(5)` between loop iterations, plus badge OS coroutines
(display refresh etc.) grabbing time between our yields. Reducing
`poll_ms` and adding yields made it *worse* (more preemption
opportunities for competing coroutines).
**Rationale**: mp_sched runs between Python bytecodes at ~1 ms latency,
regardless of what the async scheduler is doing. Moving the
sync-critical LIGHT_PULSE → perimeter LEDs path into the IRQ callback
brings Tildagon end-to-end to ~7 ms mean, closing the visual sync gap
with the StickC. Everything the fast path can't safely do (TOFU, full
dedup ring, LCD rendering, bookkeeping) stays on the async path. The
async path's perimeter render is skipped for pulses the fast path
handled via `_fast_paint_last_key` matching. Single-slot dedup in the
fast path drops the Director's 2× redundant TX (same key twice ~1 ms
apart). Exceptions inside the mp_sched callback are swallowed - a raise
in that context kills all scheduled callbacks, and the async path is
the fallback anyway. If a future badge OS release breaks mp_sched
compatibility, the fallback path continues to work with the (larger)
async delay.

### `_APP_VERSION` load
**File**: `app.py` (near `_APP_VERSION = "?"`)
**Context**: Read once from `metadata.json` (the on-badge runtime
artefact copied alongside `app.py` by `deploy.sh`).
**Rationale**: Displayed on the Lume searching-for-signal screen. Read
dynamically rather than baked as a literal so the value tracks
`metadata.json` without a separate constant to keep in sync. Fallback
path list because `__file__` semantics differ: host pytest has an
absolute path (rsplit gives the project dir); badge deploy places
`metadata.json` at `/apps/nocturnation/`, and depending on how the
launcher spawns the app, `__file__` may or may not include the full
path - try all candidates. Falls back to `"?"` so the failure surfaces
on the screen as `v?` rather than reading as a healthy value.

### Director channel + forbidden channels
**File**: `app.py` (`DIRECTOR_CHANNEL`, `DIRECTOR_FORBIDDEN_TX_CHANNELS`,
`DIRECTOR_SOURCE_ID`)
**Context**: The Tildagon must not broadcast Director on channel 11 (the
Performance band).
**Rationale**: Channel 11 is reserved for commercial / show Directors
with random-per-boot Performance-range source IDs. A swarm of Tildagons
at EMF transmitting on ch 11 would compete with the orchestrator's
StickC Director and drown out cue traffic. `DIRECTOR_CHANNEL = 1` is the
only authorised channel today; the explicit blocklist gives us a hard
runtime gate that catches any accidental change (future bug, fork
divergence, settings-file injection) before the radio is brought up.
`DIRECTOR_SOURCE_ID = 0x20` is a fixed community-range id; the Epic 5.5
random-per-boot allocation is a channel-11 concern only.

### Signal-loss fallback wash
**File**: `app.py` (`FALLBACK_*` constants group, `_evaluate_fallback`,
`_emit_fallback_*`)
**Context**: EMF-prep behaviour when a Director goes silent. Same
timings as the StickC `LumeMode` constants so the fleet converges on the
same idle effect at the same moment.
**Rationale**: 3 s silence surfaces the NO SIGNAL diagnostic;
10 s silence synthesises a `LIGHT_WASH` (blue<->purple ping-pong, muted
intensity); 40 s silence emits a synthetic `LIGHT_WASH_END` starting the
fade-to-black. `release_time` is u8 (~25.5 s cap), so the "30 s fade"
the operator asked for is served by the maximum the wash state machine
accepts - functionally equivalent to the eye. Any inbound frame cancels
the fallback with a short-release END so returning Director traffic
isn't fighting the synthetic baseline.

### Fleet render-tick alignment
**File**: `app.py` (`_render_snap_ms`, `_render_force_paint`,
`_observe_frame`, `_receive_loop`)
**Context**: Without alignment, envelope progression frames land at each
device's independent "local ticks_ms() paint phase" and cross-Lume sync
under high-cadence traffic drifts visibly.
**Rationale**: Every admitted frame is a shared physical event both
this device and every other Lume saw at the same wall-clock instant
(within ~1 ms via `peers_table`). Snapping the perimeter tick anchor to
`arrival_ms` keeps subsequent renders on a shared reference across the
fleet. `_render_force_paint` bypasses the 20 Hz gate on pulse-family
dispatches so colour transitions become visible within one poll cadence
of dispatch - critical for cross-Lume sync on Rainbow, DMX bridge, and
per-beat sparkles at DnB tempos. HEARTBEAT arrivals cover quiet
stretches when no LIGHT_PULSE would otherwise re-anchor.

### Q6 workaround: STA_IF first-config-after-active
**File**: `app.py` (`_bounce_radio`, `_scan_until_locked`,
`_director_session`)
**Context**: The badge's STA_IF layer only honours the FIRST
`wlan.config(channel=N)` call after each `active(True)` - subsequent
channel-sets raise RuntimeError 0xffffffff and the scanner freezes on
the first channel (usually 11).
**Rationale**: Bouncing STA_IF `active(False)`/`active(True)` resets
that counter, letting SCAN_ORDER (11 -> 1 -> 6) actually rotate.
`_bounce_radio` is lighter than `_release_radio` + `_acquire_radio`:
skips the badge_wifi restore/stop cycle and preserves scanner / TOFU /
signal-tracker state, which must survive across auto-scan channel
rotations. LR PHY + PM_NONE need re-applying because `active(False)`
throws them away; the ESP-NOW peer table survives the wlan cycle in
practice, but bouncing `esp.active` around it is safer against future
MicroPython builds tightening the coupling.

For Director sessions: on entry the FIRST-config slot might have been
consumed by a prior Lume-session scan, so the bounce ensures ch 1 is a
fresh first-config after `active(True)`. Peer re-add is needed because
`esp.active(False)` inside the bounce wipes the peer table; `add_peer`
is idempotent-with-OSError.

### Long-range PHY + PM_NONE
**File**: `app.py` (`_acquire_radio`, `_bounce_radio`)
**Context**: `wlan.config(protocol=8)` switches PHY to ESP-NOW Long
Range. Halves the bitrate (500 kbps vs 1 Mbps) in exchange for 2.5-7x
open-air range.
**Rationale**: Fleet-wide commitment - LR-only peers cannot decode
standard 802.11b/g/n peers, so every Director and Lume must enable this
together. Integer 8 = `WIFI_PROTOCOL_LR` in ESP-IDF v5.x. Passed as a
raw int rather than `network.WLAN.PROTOCOL_LR` because the badge's
MicroPython (5114f2c-dirty) has the protocol setter but not the named
LR constant; the setter accepts any int and forwards to
`esp_wifi_set_protocol()`. `wlan.config(pm=PM_NONE)` disables modem
power-save: the STA defaults to a duty-cycled PM mode that sleeps
through short ESP-NOW bursts (Director sends 2x retransmits within
~2 ms), so receivers must set PM_NONE for reliable receive. Raises idle
current; any future light-sleep work must keep the radio awake for
heartbeat windows or this bug returns.

### ESP-NOW rxbuf sizing + drain-until-empty
**File**: `app.py` (`_acquire_radio` `esp.config(rxbuf=8192)`;
`_receive_loop` `drain_limit = 16`)
**Context**: Default rxbuf (~526 bytes, ~13 messages) is too small for
high-rate effects (Rainbow at 10 Hz + 2x StickC redundant TX = 20 msg/s;
per-beat sparkles at 8+ Hz likewise) and can outrun our ~12 Hz poll
baseline.
**Rationale**: 8 KB gives ~200 messages of headroom. When the queue
fills, some MicroPython builds stop delivering entirely - burst of
correct renders followed by a hard cut. Draining until empty per poll
iteration (capped at 16 so a broken sender can't starve the
render/asyncio path) keeps the queue flowing.

### Debug overlay (Epic 15 bench follow-up)
**File**: `app.py` (`_draw_debug_overlay`, `_last_frame_ms`,
`_last_hop_count`, `_frame_window`, `_hops_seen`)
**Context**: Diagnostic LCD view enabled from the Settings menu, used to
objectively measure repeat-mode behaviour at range.
**Rationale**: Layout top-to-bottom - lock label, last frame age
prominent, frames/10s, hop count + live meter of further hops also
reaching us. Background tints by signal health band (green <1.2 s,
amber 1.2-2.0 s, red >2.0 s). "Hop: N (a b ...)" where (a b ...) are
hop levels GREATER than N observed this session; empty parens after a
known-relaying repeater has been firing = relay TX isn't reaching us.
Amber uses black text because yellow + white is unreadable in bright
daylight at arm's length. Diagnostic clarity beats aesthetics during a
range test.

Frames/10s tracks every frame (LIGHT_PULSE + LIGHT_WASH + heartbeats)
because real shows fire 4-8 LIGHT_PULSEs/sec at peak, so heartbeats-only
would understate traffic. `_hops_seen` flips True the first time a
frame at each hop arrives - any True at hop>=1 is cast-iron proof a
relay path reached us during this session. Duplicates still update the
diagnostic state (`_observe_frame` runs before the dedup gate) so the
overlay can show `Hop:1` when a relayed frame arrives even if it's a
dup of a direct `hop:0` we already rendered.

### DirID-keyed background image
**File**: `app.py` (`draw`, `_lcd_background_rgb01`, `bg_images`)
**Context**: Epic 13 Phase 2A: LCD background image (JPG) keyed off the
TOFU lock, via the documented `ctx.image()` API.
**Rationale**: The Tildagon Ctx reference documents `image(path, x, y,
w, h)` as the supported path for JPG/PNG. Ctx caches the decoded image
internally by path so we can call it every frame without re-decoding.
An unknown DirID falls back to a bundled default logo, and missing
default means "no image, paint the solid colour underneath as before".
Once locked, the Lume LCD becomes a content surface (mirrors the
StickC's "LCD is content in Lume" role); the diagnostic HUD is
suppressed then because brand mark / channel / frame count would be
noise against operator-paced text content. NO SIGNAL still surfaces as
a small footer because radio liveness matters even when a lyric is on
the screen.

### Foreground pop/push semantics
**File**: `app.py` (`_on_foreground_push`, `_on_foreground_pop`)
**Context**: Perimeter LEDs continue animating when the app is
backgrounded (architecture spec section 7.3). The renderer keeps
ticking via `background_task`.
**Rationale**: On push we keep the perimeter renderer state (was still
animating), clear the LCD renderer's envelope (was dispatched but not
drawn) so the wash starts cleanly from the next fire rather than
mid-envelope, and re-inhibit patterns defensively in case another app
emitted `PatternEnable` while we were away. On pop we keep
`PatternDisable` in effect so the badge's patterndisplay service
doesn't fight us for the LED ring, and let the receive + render loop
keep running. The LCD goes idle automatically - the OS routes `draw()`
calls to the new foreground app.

### Auto-scan Q6 fallback
**File**: `app.py` (`_scan_until_locked`)
**Context**: If STA_IF rejects channel config even after a bounce,
something deeper is wrong.
**Rationale**: Fall back to receive on whichever channel was last
successfully set (often the first scan target). Preserves the pre-Q6-
workaround behaviour as the ultimate fallback. Operator gets a
"no-scan" status line telling them which channel to align the Director
Stick to.

### `_wash_max_hold` failsafe (mirrored on perimeter)
**File**: `app.py` inbound path; `nocturnation/render/perimeter.py`
`WASH_MAX_HOLD_MS`
**Context**: A LIGHT_WASH with `ttl_seconds == 0` is "infinite" per
spec: it holds until an explicit LIGHT_WASH_END arrives. If that frame
is lost, a wash with `pulse_response = 0` also gates PULSE - so the
Lume sits unresponsive forever.
**Rationale**: After `WASH_MAX_HOLD_MS` (30 min) the receiver
self-releases the wash so a missed WASH_END eventually recovers.
Mirrored between the LCD renderer and perimeter renderer.

### Director TX heartbeat pip
**File**: `app.py` (`_draw_director`, `_director_last_tx_ms`)
**Context**: Small 10x10 pip in the top-right of the Director LCD whose
colour is derived from the age of the last successful `esp.send()`.
**Rationale**: Gives the operator a visual "the radio is actually
broadcasting" cue without hooking up USB serial. Green < 200 ms, dim
green < 2000 ms, red beyond (or never). 2 s outer threshold matches the
1 Hz heartbeat cadence with margin. Position (60, -85) with 10x10 keeps
all four corners under radius 105, well inside the 120 visible arc of
the round display - previous position (95, -110) landed at radius 145
(completely off-screen).

### Show-error boundary
**File**: `app.py` (`_draw_director`)
**Context**: A crashing Show must not take the UI down with it.
**Rationale**: Third-party Show code is a system boundary. The catch
paints a black background with a red "show error" line so the operator
can back out to the picker.

### QR code sizing
**File**: `app.py` (`_draw_help`)
**Context**: The round 240 px screen inscribes a ~170 px square
(240/sqrt2), and a QR's corner finder patterns must stay on-screen to
scan.
**Rationale**: Floor the module size to keep the whole code within
170 px. The shkspr.mobi blog's "+1" can overshoot for longer URLs and
clip the corners under the bezel.

## nocturnation/repeater.py

### FSM overview
**File**: `nocturnation/repeater.py` (module docstring)
**Context**: Epic 17 dynamic repeater FSM. Runs on each Tildagon in
Lume mode when Repeat=AUTO.
**Rationale**: States:

- LISTENING - default; sets a 100 ms peer-watch on each admitted frame
  (hop < 3) that arrives at the LOWEST hop we hear it at. A frame we
  can also hear closer to the source arms no watch - we're in its
  coverage core, not at an edge that a next-hop relay would serve. Any
  single "uncovered" watch (deadline passes with no matching peer relay
  at hop+1) -> CANDIDATE.
- CANDIDATE - election pending; counts down X frames (random 2-9). Any
  peer relay observed during the count -> LISTENING. Counter elapses ->
  ACTIVE.
- ACTIVE - relays every admitted first-seen frame at hop+1. Records
  (src, seq, hop_txed) in a ring so a peer's frame matching on all
  three signals we are redundant -> COOLDOWN.
- COOLDOWN - still relaying, but assessing peer. Counts X frames
  (random 5-20). If peer relays stop (no ring match for
  ~3 director-frames) -> ACTIVE. If X elapses with peer still active
  -> LISTENING.
- DISABLED - Repeat=OFF from Config; observer not wired, FSM never
  transitions. Not a runtime state - this is what "no FSM instance"
  looks like from the app's POV.

ESP-NOW broadcast does NOT loop back on this platform (bench-confirmed
via `tools/espnow_loopback_probe.py`), so any received frame matching a
TX-ring entry is by construction from a peer device.

### `_output_hop` invariants
**File**: `nocturnation/repeater.py` (`_output_hop`, `_on_frame_candidate`,
`_on_frame_active`, `_on_frame_cooldown`)
**Context**: Elected output hop preserved through CANDIDATE -> ACTIVE
-> COOLDOWN chain, reset in `_to_listening`.
**Rationale**: Two load-bearing rules:

1. CANDIDATE cancel triggers only on peer traffic at this hop (a hop 2
   candidate ignores hop 1 traffic). Covering ANY watch previously
   fired the cancel, causing LISTENING <-> CANDIDATE oscillation when
   an engineered hop 1 repeater was active but hop 2 was still vacant.
2. ACTIVE / COOLDOWN relay ONLY frames where hop+1 matches this hop -
   a hop 2 elected device produces hop=2 only, ignoring hop=0 inputs
   so it doesn't compete with an engineered hop 1 repeater at hop=1.

### Lowest-hop watch gating
**File**: `nocturnation/repeater.py` (`_note_hop_should_watch`,
`_recent_lo`, `_recent_lo_order`)
**Context**: A watch at hop H is the seed of hop (H+1) self-election.
We only arm one at the LOWEST hop we personally receive a frame at.
**Rationale**: If we can also hear the same frame closer to the source,
an outer relay hop from us serves nobody we don't already reach.
Electing anyway strands us at an outer hop with no peer to hand off to
and almost no fresh input to relay - a badge beside a StickC hop 1
repeater parked in ACTIVE(hop 2), tx crawling, px stuck at 0, never
standing down. `seq == 0` ("no sequencing") can't be tracked per-frame,
so it keeps the prior always-watch behaviour.

### ACTIVE/COOLDOWN idle timeout
**File**: `nocturnation/repeater.py` (`ACTIVE_IDLE_TIMEOUT_MS`, `tick`)
**Context**: 3 s of no successful relay -> step down to LISTENING.
**Rationale**: An elected role becomes obsolete when upstream at our
input hop stops producing traffic. Without the timeout, an ACTIVE(hop 2)
device stayed stuck when its hop 1 upstream (StickC) was turned off:
received hop=0 from Director but was out of role to relay it. 3 s
approx 3 heartbeat cycles; long enough to bridge a normal quiet gap,
short enough for responsive recovery.

### CANDIDATE / COOLDOWN counter ranges
**File**: `nocturnation/repeater.py` (`CANDIDATE_MIN_FRAMES` group,
`COOLDOWN_MIN_FRAMES` group)
**Context**: Random per-election windows.
**Rationale**: CANDIDATE min = 2 gives a settling period that absorbs
the ~2% single-frame peer-relay loss; probability of two consecutive
failed peer relays from a healthy peer is 0.04%. COOLDOWN can be longer
because we're already relaying during the assessment - no service
interruption during cooldown, so we can afford a wider window.

### Peer-watch and TX ring sizing
**File**: `nocturnation/repeater.py` (`WATCH_RING_SIZE`, `TX_RING_SIZE`,
`RECENT_HOP_MEMORY_SIZE`)
**Context**: All three bounded rings; sized so we never lose a pending
entry under normal traffic.
**Rationale**: 32 (watches, tx) sized larger than the in-flight frame
count (retransmits + a few frames of scheduling latency). 64 for
recent-hop memory (tracks (src, seq) hop history so lower-hop copies
can supersede higher-hop watches without unbounded growth).

### `_transmit_relay` with `skip_tx`
**File**: `nocturnation/repeater.py` (`_transmit_relay`, `_skip_tx`)
**Context**: When True, the IRQ handler in app.py already TX'd; we only
run bookkeeping.
**Rationale**: The IRQ-context handler mutates hop_count and calls
`esp.send()` at radio-callback latency (~5 ms) rather than at
async-poll latency (~85 ms). Duplicating the TX here would double
airtime per hop for no benefit. Bookkeeping (`_record_tx`,
`_relayed_count`, `_last_relay_ms`, cooldown timers) still runs so peer
detection and self-diagnostics stay accurate. When irq isn't installed
(older firmware) `skip_tx` stays False and this is the sole TX path.
Copy `raw_buf` first when we do TX so we don't corrupt the caller's
buffer.

## nocturnation/render/perimeter.py

### Frequency caps
**File**: `nocturnation/render/perimeter.py` (`CALM_MIN_INTERVAL_MS`,
`FULL_MIN_INTERVAL_MS`)
**Context**: Minimum ms between accepted dispatch calls per Calm Mode.
**Rationale**: Calm mode keeps the 500 ms (2 Hz) Harding-safe floor for
badges worn by non-consenting audience. Full mode was 250 ms (4 Hz) but
that silently dropped every other sparkle at 140 BPM
(`sparkle_on_beat` at 7 Hz = 143 ms gap); raised to 60 ms (~16 Hz) so
per-beat sparkles land through 200+ BPM and sub-beat sparkles still
fit. The cap exists to guard against pathological back-to-back
dispatches, not to throttle legitimate music tempo.

### Pulse attack lerp source
**File**: `nocturnation/render/perimeter.py` (`dispatch`,
`_pulse_color_at`)
**Context**: Attack phase lerps from the LED's current rendered colour
(`_last_rendered`) to the new pulse's target.
**Rationale**: Epic 6C Phase G ADR fix. Pre-fix behaviour ramped from
brightness 0, so a series of rainbow pulses snapped through black
between colours. This fix produces smooth crossfades when attack > 0;
T_0_MS attacks still snap, by design. Release fades back to the wash
baseline (rather than to black) so pulses over a wash-active LED
integrate visually.

### Wash `_wash_baseline_at` attack/release lerp math
**File**: `nocturnation/render/perimeter.py` (`_wash_baseline_at`)
**Context**: Attack lerps from `pre_wash_*` to `(post_r, post_g,
post_b)`. Release lerps from `pre_wash_*` to `release_end_*`.
**Rationale**: Use the pre-cap `pre_wash` values as the lerp source so
the intensity + brightness cap already applied inside `post_*` don't
double-apply. When a wash is superseded in flight, we capture the
instantaneous baseline as `pre_wash_*` so the transition is visually
continuous.
