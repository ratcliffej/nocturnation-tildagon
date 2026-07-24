#!/usr/bin/env bash
# disk/dev/provision_flopagon.sh
#
# One-shot Flopagon provisioning: EEPROM header + bootstrap + disk
# contents in a single mpremote session. Use this when setting up a
# fresh Flopagon (or reprovisioning a corrupted one).
#
# Does everything in order:
#   1. Compile disk/bootstrap/app.py to .mpy if missing / stale
#   2. Provision EEPROM: header + mkfs LFS2 + bootstrap.mpy
#   3. Provision flash: mount + wipe + installer sources + testapp
#   4. Hard-reset the badge so the new bootstrap runs cleanly
#
# Prereqs:
#   - Flopagon inserted (edit PORT in provision_flopagon.py to match
#     your slot; default is 1)
#   - Write-protect jumper shorted (permanent on Jason's dev boards)
#
# Usage:
#   ./disk/dev/provision_flopagon.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

BOOT_SRC="disk/bootstrap/app.py"
BOOT_MPY="disk/bootstrap/app.mpy"

# Rebuild bootstrap if missing or older than source.
if [ ! -f "$BOOT_MPY" ] || [ "$BOOT_SRC" -nt "$BOOT_MPY" ]; then
    echo "[provision] compiling bootstrap (-O3 for V1 EEPROM budget)"
    .venv/bin/mpy-cross -O3 "$BOOT_SRC"
fi
echo "[provision] bootstrap: $(stat -f %z "$BOOT_MPY") bytes"

echo "[provision] running one-shot provisioning on the badge"
mpremote resume mount . run disk/dev/provision_flopagon.py

echo "[provision] resetting badge"
mpremote reset

cat <<'EOF'

[provision] done.

Physical remove + reinsert the Flopagon. On the badge screen you
should see the hub menu:

    Install app
    Backup app
    Delete from disk
    Delete from badge
    Exit

If you see "Disk error / <something>" instead, the last [provision]
line before the reset points at what went wrong.
EOF
