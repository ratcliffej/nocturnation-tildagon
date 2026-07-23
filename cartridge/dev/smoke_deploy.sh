#!/usr/bin/env bash
# cartridge/dev/smoke_deploy.sh
#
# Stage the Phase 2 installer + a synthetic "testapp" on the badge's
# INTERNAL filesystem at /cartridge/, so the installer can be validated
# in isolation before the Phase 3 EEPROM bootstrap + Flopagon SPI mount
# get involved.
#
# What lands on the badge:
#   /cartridge/installer/{app.py, _fsutil.py, _manifest.py, __init__.py}
#   /cartridge/apps/testapp/{app.py, cartridge.json, metadata.json}
#
# From the badge REPL you can then:
#   >>> import sys
#   >>> sys.path.append("/cartridge")
#   >>> from installer.app import InstallerApp
#   >>> app = InstallerApp()
#   >>> # ...drive the state machine, confirm files land in /apps/testapp/
#
# Or the eventbus route:
#   >>> from system.scheduler.events import RequestStartAppEvent
#   >>> from system.eventbus import eventbus
#   >>> eventbus.emit(RequestStartAppEvent(app))
#
# Usage:
#   ./cartridge/dev/smoke_deploy.sh              # stage + print launch instructions
#   ./cartridge/dev/smoke_deploy.sh --cleanup    # wipe /cartridge/ + /apps/testapp/
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
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ "$MODE" == cleanup ]]; then
    echo "[smoke] wiping /cartridge and /apps/testapp on the badge"
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
rmtree("/cartridge")
rmtree("/apps/testapp")
'
    echo "[smoke] done"
    exit 0
fi

# ------------------------------------------------------------------ deploy

echo "[smoke] creating /cartridge and subfolders on the badge"
mpremote exec '
import os
for p in ("/cartridge", "/cartridge/installer",
         "/cartridge/apps", "/cartridge/apps/testapp"):
    try:
        os.mkdir(p)
    except OSError:
        pass
'

echo "[smoke] copying installer sources"
mpremote cp cartridge/installer/app.py       :cartridge/installer/app.py
mpremote cp cartridge/installer/_fsutil.py   :cartridge/installer/_fsutil.py
mpremote cp cartridge/installer/_manifest.py :cartridge/installer/_manifest.py
mpremote cp cartridge/installer/__init__.py  :cartridge/installer/__init__.py

# Synthetic testapp payload: a valid Tildagon app + a cartridge.json
# manifest so the installer's picker enumerates it. Files are built here
# so the test never touches the real NocturNation sources.
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

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

echo "[smoke] copying synthetic /cartridge/apps/testapp"
mpremote cp "$TMPDIR/app.py"          :cartridge/apps/testapp/app.py
mpremote cp "$TMPDIR/cartridge.json"  :cartridge/apps/testapp/cartridge.json
mpremote cp "$TMPDIR/metadata.json"   :cartridge/apps/testapp/metadata.json

echo "[smoke] verifying layout"
mpremote exec '
import os
def show(p, depth=0):
    try:
        for e in sorted(os.ilistdir(p)):
            name, kind = e[0], e[1]
            print(" " * (depth * 2) + name + ("/" if (kind & 0x4000) else ""))
            if kind & 0x4000:
                show(p + "/" + name, depth + 1)
    except OSError as exc:
        print("(error at %s: %s)" % (p, exc))
print("/cartridge/")
show("/cartridge", 1)
'

cat <<'EOF'

[smoke] deploy complete.

Drive the installer from the badge REPL:

    import sys
    sys.path.append("/cartridge")
    from installer.app import InstallerApp
    from system.scheduler.events import RequestStartAppEvent
    from system.eventbus import eventbus
    app = InstallerApp()
    eventbus.emit(RequestStartAppEvent(app))

The picker should list "Cartridge test app v1.0.0". Confirming should
copy /cartridge/apps/testapp/ -> /apps/testapp/ on the badge and emit
InstallNotificationEvent, at which point the launcher should show a
"Cartridge test app" entry.

Cleanup (wipes /cartridge/ + /apps/testapp/):

    ./cartridge/dev/smoke_deploy.sh --cleanup
EOF
