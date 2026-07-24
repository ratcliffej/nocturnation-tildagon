#!/usr/bin/env bash
# cartridge/dev/smoke_deploy.sh
#
# Stage the Phase 2 installer + a synthetic "testapp" under
# /apps/nocturnation_cartridge_smoke/ on the badge and register it as
# a first-class badge app so the badge launcher can start it the
# normal way. Avoids the REPL-based launch path, which hits a circular
# import in system.eventbus / system.scheduler when the scheduler
# hasn't gone through its normal boot init.
#
# Layout on the badge:
#   /apps/nocturnation_cartridge_smoke/
#       app.py           # thin wrapper: __app_export__ = InstallerApp
#       __init__.py
#       metadata.json    # visible in launcher as "Cartridge smoke test"
#       installer/
#           app.py, _fsutil.py, _manifest.py, __init__.py
#       apps/
#           testapp/
#               app.py, cartridge.json, metadata.json
#
# CARTRIDGE_MOUNT in installer/app.py is discovered from __file__ at
# import time, so it correctly reads
# /apps/nocturnation_cartridge_smoke/apps/*/ without any config.
#
# Workflow:
#   1. Run this script (deploys + resets the badge).
#   2. On the badge launcher, tap "Cartridge smoke test".
#   3. Picker should list "Cartridge test app v1.0.0" -> confirm.
#   4. Progress bar + done screen.
#   5. Cancel back to launcher; verify "Cartridge test app" now shows.
#   6. Optional: soft-reboot and confirm it survives.
#   7. ./cartridge/dev/smoke_deploy.sh --cleanup when done.
#
# Usage:
#   ./cartridge/dev/smoke_deploy.sh              # deploy + reset
#   ./cartridge/dev/smoke_deploy.sh --no-reset   # deploy, skip reset
#   ./cartridge/dev/smoke_deploy.sh --cleanup    # wipe smoke folders + old /cartridge
#   ./cartridge/dev/smoke_deploy.sh --help

set -euo pipefail

MODE=deploy
RESET=1
for arg in "$@"; do
    case "$arg" in
        --cleanup) MODE=cleanup ;;
        --no-reset) RESET=0 ;;
        --help|-h) MODE=help ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$MODE" == help ]]; then
    sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

SMOKE_ROOT="/apps/nocturnation_cartridge_smoke"
INSTALLED_TARGET="/apps/testapp"
OLD_ROOT="/cartridge"   # left over from the pre-persistence layout

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

if [[ "$MODE" == cleanup ]]; then
    echo "[smoke] wiping ${SMOKE_ROOT}, ${INSTALLED_TARGET}, and ${OLD_ROOT}"
    mpremote exec "${RMTREE_PY}
rmtree('${SMOKE_ROOT}')
rmtree('${INSTALLED_TARGET}')
rmtree('${OLD_ROOT}')
"
    if [[ "$RESET" -eq 1 ]]; then
        echo "[smoke] resetting badge"
        mpremote reset
    fi
    echo "[smoke] done"
    exit 0
fi

# ------------------------------------------------------------------ deploy

echo "[smoke] wiping any previous ${SMOKE_ROOT} + ${OLD_ROOT}"
mpremote exec "${RMTREE_PY}
rmtree('${SMOKE_ROOT}')
rmtree('${OLD_ROOT}')
"

echo "[smoke] creating ${SMOKE_ROOT}/{installer,apps/testapp}"
mpremote exec "
import os
for p in ('${SMOKE_ROOT}',
          '${SMOKE_ROOT}/installer',
          '${SMOKE_ROOT}/apps',
          '${SMOKE_ROOT}/apps/testapp'):
    try:
        os.mkdir(p)
    except OSError:
        pass
"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Wrapper app.py: makes the smoke folder a normal launcher-visible
# badge app. Delegates to InstallerApp via __app_export__.
cat > "$TMPDIR/wrapper_app.py" <<'PY_EOF'
"""Smoke-test wrapper for the cartridge installer.

Presents the installer as a normal badge app the launcher can start
directly. Only used for Phase 2 dev; Phase 3 launches the installer
through the EEPROM bootstrap instead.
"""

from .installer.app import InstallerApp

__app_export__ = InstallerApp
PY_EOF

cat > "$TMPDIR/wrapper_init.py" <<'PY_EOF'
# Wrapper package for the cartridge installer smoke test.
PY_EOF

cat > "$TMPDIR/wrapper_metadata.json" <<'JSON_EOF'
{
    "name": "Cartridge smoke test",
    "hidden": false,
    "version": "0.0.0"
}
JSON_EOF

echo "[smoke] copying wrapper + installer sources"
mpremote cp "$TMPDIR/wrapper_app.py"          ":${SMOKE_ROOT}/app.py"
mpremote cp "$TMPDIR/wrapper_init.py"         ":${SMOKE_ROOT}/__init__.py"
mpremote cp "$TMPDIR/wrapper_metadata.json"   ":${SMOKE_ROOT}/metadata.json"
mpremote cp cartridge/installer/app.py        ":${SMOKE_ROOT}/installer/app.py"
mpremote cp cartridge/installer/_fsutil.py    ":${SMOKE_ROOT}/installer/_fsutil.py"
mpremote cp cartridge/installer/_manifest.py  ":${SMOKE_ROOT}/installer/_manifest.py"
mpremote cp cartridge/installer/__init__.py   ":${SMOKE_ROOT}/installer/__init__.py"

# Synthetic testapp payload: a real Tildagon App that renders a
# label + a cartridge.json manifest for the installer's picker.
# NOTE: base App.update() returns False, which suppresses draw() -
# the app MUST override update() to return non-False, or the screen
# never repaints and the app "hangs" from the operator's view.
cat > "$TMPDIR/app.py" <<'PY_EOF'
# Synthetic testapp for the cartridge installer smoke test. Renders a
# label + handles CANCEL so the operator can exit back to the launcher.
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
        return True  # return non-False so the scheduler calls draw()

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

echo "[smoke] copying synthetic ${SMOKE_ROOT}/apps/testapp"
mpremote cp "$TMPDIR/app.py"          ":${SMOKE_ROOT}/apps/testapp/app.py"
mpremote cp "$TMPDIR/cartridge.json"  ":${SMOKE_ROOT}/apps/testapp/cartridge.json"
mpremote cp "$TMPDIR/metadata.json"   ":${SMOKE_ROOT}/apps/testapp/metadata.json"

echo "[smoke] verifying layout"
mpremote exec "
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
print('${SMOKE_ROOT}/')
show('${SMOKE_ROOT}', 1)
"

if [[ "$RESET" -eq 1 ]]; then
    echo "[smoke] resetting badge (launcher rescan)"
    mpremote reset
fi

cat <<EOF

[smoke] deploy complete.

Next steps on the badge:
  1. Wait for the badge to finish rebooting.
  2. On the launcher, scroll to "Cartridge smoke test" and tap CONFIRM (C).
  3. Picker should show "Install Cartridge test app v1.0.0" -> tap CONFIRM.
  4. Progress bar animates, done screen shows "OK Cartridge test app".
  5. Tap CANCEL (F) to exit; the launcher should now include a
     "Cartridge test app" entry.

Verify install landed on disk:
  mpremote exec 'import os; print(os.listdir("${INSTALLED_TARGET}"))'

Cleanup:
  ./cartridge/dev/smoke_deploy.sh --cleanup
EOF
