# Flopagon EEPROM autoboot bootstrap

Phase 3 of the disk workflow. This is the tiny app that lives on
the Flopagon's 2 KB EEPROM and gets auto-run by the badge when the
Flopagon is inserted. Its only job is to init the 16 MB flash, mount
it, and hand off to the Phase 2 installer.

## Size budget

The V1 Flopagon EEPROM is **2 KB total**, holding:

| Component | Bytes |
|---|--:|
| Hexpansion identity header | 32 |
| LittleFS2 filesystem metadata | ~200-400 |
| `app.mpy` (this) | rest |

Realistic ceiling for `app.mpy` on V1: ~1.6 KB.

**Current build:** 1304 bytes. Nathan's original Flopagon `app.mpy`
(the mount/format helper) is 1528 bytes for reference.

The V2 Flopagon has an 8 KB EEPROM (identifiable by black PCB); more
headroom but the same file must fit either variant, so we design to
the V1 budget.

Every line here spends byte budget. Menus, multi-app selection, copy
logic, verbose error UI — all belong in the installer (unlimited
16 MB flash). Adding logic to this bootstrap will overflow the V1
EEPROM.

## Runtime flow

```
badge inserts Flopagon
    -> HexpansionManagerApp mounts the EEPROM at /hexpansion_<N>
    -> auto-launches /hexpansion_<N>/app.py (this, as .mpy)
        -> Bootstrap.__init__(config=HexpansionConfig(port))
            1. init SPI on config.pin[1..3] (sck/mosi/miso)
            2. init FLASH on config.pin[0] as CS (2-attempt retry on ValueError)
            3. vfs.mount(flash, "/disk")
            4. sys.path.append("/disk")
            5. from installer.app import InstallerApp
            6. Bootstrap forwards draw/update/background_update to installer
        <- InstallerApp.__init__ discovers DISK_MOUNT from __file__
           = "/disk", reads /disk/apps/*/disk.json, etc.
```

On Flopagon removal, `Bootstrap.deinit()` propagates to the installer
(if it has one), then unmounts the flash + releases SPI.

## Compile

```
.venv/bin/mpy-cross -O3 disk/bootstrap/app.py
```

Output: `disk/bootstrap/app.mpy`. Check the byte count against
the ~1.6 KB V1 budget above before writing.

**`-O3` is load-bearing.** It strips source line info from tracebacks
(saves ~100 bytes on this file). All our exception paths swallow to
`self._error` and never print tracebacks, so no diagnostics are lost.
Without `-O3` the .mpy is ~1630 bytes and hits ENOSPC on the V1
EEPROM's LFS2 during the `mpremote cp`.

The mpy version is determined by whatever mpy-cross is installed;
Tildagon firmware must be at least that version to load it. Nathan's
Flopagon repo doesn't pin a version and the .mpy travels between
badges without issue, so we don't pin either. If a badge fails to
load our .mpy after a firmware downgrade, cross-compile with an older
mpy-cross.

## Write to the EEPROM

Nathan's Flopagon setup already puts a valid hexpansion header +
LittleFS2 filesystem on the EEPROM, with his own `app.mpy` on it.
Overwriting his `app.mpy` with ours takes over the autoboot without
touching the header (which the badge's hexpansion manager reads
first).

Steps (per Flopagon, once per disk):

1. **Short the write-protect jumper.** The 0.1" header in the corner
   of the PCB — bridge with a jumper wire or tweezers. This is
   Nathan's standard mechanism; on Flopagon V2 the pads are in the
   same location. The board used for our development already has
   this jumper permanently bridged.

2. **Insert the Flopagon into any port on the badge.** The badge OS
   will auto-mount the EEPROM at `/hexpansion_<N>` (where N is the
   port number, 1-6 clockwise from top-right).

3. **From the host, connect to the badge and overwrite the .mpy:**

   ```
   mpremote cp disk/bootstrap/app.mpy :hexpansion_<N>/app.mpy
   ```

   The badge's hexpansion manager will only re-launch our app on the
   next insertion cycle — leave the Flopagon in place, then either
   soft-reboot the badge (`mpremote reset`) or remove + re-insert the
   Flopagon.

4. **Remove the jumper.** Leaving it shorted allows accidental
   overwrites; the default write-protected state is safer for
   distribution.

## Verify

After writing + reset/re-insert:

- Serial console should show the hexpansion insertion event and our
  bootstrap launching (not Nathan's mount menu).
- If the flash is populated with `/installer/app.py` + at least one
  `/apps/*/disk.json`, the installer picker appears.
- If the flash is empty (no `installer/`), the bootstrap's error
  screen shows `Disk error / No installer`.

## Prerequisites for a full working disk

This bootstrap alone doesn't do anything useful — it hands off to the
installer, which reads from the flash. A working disk needs, in
this order:

1. Flopagon provisioned with Nathan's `setup_flopagon(port)` — one-off,
   creates hexpansion header + formats LittleFS2 on the EEPROM +
   copies Nathan's `app.mpy` to it.
2. This bootstrap's `.mpy` overwrites Nathan's `app.mpy` on the EEPROM
   (steps above).
3. Installer + at least one app copied onto the 16 MB flash:
   - `/installer/{app.py, _fsutil.py, _manifest.py, __init__.py}`
   - `/apps/<slug>/{app.py, disk.json, metadata.json, ...}`

Phase 4 (provisioning script + docs) turns this into a repeatable
one-command flow.
