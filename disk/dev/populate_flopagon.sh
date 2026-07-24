#!/usr/bin/env bash
# disk/dev/populate_flopagon.sh
#
# Push the installer + a synthetic testapp onto the Flopagon's 16 MB
# flash via the mount our bootstrap creates at /disk. Lets us
# drive the full E2E flow without waiting for Phase 1 ("Copy to
# Flopagon" from the Lume app) to land.
#
# Uses `mpremote mount . run` so the whole provisioning runs in ONE
# session - a mid-op Flopagon contact drop can't strand us between
# a mkdir and a cp any more.
#
# Prerequisites:
#   1. Flopagon inserted, /disk/ visible in `mpremote resume ls`
#      (i.e. our bootstrap ran - you saw "Disk error / No
#      installer" on the badge, or the installer was already there
#      from a prior run)
#   2. Working directory is the Tildagon repo root
#
# What lands on /disk/:
#   /disk/installer/{app.py, _fsutil.py, _manifest.py, __init__.py}
#   /disk/apps/testapp/{app.py, disk.json, metadata.json}
#
# Usage:
#   ./disk/dev/populate_flopagon.sh              # populate
#   ./disk/dev/populate_flopagon.sh --cleanup    # wipe installer + apps
#   ./disk/dev/populate_flopagon.sh --help

set -euo pipefail

MODE=deploy
for arg in "$@"; do
    case "$arg" in
        --cleanup) MODE=cleanup ;;
        --help|-h) MODE=help ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$MODE" == help ]]; then
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ "$MODE" == cleanup ]]; then
    echo "[populate] wiping /disk/installer + /disk/apps"
    mpremote resume exec '
import os
def rmtree(p):
    try:
        for e in os.ilistdir(p):
            path = p + "/" + e[0]
            if e[1] & 0x4000:
                rmtree(path)
            else:
                os.remove(path)
        os.rmdir(p)
    except OSError:
        pass
rmtree("/disk/installer")
rmtree("/disk/apps")
'
    echo "[populate] done"
    exit 0
fi

# All operations happen in one mpremote session. `mount .` maps the
# repo root to /remote on the badge; `run` executes the provisioning
# script, which reads from /remote/... and writes to /disk/...
# Between commands in this chain the connection stays open, so a
# Flopagon contact drop mid-provision surfaces as one clean error
# rather than a half-populated flash.
mpremote resume mount . run disk/dev/_populate_provision.py

cat <<'EOF'

To exercise the full E2E flow now:
  1. mpremote reset
  2. Physically remove + re-insert the Flopagon
  3. On the badge:
     - Our bootstrap runs
     - Installer picker: "Install Disk test app v1.0.0"
     - Tap CONFIRM (C) to install
     - Progress bar animates
     - Done screen: "OK Disk test app"
     - Tap CANCEL (F) to exit
     - Launcher should now include "Disk test app"

Verify the install landed on the badge's internal filesystem:
  mpremote resume ls :apps/testapp/

Cleanup this staging (leaves the badge's installed testapp alone):
  ./disk/dev/populate_flopagon.sh --cleanup
EOF
