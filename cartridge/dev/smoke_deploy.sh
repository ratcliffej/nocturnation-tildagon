#!/usr/bin/env bash
# cartridge/dev/smoke_deploy.sh
#
# Stage the Phase 2 installer + a synthetic "testapp" under
# /apps/nocturnation_cartridge_smoke/ on the badge's internal
# filesystem, so the installer can be validated in isolation before the
# Phase 3 EEPROM bootstrap + Flopagon SPI mount get involved.
#
# Why under /apps/: badge OS updates wipe root-level user-created
# directories. /apps/ persists across updates, so the smoke test
# survives a reflash without a redeploy.
#
# What lands on the badge:
#   /apps/nocturnation_cartridge_smoke/
#       metadata.json                 <- hidden:true keeps it out of launcher
#       installer/
#           app.py, _fsutil.py, _manifest.py, __init__.py
#       apps/
#           testapp/
#               app.py, cartridge.json, metadata.json
#
# The installer's CARTRIDGE_MOUNT is auto-discovered from __file__ at
# import time, so instantiating InstallerApp() when installer.app is
# imported from this path will look for apps at
# /apps/nocturnation_cartridge_smoke/apps/*/.
#
# From the badge REPL:
#   >>> import sys
#   >>> sys.path.append("/apps/nocturnation_cartridge_smoke")
#   >>> from installer.app import InstallerApp
#   >>> from system.scheduler.events import RequestStartAppEvent
#   >>> from system.eventbus import eventbus
#   >>> eventbus.emit(RequestStartAppEvent(InstallerApp()))
#
# Usage:
#   ./cartridge/dev/smoke_deploy.sh              # stage + print launch instructions
#   ./cartridge/dev/smoke_deploy.sh --cleanup    # wipe smoke folders + old /cartridge
#   ./cartridge/dev/smoke_deploy.sh --help

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
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

SMOKE_ROOT="/apps/nocturnation_cartridge_smoke"
INSTALLED_TARGET="/apps/testapp"
OLD_ROOT="/cartridge"   # left over from the pre-persistence smoke layout

if [[ "$MODE" == cleanup ]]; then
    echo "[smoke] wiping ${SMOKE_ROOT}, ${INSTALLED_TARGET}, and ${OLD_ROOT}"
    mpremote exec "
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
rmtree('${SMOKE_ROOT}')
rmtree('${INSTALLED_TARGET}')
rmtree('${OLD_ROOT}')
"
    echo "[smoke] done"
    exit 0
fi

# ------------------------------------------------------------------ deploy

echo "[smoke] wiping any previous ${SMOKE_ROOT} + ${OLD_ROOT}"
mpremote exec "
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

# metadata.json for the wrapper folder itself: hidden:true so it
# doesn't appear in the badge launcher between smoke runs.
cat > "$TMPDIR/wrapper_metadata.json" <<'JSON_EOF'
{
    "name": "Cartridge smoke test",
    "hidden": true,
    "version": "0.0.0"
}
JSON_EOF

echo "[smoke] copying installer sources"
mpremote cp "$TMPDIR/wrapper_metadata.json"  ":${SMOKE_ROOT}/metadata.json"
mpremote cp cartridge/installer/app.py        ":${SMOKE_ROOT}/installer/app.py"
mpremote cp cartridge/installer/_fsutil.py    ":${SMOKE_ROOT}/installer/_fsutil.py"
mpremote cp cartridge/installer/_manifest.py  ":${SMOKE_ROOT}/installer/_manifest.py"
mpremote cp cartridge/installer/__init__.py   ":${SMOKE_ROOT}/installer/__init__.py"

# Synthetic testapp payload: a real Tildagon App that renders a
# label + a cartridge.json manifest for the installer's picker.
cat > "$TMPDIR/app.py" <<'PY_EOF'
# Synthetic testapp for the cartridge installer smoke test. Renders a
# single centred label so a post-install reboot visibly confirms the
# copy landed and the launcher picked the app up.
import app


class TestApp(app.App):
    def draw(self, ctx):
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()
        ctx.rgb(1, 1, 1)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 22
        ctx.move_to(0, -10).text("Cartridge")
        ctx.move_to(0, 15).text("test app")


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

cat <<EOF

[smoke] deploy complete.

Drive the installer from the badge REPL:

    import sys
    sys.path.append("${SMOKE_ROOT}")
    from installer.app import InstallerApp
    from system.scheduler.events import RequestStartAppEvent
    from system.eventbus import eventbus
    eventbus.emit(RequestStartAppEvent(InstallerApp()))

The picker should list "Cartridge test app v1.0.0". Confirming should
copy ${SMOKE_ROOT}/apps/testapp/ -> /apps/testapp/ on the badge and
emit InstallNotificationEvent, at which point the launcher should
show a "Cartridge test app" entry.

Cleanup (wipes smoke folders + /apps/testapp):

    ./cartridge/dev/smoke_deploy.sh --cleanup
EOF
