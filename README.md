# NocturNation Tildagon receiver app

> Open-source crowd lighting, on the EMF Tildagon badge.

This repository is the MicroPython implementation of the NocturNation receiver protocol, targeting the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/). The badge listens on ESP-NOW for `LIGHT_COMMAND` frames from a NocturNation master Stick, animates the six perimeter LEDs and 240×240 round LCD in sync with the music, and runs Calm Mode by default per the photosensitivity safety constraints.

**This is a fresh codebase, not a port of the M5 Stick firmware.** The Tildagon shares the [NocturNation ESP-NOW protocol](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md) with the M5 sticks but nothing else - different chip family (ESP32-C3 RISC-V), different language (MicroPython), different SDK (Tildagon OS app framework).

## Status

Epic 5 (Tildagon receiver app):

- **Block 1 (platform familiarisation)**: shipped. App installs via `mpremote cp` + reset, appears in the badge launcher, draws the brand-mark, exits cleanly on CANCEL. Bench-verified on real hardware 2026-05-12.
- **Block 2 (ESP-NOW receive)**: shipped at the protocol layer. Frame parser, deduplication ring, hop-count enforcement, and channel auto-scan state machine all landed with 44 host-side parity tests against protocol manual annex C. The async `background_task` in `app.py` wires these together for ESP-NOW receive on real hardware, with the operating channel and frame count displayed on the LCD. Hardware verification (master-Stick to Tildagon frame delivery, on-screen frame counter incrementing) is the remaining acceptance criterion.
- **Block 3-7**: not started. See the [Epic 5 plan](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/epics/epic-05-tildagon.md) for the full block plan.

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

The repo mirrors the Tildagon's on-badge `/apps/<owner_appname>/` layout. Deploy is a copy-and-reset cycle:

```sh
# From the repo root, with the badge plugged in via USB.

# 1. Copy the app into the badge's /apps/ directory.
mpremote cp -r apps/nocturnation :apps/nocturnation

# 2. Reset the badge so the launcher re-scans /apps/.
mpremote reset

# 3. Scroll to "NocturNation" in the launcher and tap to run.
```

For fast iteration after the first deploy, re-copy just the file you changed and re-launch from the badge UI:

```sh
mpremote cp apps/nocturnation/app.py :apps/nocturnation/app.py
# Back out of the app on the badge, then re-launch from the launcher.
```

`mpremote mount apps` is also available - it mounts the local `apps/` folder at `/remote` on the badge for ad-hoc REPL scripting. It does NOT deploy the app to the launcher: the launcher only scans `/apps/`, so calling `NocturNationApp()` from the mounted REPL doesn't run the app the way the launcher does (instantiate + register with scheduler + drive update/draw). Use `mount` for poking at modules from the REPL; use `cp` + `reset` to actually launch the app.

### Native unit tests

The protocol implementation has byte-level parity tests against the reference test vectors in the [protocol manual annex C](https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md#annex-c-reference-test-vectors):

```sh
pip install -e ".[dev]"
pytest
```

The tests run on the host Python; no hardware required. `pyproject.toml` adds `apps/nocturnation/` to the pytest `pythonpath` so imports like `from nocturnation.protocol import parse_frame` resolve identically on host and badge.

## Project layout

```
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
        perimeter.py                   Six-LED perimeter renderer (Block 3)
        lcd.py                         Round-LCD renderer (Block 4)
      config.py                        In-app settings (Block 5)
tests/                                 Host-side pytest suite (not deployed to badge)
  test_frame.py                        Header + payload parser tests (Block 1/2)
  test_dedup.py                        Dedup ring tests (Block 2)
  test_receive.py                      Receive pipeline tests (Block 2)
  test_channel_scan.py                 Channel scanner state-machine tests (Block 2)
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

## Contributing

This repository is private during initial development. If you'd like to contribute, open an issue (or reach out directly) to discuss.

For protocol changes: the protocol manual in `nocturnation-m5` is the source of truth. Any change that affects the wire must update the protocol manual *and* both implementations (M5 and Tildagon). Cross-platform interop tests live in the M5 repo.

## Licence

MIT for code (see [LICENSE](LICENSE)). Documentation references in this README link to CC BY-SA 4.0 manuals in the M5 firmware repo.
