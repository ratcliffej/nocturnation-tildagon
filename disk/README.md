# Flopagon disk

Deploy Tildagon apps without WiFi. Plug in a pre-provisioned Flopagon
and the badge auto-launches a disk manager: install apps from the
disk, back apps up onto the disk, or delete apps from either side.

Solves the EMF Stage D problem: punters wanted NocturNation on their
badges but the site WiFi was too poor to reach the app store. The
disk manager works for any Tildagon app, not just NocturNation.

## Layout

```
disk/
├── installer/          # runs on the Tildagon when the disk is inserted
│   ├── app.py          # DiskManagerApp — hub menu + all four flows
│   ├── _jobs.py        # CopyJob + DeleteJob state machines
│   ├── _badge_apps.py  # enumerate apps on /apps/
│   ├── _fsutil.py      # filesystem helpers (dual-runtime)
│   └── _manifest.py    # disk.json read/write helpers
├── bootstrap/          # 2 KB EEPROM app that auto-launches installer
├── dev/                # dev tooling (populate, smoke test, probe)
└── README.md           # this file
```

## Operations (hub menu)

1. **Install app** — copy from disk to badge
2. **Backup app** — copy from badge to disk (writes `disk.json` manifest)
3. **Delete from disk** — remove an app from disk
4. **Delete from badge** — remove an app from the Tildagon
5. **Exit** — return to launcher

Each operation drills into a sub-picker of the relevant apps, asks for
confirmation, then progresses through the job with an on-screen bar
(for copies) or a single-tick delete.

## Contract with the bootstrap

Bootstrap mounts the Flopagon's 16 MB flash at any path and adds it to
`sys.path`. Installer discovers its own mount from `__file__` —
installed as `<mount>/installer/app.py`, it reads `<mount>/apps/*/`.
Production mount is `/disk`.

Expected layout on the disk:

```
<mount>/
├── installer/
└── apps/
    ├── nocturnation/
    │   ├── disk.json
    │   ├── app.py
    │   ├── metadata.json
    │   └── ...
    └── <other-app>/
        └── ...
```

`disk.json` schema (v1):

```json
{
    "manifest_version": 1,
    "name": "NocturNation",
    "slug": "nocturnation",
    "version": "1.0.18",
    "files": 51,
    "copied_at": 12345678
}
```

Written by the Backup flow. Read by the Install picker for display.

## Behaviour details

- **Slug preservation** — a badge app at `/apps/<slug>/` backs up to
  `<mount>/apps/<slug>/`. On install into a different badge, the same
  slug is used.
- **Overwrites** — Install wipes any existing `/apps/<slug>/` before
  copying. Backup wipes any existing `<mount>/apps/<slug>/`.
- **Copy progress** — one file per background-tick (~20 Hz) so draw()
  keeps rendering. A 50-file app takes ~2.5 s.
- **Failure handling** — file-level errors abort that job with a
  visible message; other apps and the badge itself are untouched.
- **Launcher rescan** — Install + Delete-from-badge emit
  `InstallNotificationEvent` so newly present / absent apps update
  in the launcher without a reboot.
- **Yanking the Flopagon mid-op** — the current job errors on the
  next read, transitions to Done with a fail message.

## Hidden apps

`metadata.json` with `hidden: true` are excluded from the Backup +
Delete-from-badge pickers (the same filter the launcher uses).

## Testing

Host pytest covers the pure-Python helpers:

```bash
.venv/bin/pytest tests/test_disk_*.py
```

The `DiskManagerApp` UI itself is Tildagon-runtime only and needs
hardware validation.
