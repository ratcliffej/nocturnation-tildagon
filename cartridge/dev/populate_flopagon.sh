#!/usr/bin/env bash
# cartridge/dev/populate_flopagon.sh
#
# Push the installer + a synthetic testapp onto the Flopagon's 16 MB
# flash via the mount our bootstrap creates at /cartridge. Lets us
# drive the full E2E flow without waiting for Phase 1 ("Copy to
# Flopagon" from the Lume app) to land:
#
#     Flopagon inserted
#       -> our bootstrap runs (Phase 3)
#       -> mounts flash at /cartridge
#       -> imports installer.app (Phase 2)
#       -> picker shows "Cartridge test app v1.0.0"
#       -> confirm -> copy /cartridge/apps/testapp -> /apps/testapp
#       -> InstallNotificationEvent -> launcher rescans
#       -> "Cartridge test app" appears in launcher
#
# Prerequisites (this script does NOT set them up):
#   1. Flopagon inserted, /cartridge/ visible in `mpremote resume ls`
#      (i.e. our bootstrap ran successfully — you saw "Cartridge error
#      / No installer" on the badge)
#   2. Working directory is the Tildagon repo root
#
# What lands on /cartridge/:
#   /cartridge/installer/{app.py, _fsutil.py, _manifest.py, __init__.py}
#   /cartridge/apps/testapp/{app.py, cartridge.json, metadata.json}
#
# Usage:
#   ./cartridge/dev/populate_flopagon.sh              # populate
#   ./cartridge/dev/populate_flopagon.sh --cleanup    # wipe /cartridge/ contents
#   ./cartridge/dev/populate_flopagon.sh --help

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
    sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CARTRIDGE_ROOT="/cartridge"

RMTREE_PY="
import os
def rmtree(p):
    try:
        for e in os.ilistdir(p):
            path = p + '/' + e[0]
            if e[1] & 0x4000:
                rmtree(path)
            else:
                os.remove(path)
        os.rmdir(p)
    except OSError:
        pass
"

# Sanity check the mount exists before we start.
if ! mpremote resume exec "
import os
try:
    os.listdir('${CARTRIDGE_ROOT}')
except OSError:
    raise SystemExit(1)
" >/dev/null 2>&1; then
    cat >&2 <<EOF
[populate] ${CARTRIDGE_ROOT}/ isn't mounted on the badge.

The Phase 3 bootstrap must be running and its flash mount must be
active. Try:
  1. Confirm Flopagon is inserted and physically stable
  2. Verify the bootstrap is on the EEPROM:
       mpremote resume ls :hexpansion_<N>/     # expect app.mpy 1461 bytes
  3. Verify the mount:
       mpremote resume ls                       # expect /cartridge/ at root
EOF
    exit 1
fi

if [[ "$MODE" == cleanup ]]; then
    echo "[populate] wiping ${CARTRIDGE_ROOT}/installer + ${CARTRIDGE_ROOT}/apps"
    mpremote resume exec "${RMTREE_PY}
rmtree('${CARTRIDGE_ROOT}/installer')
rmtree('${CARTRIDGE_ROOT}/apps')
"
    echo "[populate] done"
    exit 0
fi

# ----------------------------------------------------------------- deploy

echo "[populate] wiping any previous installer + apps under ${CARTRIDGE_ROOT}"
mpremote resume exec "${RMTREE_PY}
rmtree('${CARTRIDGE_ROOT}/installer')
rmtree('${CARTRIDGE_ROOT}/apps')
"

echo "[populate] creating ${CARTRIDGE_ROOT}/{installer,apps/testapp}"
mpremote resume exec "
import os
for p in ('${CARTRIDGE_ROOT}/installer',
          '${CARTRIDGE_ROOT}/apps',
          '${CARTRIDGE_ROOT}/apps/testapp'):
    try:
        os.mkdir(p)
    except OSError:
        pass
"

echo "[populate] copying installer sources"
mpremote resume cp cartridge/installer/app.py        ":${CARTRIDGE_ROOT}/installer/app.py"
mpremote resume cp cartridge/installer/_fsutil.py    ":${CARTRIDGE_ROOT}/installer/_fsutil.py"
mpremote resume cp cartridge/installer/_manifest.py  ":${CARTRIDGE_ROOT}/installer/_manifest.py"
mpremote resume cp cartridge/installer/__init__.py   ":${CARTRIDGE_ROOT}/installer/__init__.py"

# Build the synthetic testapp locally (mirrors smoke_deploy.sh).
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

cat > "$TMPDIR/app.py" <<'PY_EOF'
# Synthetic testapp for the cartridge installer E2E. Overrides
# update() to return True so draw() fires (base App.update returns
# False and would suppress render). Handles CANCEL for clean exit.
import app
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus
from system.scheduler.events import RequestForegroundPushEvent


class TestApp(app.App):
    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self._foregrounded = False

    def update(self, delta):
        if not self._foregrounded:
            eventbus.emit(RequestForegroundPushEvent(self))
            self._foregrounded = True
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()
        return True

    def draw(self, ctx):
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 22
        ctx.move_to(0, -10).text("Cartridge")
        ctx.move_to(0, 15).text("test app")
        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6)
        ctx.move_to(0, 70).text("F to exit")


__app_export__ = TestApp
PY_EOF

cat > "$TMPDIR/cartridge.json" <<'JSON_EOF'
{
    "manifest_version": 1,
    "name": "Cartridge test app",
    "slug": "testapp",
    "version": "1.0.0",
    "files": 2,
    "copied_at": 0
}
JSON_EOF

cat > "$TMPDIR/metadata.json" <<'JSON_EOF'
{
    "name": "Cartridge test app",
    "hidden": false,
    "version": "1.0.0"
}
JSON_EOF

echo "[populate] copying synthetic ${CARTRIDGE_ROOT}/apps/testapp"
mpremote resume cp "$TMPDIR/app.py"          ":${CARTRIDGE_ROOT}/apps/testapp/app.py"
mpremote resume cp "$TMPDIR/cartridge.json"  ":${CARTRIDGE_ROOT}/apps/testapp/cartridge.json"
mpremote resume cp "$TMPDIR/metadata.json"   ":${CARTRIDGE_ROOT}/apps/testapp/metadata.json"

echo "[populate] verifying layout"
mpremote resume exec "
import os
def show(p, depth=0):
    try:
        for e in sorted(os.ilistdir(p)):
            name, kind = e[0], e[1]
            print(' ' * (depth * 2) + name + ('/' if (kind & 0x4000) else ''))
            if kind & 0x4000:
                show(p + '/' + name, depth + 1)
    except OSError as exc:
        print('(error at %s: %s)' % (p, exc))
print('${CARTRIDGE_ROOT}/')
show('${CARTRIDGE_ROOT}', 1)
"

cat <<EOF

[populate] done.

To exercise the full E2E flow now:
  1. mpremote reset
  2. Physically remove + re-insert the Flopagon
  3. On the badge:
     - Our bootstrap runs
     - Installer picker appears with "Cartridge test app v1.0.0"
     - Tap CONFIRM (C) to install
     - Progress bar animates
     - Done screen: "OK Cartridge test app"
     - Tap CANCEL (F) to exit
     - Launcher should now include "Cartridge test app"

Verify the install landed on the badge's internal filesystem:
  mpremote resume ls :apps/testapp/

Cleanup this staging (leaves the badge's installed testapp alone):
  ./cartridge/dev/populate_flopagon.sh --cleanup
EOF
