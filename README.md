# NocturNation Tildagon receiver app

> Open-source crowd lighting, on the EMF Tildagon badge.

This repository is the MicroPython implementation of the NocturNation receiver protocol, targeting the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/). The badge listens on ESP-NOW for `LIGHT_COMMAND` frames from a NocturNation master Stick, animates the six perimeter LEDs and 240×240 round LCD in sync with the music, and runs Calm Mode by default per the photosensitivity safety constraints.

**This is a fresh codebase, not a port of the M5 Stick firmware.** The Tildagon shares the [NocturNation ESP-NOW protocol](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md) with the M5 sticks but nothing else - different chip family (ESP32-C3 RISC-V), different language (MicroPython), different SDK (Tildagon OS app framework).

## Status

Epic 5 (Tildagon receiver app):

- **Block 1 (platform familiarisation)**: shipped. App installs via `mpremote cp` + reset, appears in the badge launcher, draws the brand-mark, exits cleanly on CANCEL. Bench-verified on real hardware 2026-05-12.
- **Block 2 (ESP-NOW receive)**: shipped. Frame parser, deduplication ring, hop-count enforcement, and channel auto-scan state machine all landed with host-side parity tests against protocol manual annex C. The async `background_task` wires these together for ESP-NOW receive on real hardware. Bench-verified 2026-05-12: real LIGHT_COMMAND frames from a master Stick on channel 11 parse correctly and increment the on-screen counter. Channel auto-scan-across-channels couldn't be implemented as written (the badge's networking layer rejects a second `wlan.config(channel=...)` call); tracked as Epic 5 open design question Q6 for Block 5.
- **Block 3 (perimeter LED rendering)**: shipped. `PerimeterRenderer` arms per-LED envelopes from each accepted LIGHT_COMMAND (chance-gated independently per LED, then ramped through attack/sustain/release per the protocol's Time and Chance enums). Frequency cap and 50 % brightness cap enforce Calm Mode default-on per architecture spec section 15. The app's `update()` tick advances envelopes and commits via `tildagonos.leds.write()`. Bench-verified 2026-05-12: master kicks fire the ring in sync, no flicker (after emitting `PatternDisable` to suppress the badge's system patterndisplay service).
- **Block 4 (LCD pulse rendering)**: shipped at the protocol layer. `LcdRenderer` renders an accepted LIGHT_COMMAND as a soft full-screen colour wash on the round 240×240 LCD. Calm Mode default-on disables LCD pulsing entirely per architecture spec section 15.3 ("screen flashing disabled"); Full mode arms an envelope at peak brightness capped at 60 % (an uncapped full-screen wash at face distance is uncomfortably bright). Frequency cap matches the perimeter renderer's Full mode (4 Hz). Bench verification of the wash needs Block 5's settings UI to flip into Full mode.
- **Block 5 (configuration UI)**: shipped at the protocol layer. Persistent `Settings` (Calm Mode toggle, group 0..3 cycle, channel auto/1/11 cycle) stored as JSON at `/nocturnation_settings.json` outside the app dir so deploys don't clobber operator preferences. In-app menu via `app_components.Menu`: CONFIRM (button C) opens, CANCEL (button F) backs out, selecting a line cycles its value. Class + group filter on inbound LIGHT_COMMAND per protocol manual section 4.2 and Epic 5 Q1/Q2.
- **Block 6 (NO SIGNAL + backgrounded + MUSIC_EVENT)**: shipped at the protocol layer. `SignalTracker` records every accepted frame's timestamp; the draw loop overlays NO SIGNAL after a 3 s gap per protocol manual section 6.2. MUSIC_EVENT frames trigger local synthetic fires: DROP → bright whiteout, BREAKDOWN → slow dim blue. Backgrounded operation per architecture spec section 7.3 - the perimeter LED ring keeps animating when the operator switches to another badge app.
- **Block 7 (submission readiness)**: shipped at the protocol layer. `tildagon.toml` manifest written per EMF app-store schema; `CHANGELOG.md` tracks the release. **120/120 host tests passing.** Remaining Block 7 acceptance is hardware-side and gated on operator workflow:
  - M5↔Tildagon interop bench session with bracelets.
  - Battery drain measurement (idle vs receiver-app over 2 h).
  - Flipping the repo to **public** (required for the EMF crawler at <https://apps.badge.emfcamp.org> to find it).
  - Adding the `tildagon-app` GitHub topic.
  - Creating a release tag (e.g. `v1`).
  - Submitting via the EMF app store and tracking review.

Both repositories are currently private; access is granted on request.

## Related documentation

The NocturNation manuals live in the [nocturnation-m5](https://github.com/ratcliffej/nocturnation-m5) repository:

- [Protocol manual](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md) - **the implementation specification for this repo**. Every byte the Tildagon receives is documented here.
- [User manual](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/user-manual.md) - operator-facing behaviour the Tildagon should match where applicable.
- [Flow diagrams](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/flow-diagrams.md) - Mermaid renderings of the receive pipeline + class+group routing.

## Quick start

### Prerequisites

- An EMF Tildagon badge (or the [badge-2024-software simulator](https://github.com/emfcamp/badge-2024-software) running locally).
- `mpremote` installed: `pip install mpremote`.
- Python 3.10+ on your development host for `pytest`.

### Test on real hardware

The repo mirrors the Tildagon's on-badge `/apps/<owner_appname>/` layout. The wipe-and-copy deploy is wrapped in `deploy.sh`:

```sh
./deploy.sh
```

That handles two mpremote gotchas: `cp -r src dest` nests src inside dest when dest already exists, and `rm -r` is not honoured recursively on some mpremote versions (errors with `OSError: 39` / `ENOTEMPTY` when `__pycache__/` is present). The script wipes via `mpremote exec` using a MicroPython recursive delete, then copies, then resets.

After `deploy.sh` finishes, scroll to "NocturNation" in the badge launcher and tap to run. To inspect the badge filesystem without re-launching, run `./deploy.sh --no-reset` then `mpremote repl`.

Equivalent manual sequence if you'd rather not use the script:

```sh
mpremote exec "
import os
def rmtree(p):
    for e in os.ilistdir(p):
        path = p + '/' + e[0]
        if e[1] & 0x4000:
            rmtree(path)
        else:
            os.remove(path)
    os.rmdir(p)
rmtree('/apps/nocturnation')
"
mpremote cp -r apps/nocturnation :apps/
mpremote reset
```

Verify what landed:

```sh
mpremote ls :apps/nocturnation
mpremote ls :apps/nocturnation/nocturnation
```

For fast iteration after a successful first deploy, re-copy individual files in place. Writing over an existing file does NOT have the nesting problem:

```sh
mpremote cp apps/nocturnation/app.py :apps/nocturnation/app.py
mpremote reset
# Back out of the app on the badge, then re-launch from the launcher.
```

`mpremote mount apps` is also available - it mounts the local `apps/` folder at `/remote` on the badge for ad-hoc REPL scripting. It does NOT deploy the app to the launcher: the launcher only scans `/apps/`, so calling `NocturNationApp()` from the mounted REPL doesn't run the app the way the launcher does (instantiate + register with scheduler + drive update/draw). Use `mount` for poking at modules from the REPL; use `rm` + `cp` + `reset` to actually launch the app.

### Native unit tests

The protocol implementation has byte-level parity tests against the reference test vectors in the [protocol manual annex C](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md#annex-c-reference-test-vectors):

```sh
pip install -e ".[dev]"
pytest
```

The tests run on the host Python; no hardware required. `pyproject.toml` adds `apps/nocturnation/` to the pytest `pythonpath` so imports like `from nocturnation.protocol import parse_frame` resolve identically on host and badge.

## Project layout

```
tildagon.toml                          EMF app-store submission manifest (Block 7)
CHANGELOG.md                           Per-release release notes (Block 7)
apps/                                  Mirrors the badge's /apps/ filesystem
  nocturnation/                        owner_appname (single dir per app)
    app.py                             App class + __app_export__
    metadata.json                      Tildagon OS manifest (name / hidden / version)
    nocturnation/                      Internal package
      protocol/
        constants.py                   Message types, device classes, enum values
        frame.py                       ESP-NOW frame parser + validator
        dedup.py                       Sixteen-deep dedup ring (Block 2)
      receive.py                       parse + dedup + hop-count pipeline (Block 2)
      channel_scan.py                  Auto-scan state machine (Block 2)
      render/
        envelope.py                    Shared ASR brightness math + Time enum
        perimeter.py                   Twelve-LED perimeter renderer (Block 3)
        lcd.py                         Round-LCD pulse renderer (Block 4)
      settings.py                      Persistent settings model (Block 5)
      signal_tracker.py                NO SIGNAL gap detector (Block 6)
      music_event.py                   MUSIC_EVENT synthetic-fire factory (Block 6)
tests/                                 Host-side pytest suite (not deployed to badge)
  test_frame.py                        Header + payload parser tests (Block 1/2)
  test_dedup.py                        Dedup ring tests (Block 2)
  test_receive.py                      Receive pipeline tests (Block 2)
  test_channel_scan.py                 Channel scanner state-machine tests (Block 2)
  test_perimeter.py                    Perimeter LED renderer tests (Block 3)
  test_lcd.py                          LCD pulse renderer tests (Block 4)
  test_settings.py                     Settings model tests (Block 5)
  test_signal_tracker.py               NO SIGNAL detector tests (Block 6)
  test_music_event.py                  MUSIC_EVENT synthesis tests (Block 6)
pyproject.toml                         Dev environment + pytest config
```

The on-badge import path for the app is `apps.nocturnation.app`; the internal package imports as `nocturnation.protocol`, `nocturnation.render`, etc. (the app dir is on sys.path when the app loads).

## Architecture notes

- **Slave-only**. Tildagon has no microphone and is architecturally a pure receiver. See [Epic 5 architectural constraint](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/epics/epic-05-tildagon.md#architectural-constraint-tildagon-is-slave-only).
- **Device class** is `MultiLedScreen` (`0x03`). Light-class addressed broadcasts (`01:00`) also render on the perimeter LEDs - see Epic 5 open design question Q1.
- **Receive on channels 1 or 11** (11 first per [protocol manual §5.3](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md#5-channel-discovery)).
- **Transmit (manual-master mode, if implemented)** - hard rule, channel 1 only. Channel 11 is reserved for the official master.
- **Group ID** defaults to a random value in {1, 2, 3} at first boot (Block 5; matches the M5 Stick behaviour).
- **Calm Mode** default-on: 2 Hz frequency cap, 50 % brightness cap, LCD flash disabled. Operator opt-in to full-effect mode via in-app settings.

## EMF app-store submission

When ready to publish (Block 7 close-out):

1. **Flip the repo to public.** The EMF app-store crawler at <https://apps.badge.emfcamp.org/> only sees public GitHub repos. There is no private submission path; publishing means going public.
2. **Add the `tildagon-app` GitHub topic** to the repo (Settings → Topics on the repo page, or `gh repo edit ratcliffej/nocturnation-tildagon --add-topic tildagon-app`).
3. **Verify `tildagon.toml`** matches your intended release: `name`, `description` (≤ 140 chars), `version` (integer, monotonic), `author`, `category`, `license` (SPDX), `url`.
4. **Create a release tag**: `git tag v1 && git push --tags`, then create the release on GitHub from that tag (or `gh release create v1 --notes-file CHANGELOG.md`).
5. **Wait ~15 minutes**: the crawler picks up the release and the app appears in the app store. Track listing at <https://apps.badge.emfcamp.org/>.
6. **Iterate via new releases** for fixes. Bump `version` in `tildagon.toml` and create a new release tag; the crawler reads each release in turn.

The `tildagon.toml` schema reference is at <https://tildagon.badge.emfcamp.org/tildagon-apps/publish/>.

## Contributing

This repository is private during initial development. If you'd like to contribute, open an issue (or reach out directly) to discuss.

For protocol changes: the protocol manual in `nocturnation-m5` is the source of truth. Any change that affects the wire must update the protocol manual *and* both implementations (M5 and Tildagon). Cross-platform interop tests live in the M5 repo.

## Licence

MIT for code (see [LICENSE](LICENSE)). Documentation references in this README link to CC BY-SA 4.0 manuals in the M5 firmware repo.
