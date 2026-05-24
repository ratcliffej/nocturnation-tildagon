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
- **399 host-side tests**, all passing, including byte-level parity against the protocol manual's reference vectors.

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
