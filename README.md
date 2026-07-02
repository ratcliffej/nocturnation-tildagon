# NocturNation — Tildagon app

> Open-source crowd lighting, on the EMF Tildagon badge.

> ⚠️ **Photosensitivity warning.** NocturNation flashes the badge's perimeter LEDs in time with music — and, in Full mode, the round screen too. Flashing light can trigger seizures in people with photosensitive epilepsy, and the badge sits close to your face. **Calm Mode is ON by default** (reduced brightness, 2 Hz cap, screen flashing disabled), and we strongly recommend leaving it on, especially in a crowd. If you have photosensitive epilepsy or are sensitive to flashing light, please take care — keep Calm Mode on, or skip this app. NocturNation is an open-source hobby project, not a certified safety or medical device; you use it at your own risk.

NocturNation turns a crowd into a light show: a **Director** listens to music, detects the beat, and broadcasts light commands over ESP-NOW to a swarm of **Lumes** that light up in time. This repository is the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/) app — it lets a badge join a NocturNation show in two ways:

- **Lume (receiver).** The badge listens for a Director's light commands and animates its twelve perimeter LEDs and round LCD in sync with the music. **Calm Mode** is on by default, so it is comfortable to wear straight out of the box.
- **Director.** The badge runs the show itself: tap it to the beat (the IMU turns a sharp tap into a beat) or press a button, and it fires a pulse to its own LEDs *and* broadcasts to any other badges and bracelets in range. A group of people can tap together and watch the room flash as one — useful when there is no Director Stick nearby. Director transmits on the hobby channel only, so it never competes with a venue's official show.

It is a fresh MicroPython codebase, not a port of the [M5 Stick firmware](https://github.com/ratcliffej/nocturnation-stickc). The two share only the on-wire [NocturNation ESP-NOW protocol](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/protocol-manual.md) — different chip (ESP32-C3), different language (MicroPython), different SDK (Tildagon OS).

## Documentation

The NocturNation manuals live in the [**nocturnation-docs**](https://github.com/ratcliffej/nocturnation-docs) repository:

- [Protocol manual](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/protocol-manual.md) — **the implementation specification for this repo.** Every byte the badge sends and receives is defined here.
- [User manual](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/user-manual.md) — operator-facing behaviour the badge matches where applicable.
- [Developer guide](https://github.com/ratcliffej/nocturnation-docs/blob/main/developing-shows.md) — how to write a Show; the same authoring model applies on the badge and the M5 Sticks.
- [Flow diagrams](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/flow-diagrams.md) — the receive pipeline and class-and-group routing as Mermaid diagrams.

## What the app does

**As a Lume:**

- Parses ESP-NOW `LIGHT_COMMAND` frames with a deduplication ring and hop-count guard; pins to channel 1 or 11.
- Renders per-LED attack/sustain/release envelopes on the perimeter ring (each LED chance-gated independently) and a soft colour wash on the round LCD.
- Shows **NO SIGNAL** after a 3-second gap, and renders `MUSIC_EVENT` cues (drop, breakdown) as local effects.
- Keeps animating when backgrounded, so the lights continue while you use another badge app.

**As a Director:**

- Turns IMU taps (or a button press) into beats and broadcasts `LIGHT_COMMAND` frames with 3× redundancy plus a 1 Hz heartbeat, while driving its own LEDs and LCD through the same path.
- Ships two reference Shows — **simple_tap** (tap-to-beat with a colour palette, including a per-tap Rainbow) and **motion_wave** (movement axis picks the colour, magnitude sets brightness).
- Transmits on **channel 1 only**, keeping the channel-11 Performance band clear for an official Director.

**Throughout:**

- A MicroPython **Show plug-in framework** that mirrors the M5 firmware's `Show` surface — folder-per-show, auto-discovered, with persisted per-show settings.
- **Calm Mode** default-on (frequency and brightness caps, LCD flashing disabled) for photosensitivity safety; opt in to full effects in Settings.
- An idle start menu (**Lume / Director / Settings / Help / Quit**). WiFi stays up while idle and the radio is taken only once a mode starts, so ESP-NOW and the badge's WiFi coexist cleanly. **Help** shows a scannable QR code to the project site.
- Trust-On-First-Use source locking and Performance-band filtering, so the badge follows a single trusted Director (see the protocol manual's access-control section).
- **559 host-side tests**, all passing, including byte-level parity against the protocol manual's reference vectors.

## Repeat Mode

**Dynamic repeat** turns audience Tildagons into an opportunistic ESP-NOW mesh. On open-air stages, human bodies attenuate the radio signal enough that devices at the back of a dense crowd can drop frames from a Director on stage. When a Tildagon in Lume mode finds itself at an **unserviced edge of the mesh** — receiving signal at one hop level with no relay pushing it further out — it silently steps up as a repeater. When a peer takes over, or when the badge finds itself back in the coverage core, it steps back down. The mesh self-organises without operator input.

**How hop numbering works.** The Director broadcasts at hop 0. Each relay increments the number by one: a hop 1 repeater retransmits its hop 0 input as hop 1, a hop 2 repeater retransmits hop 1 traffic as hop 2, and so on. The wire spec caps at hop 3, so the mesh can extend at most three relay hops deep through the audience.

**The edge rule.** A badge only elects if it can't hear the same frame at a lower hop. If it hears both the Director directly (hop 0) *and* a relay of that frame (hop 1), it's in the coverage core — adding another relay from there serves no one who isn't already reached — so the badge stays LISTEN. Only edge badges cascade the mesh further out. Recovery from a repeater walking out of range takes 3-9 seconds.

**Configuration:** Config → **Repeat: AUTO** (default) enables dynamic repeat; **Repeat: OFF** disables it entirely. There is no per-badge setup — the default just works.

**LCD status on the standard HUD:**

- **LISTEN** — passively observing, no vacancy detected.
- **LISTEN?** — vacancy noticed, election pending.
- **REPEAT** — actively relaying signal.
- **CDOWN** — peer contention detected; still relaying while assessing whether to step down.

Enable **Debug: ON** in Config to see the state prominently on the debug overlay alongside `tx:` (frames relayed by this badge) and `px:` (peer frames observed).

### Examples

**1. Solo Tildagon in front of a Director.** The badge hears direct hop 0 traffic with no peer at hop 1 → within ~1-9 seconds it elects **REPEAT** and relays every incoming frame at hop 1. Coverage now extends beyond the Director's direct range.

**2. Tildagon at the back of the audience, with a StickC repeater between it and the Director.** The badge is out of the Director's direct range but hears the StickC's hop 1 relay. Since it hears the frame at only one hop level, it's at an edge → it elects **REPEAT** at hop 2, extending coverage further into the back of the audience.

**3. Tildagon in the coverage core (Director *plus* a StickC repeater both within earshot).** The badge hears the Director's hop 0 direct AND the StickC's hop 1 relay of the same frame → it's in the core, not at an edge → stays **LISTEN**. Neither hop level needs another repeater here. Airtime and battery preserved for the badges further out that actually need to cascade.

**4. A punter walks their repeating Tildagon deeper into the crowd.** The badge loses direct Director range and stops relaying (it was serving as a hop 1 repeater but has no more hop 0 inputs). ~3 seconds later its idle timer fires, dropping it to **LISTEN**. Now it only hears the StickC's hop 1 in its new position — an edge — so within a few more seconds it re-elects **REPEAT** at hop 2. Total role-transition time ~4-10 seconds.

**5. Two Tildagons at the same edge, both trying to relay at the same hop.** Both TX at the same hop simultaneously → each sees the other's frame as a peer collision → both enter **CDOWN**. Whichever's random cooldown counter expires first while the other is still active drops back to **LISTEN**; the other continues as **REPEAT**. No operator action needed.

Nothing needs monitoring at showtime — the feature is on by default and adapts continuously to how the crowd moves.

## Getting it running

### Prerequisites

- An EMF Tildagon badge (or the [badge-2024-software simulator](https://github.com/emfcamp/badge-2024-software)).
- `mpremote`: `pip install mpremote`.
- Python 3.10+ on your host for the test suite.

### Deploy to a badge

With the badge connected over USB:

```sh
git clone https://github.com/ratcliffej/nocturnation-tildagon.git
cd nocturnation-tildagon
./deploy.sh
```

`deploy.sh` wipes any previous copy, copies the app payload to `:apps/nocturnation/` on the badge, and resets. Then scroll to **NocturNation** in the badge launcher and tap to run. On launch you land in the idle menu — pick **Lume** to receive or **Director** to run a show. Button **C** confirms a selection, **F** backs out.

For fast iteration after the first deploy, copy a single changed file and reset (overwriting a file in place avoids a re-wipe):

```sh
mpremote cp app.py :apps/nocturnation/app.py
mpremote reset
```

### Run the tests

```sh
pip install -e ".[dev]"
pytest
```

The suite runs on host Python — no hardware required. `pyproject.toml` puts the repo root on the pytest path so badge imports like `from nocturnation.protocol import parse_frame` resolve identically on host and badge.

## Project layout

The **app payload** (`app.py` plus the `nocturnation/` and `shows/` packages, `metadata.json`, `uQR.py`) lives at the repo root so the app-store tarball has `app.py` at the top, as the installer requires. The rest are dev/repo scaffolding.

```
app.py                        App entry: idle menu, mode lifecycle, WiFi/radio handover
metadata.json                 On-badge launcher manifest
uQR.py                        Vendored QR generator (MIT) for the Help screen
shows/                        Installable Shows, one folder each (simple_tap, motion_wave)
nocturnation/                 Internal package
  protocol/                   Frame parse/encode, constants, dedup ring, source_id
  receive.py                  Parse + dedup + hop-count pipeline
  channel_scan.py             Channel selection
  render/                     Perimeter, LCD, pulse, envelope, display surface
  shows/                      Show base class, context, registry, input actions
  director/                   Render dispatcher, IMU adapter, controller, sender, button map
  hal/                        Capability vocabulary (1:1 with the M5 firmware)
  plugins/                    Plugin base + property bags
  settings.py                 Persistent settings
  signal_tracker.py           NO SIGNAL gap detector
  tofu.py                     Trust-On-First-Use Director lock
tildagon.toml                 EMF app-store submission manifest
deploy.sh                     Wipe + copy + reset wrapper for mpremote (dev installs)
CHANGELOG.md                  Per-release notes
tests/                        Host-side pytest suite (not deployed to the badge)
pyproject.toml                Dev environment + pytest config
```

On the badge the app lives at `/apps/nocturnation/` (via `deploy.sh`) or `/apps/nocturnation_tildagon/` (via the app store); `app.py` derives its own directory at runtime, so both work.

## Architecture notes

- **Device class** is `MultiLedScreen` (`0x03`); the badge also renders Light-class broadcasts (`01:00`) on the perimeter ring.
- **Receives** on channel 1 or 11 (11 first, per the protocol manual's channel-discovery section); **transmits** on channel 1 only.
- **Group ID** defaults to a random value in {1, 2, 3} at first boot, matching the M5 Stick behaviour.
- **Calm Mode** default-on: frequency cap, brightness cap, LCD flash disabled. Full effects are an opt-in in Settings.

## Publishing to the EMF app store

1. **Make the repo public.** The app-store crawler at <https://apps.badge.emfcamp.org/> only indexes public GitHub repos.
2. **Add the `tildagon-app` topic**: `gh repo edit ratcliffej/nocturnation-tildagon --add-topic tildagon-app`.
3. **Check `tildagon.toml`** — `name`, `description` (≤ 140 chars), `version` (monotonic integer), `author`, `category`, `license`, `url`.
4. **Tag a release**: `git tag v1 && git push --tags`, then `gh release create v1 --notes-file CHANGELOG.md`.
5. **Wait ~15 minutes** for the crawler to pick it up; the app appears in the store.
6. **Ship fixes** by bumping `version` and tagging a new release.

The manifest schema is documented at <https://tildagon.badge.emfcamp.org/tildagon-apps/publish/>.

## Privacy

NocturNation collects nothing — no account, no telemetry, no analytics, no cloud, no microphone. All traffic is short-range ESP-NOW light commands; nothing reaches the internet. See [PRIVACY.md](PRIVACY.md) for the full statement.

## Contributing

Issues and pull requests welcome. For anything that touches the wire, the [protocol manual](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/protocol-manual.md) is the source of truth — a wire change must update the manual *and* both implementations (this app and the [M5 firmware](https://github.com/ratcliffej/nocturnation-stickc)). Cross-platform interop tests live alongside the firmware.

## Licence

MIT for code (see [LICENSE](LICENSE)). The linked manuals are CC BY-SA 4.0.
