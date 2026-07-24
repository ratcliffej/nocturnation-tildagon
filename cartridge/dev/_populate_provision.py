"""Runs on the badge via `mpremote mount . run` from the populate
script. Copies the installer + synthetic testapp from the mounted
host repo (/remote/cartridge/{installer,dev/testapp}/) to the
Flopagon flash at /cartridge/{installer,apps/testapp}/. All in one
mpremote session so a mid-op contact drop can't strand us between
mkdir and cp.

Not a badge app - no __app_export__. Not launched by the launcher.
"""

import os

CARTRIDGE_ROOT = "/cartridge"
INSTALLER_DIR = CARTRIDGE_ROOT + "/installer"
APPS_DIR = CARTRIDGE_ROOT + "/apps"
TESTAPP_DIR = APPS_DIR + "/testapp"

REPO_INSTALLER = "/remote/cartridge/installer"
REPO_TESTAPP = "/remote/cartridge/dev/testapp"


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


def mkdir_p(p):
    try:
        os.mkdir(p)
    except OSError:
        pass


def copy_file(src, dst):
    with open(src, "rb") as fr:
        with open(dst, "wb") as fw:
            while True:
                chunk = fr.read(4096)
                if not chunk:
                    break
                fw.write(chunk)


def show(p, depth=0):
    try:
        for e in sorted(os.ilistdir(p)):
            name, kind = e[0], e[1]
            print(" " * (depth * 2) + name + ("/" if (kind & 0x4000) else ""))
            if kind & 0x4000:
                show(p + "/" + name, depth + 1)
    except OSError as exc:
        print("(error at %s: %s)" % (p, exc))


try:
    os.listdir(CARTRIDGE_ROOT)
except OSError:
    print("ERROR: %s not mounted. Insert Flopagon and confirm the Phase 3"
          " bootstrap ran - /cartridge/ should appear in"
          " `mpremote resume ls`." % CARTRIDGE_ROOT)
    raise SystemExit(1)

print("[populate] wiping previous installer + apps")
rmtree(INSTALLER_DIR)
rmtree(APPS_DIR)

print("[populate] creating directories")
mkdir_p(INSTALLER_DIR)
mkdir_p(APPS_DIR)
mkdir_p(TESTAPP_DIR)

print("[populate] copying installer sources")
for f in ("app.py", "_fsutil.py", "_manifest.py", "__init__.py"):
    copy_file(REPO_INSTALLER + "/" + f, INSTALLER_DIR + "/" + f)

print("[populate] copying synthetic testapp")
for f in ("app.py", "cartridge.json", "metadata.json"):
    copy_file(REPO_TESTAPP + "/" + f, TESTAPP_DIR + "/" + f)

print("[populate] verifying layout")
print(CARTRIDGE_ROOT + "/")
show(CARTRIDGE_ROOT, 1)

print("[populate] done")
