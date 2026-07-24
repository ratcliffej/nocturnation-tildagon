# Flopagon cartridge

Deploy NocturNation onto Tildagon badges without WiFi — plug in a
pre-provisioned Flopagon, and the badge auto-launches an installer
menu.

Solves the EMF Stage D problem: punters wanted NocturNation on their
badges but the site WiFi was too poor to reach the app store.

## Status

- **Phase 2 (this PR):** flash-resident installer — done.
- **Phase 3:** EEPROM autoboot bootstrap — pending.
- **Phase 1:** "Copy to Flopagon" menu item in the Lume app — pending.
- **Phase 4:** provisioning script + end-to-end README — pending.

Full workflow docs land with Phase 4. This README is the placeholder.

## Layout

```
cartridge/
├── installer/       # Phase 2 — lives on the Flopagon flash at
│   │                # /cartridge/installer/, auto-launched by the
│   │                # Phase 3 EEPROM bootstrap on hexpansion insert.
│   ├── app.py       # InstallerApp — Menu + picker + copier
│   ├── _fsutil.py   # host-testable filesystem helpers
│   └── _manifest.py # cartridge.json read/write helpers
├── bootstrap/       # Phase 3 — pending
└── provision/       # Phase 4 — pending
```

## Contract with the bootstrap

Phase 3 mounts the Flopagon's 16 MB flash at whatever path it likes
and adds it to `sys.path`. The installer discovers its own mount
point at import time from `__file__` — installed as
`<mount>/installer/app.py`, it reads `<mount>/apps/*/`. So the
bootstrap can pick any mount path without a code change here.

The mount is expected to hold:

```
<mount>/
├── installer/       # this app
└── apps/
    ├── nocturnation/
    │   ├── cartridge.json
    │   ├── app.py
    │   ├── metadata.json
    │   └── ...
    └── <other-app>/
        └── ...
```

`cartridge.json` schema (v1):

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

Written by the Phase 1 self-copy feature. Read by the installer to
build its menu. `slug` becomes the target folder name — e.g. it will
install into `/apps/nocturnation/` on the badge.

## Installer behaviour

1. On launch, enumerate `/cartridge/apps/*/cartridge.json`.
2. Show a picker:
   - 0 apps → "No apps on this cartridge" + Exit
   - 1 app → "Install &lt;name&gt; v&lt;version&gt;" + Cancel
   - N apps → one row per app + "Install all" + Cancel
3. On confirm, wipe existing `/apps/&lt;slug&gt;/` on the badge, then copy
   one file per background-tick (`background_update`, ~20 Hz) so
   the progress bar stays live.
4. Per-app failure isolation: an OSError on one file marks that app
   failed but doesn't stop the batch.
5. On completion, emit `InstallNotificationEvent()` so the launcher
   re-scans `/apps` and picks up newly installed apps without a reboot.
6. Gracefully handles Flopagon yanked mid-install (OSError on read
   surfaces as a failed install for the current app; the installer
   itself is now running from RAM so it stays alive).

## Testing

Host pytest covers the filesystem + manifest helpers:

```
.venv/bin/pytest tests/test_cartridge_fsutil.py tests/test_cartridge_manifest.py
```

The `InstallerApp` class itself is Tildagon-runtime only (badge OS
imports); it must be verified on hardware.
