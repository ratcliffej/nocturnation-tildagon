#!/usr/bin/env bash
# Deploy the NocturNation app to a Tildagon connected via USB.
#
# The app payload lives at the repo root (app.py + the nocturnation / shows
# packages + metadata.json + uQR.py) so the app-store tarball has app.py at
# the top, as the store installer requires. This script copies just that
# payload into :apps/nocturnation/ on the badge - it does NOT ship the repo's
# dev scaffolding (tests/, docs, .github, deploy.sh, pyproject.toml). The app
# store, by contrast, installs the whole repo tarball to
# /apps/nocturnation_tildagon/; app.py derives its own dir at runtime, so it
# works either way.
#
# Handles two mpremote gotchas:
#   1. `cp -r src dest` nests src inside dest when dest exists, so we wipe
#      and recreate :apps/nocturnation, then copy the packages into it.
#   2. `rm -r` is not honoured recursively on some mpremote versions and
#      errors with ENOTEMPTY (OSError 39) when a dir has __pycache__/ in it.
#      We run a recursive delete in MicroPython on the badge instead.
#
# Usage:
#   ./deploy.sh             - wipe + deploy + reset
#   ./deploy.sh --no-reset  - wipe + deploy (skip reset; useful if you
#                             want to inspect the badge before re-launch)

set -euo pipefail

RESET=1
for arg in "$@"; do
    case "$arg" in
        --no-reset) RESET=0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")"

echo "[deploy] wiping + recreating :apps/nocturnation on the badge"
mpremote exec '
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
rmtree("/apps/nocturnation")
try:
    os.mkdir("/apps")
except OSError:
    pass
os.mkdir("/apps/nocturnation")
'

# Strip host __pycache__ before copying: pytest leaves CPython .pyc files
# (one __pycache__ per package dir) that MicroPython can't use - copying them
# just wastes badge flash and slows the wipe. Only .py source should land.
echo "[deploy] stripping local __pycache__"
find nocturnation shows -name __pycache__ -type d -prune -exec rm -rf {} +

echo "[deploy] copying the app payload to :apps/nocturnation/"
mpremote cp app.py :apps/nocturnation/app.py
mpremote cp metadata.json :apps/nocturnation/metadata.json
mpremote cp uQR.py :apps/nocturnation/uQR.py
mpremote cp -r nocturnation :apps/nocturnation/   # -> :apps/nocturnation/nocturnation/
mpremote cp -r shows :apps/nocturnation/           # -> :apps/nocturnation/shows/

# Verify the copy landed BEFORE resetting: `mpremote reset` drops and
# re-enumerates the USB serial device, so any mpremote command issued
# immediately after fails with "[Errno 6] Device not configured" while the
# badge reboots. List first (REPL still alive), then reset once.
echo "[deploy] listing :apps/nocturnation"
mpremote ls :apps/nocturnation

if [[ "$RESET" -eq 1 ]]; then
    echo "[deploy] resetting badge"
    mpremote reset
fi

echo "[deploy] done. Launch 'NocturNation' from the badge UI."
