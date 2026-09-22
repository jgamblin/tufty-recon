#!/usr/bin/env python3
"""
Build a drag-and-drop release zip.

Installing from source needs a Python venv and mpremote, which is a fine ask
for a developer and a total non-starter for someone standing in a hallway with
four minutes. The badge already mounts as a USB drive, so the zip is the whole
install: open Mass Storage on the badge, drag the app folders in, eject.

    python3 tools/make_release.py
    python3 tools/make_release.py --apps recon badge_hunt

Produces dist/tufty-badgeware-<date>.zip containing:

    README.txt          three steps, no toolchain
    install-macos.command  double-clickable, copies without clobbering
    apps/<name>/...     drop these into TUFTY/apps/
"""

import argparse
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
APPS = ROOT
DIST = os.path.join(ROOT, "dist")

SKIP = {"__pycache__", ".DS_Store"}

README = """\
recon - a WiFi and Bluetooth scanner for the Pimoroni Tufty 2350
================================================================

Identifies what it sees rather than listing MAC addresses: manufacturer,
product and protocol, from databases carried on the badge. Dashboard by device
kind, drill-down detail, flags for open networks and trackers, a persistent
log, and CSV export.

Entirely passive. It never associates, transmits or captures traffic.

Install (no software needed)
----------------------------

1. Plug the badge into USB.
2. On the badge, open the "Mass Storage" app. A drive called TUFTY appears.
3. Open TUFTY, go into the "apps" folder, and drag the app folders from this
   zip's "apps" folder into it.
4. Eject TUFTY. The badge reboots and the new icons are in the launcher.

IMPORTANT: drag the app folders INTO TUFTY/apps. Do not drop the whole "apps"
folder onto TUFTY, because macOS will offer to replace the folder rather than
merge it, and that removes the apps the badge shipped with.

macOS users can instead double-click install-macos.command, which copies each
app individually and cannot clobber anything.

Requires badgeware firmware v3.1.0 or newer:
https://github.com/pimoroni/tufty2350/releases

Personalise
-----------

Source, and how it all works:
https://github.com/jgamblin/tufty-recon
"""

INSTALLER = """\
#!/bin/bash
# Double-click to install. Copies each app individually into TUFTY/apps so it
# can never replace the folder the badge shipped with.
set -u
cd "$(dirname "$0")"

VOL="/Volumes/TUFTY"
if [ ! -d "$VOL" ]; then
  echo "The badge is not mounted."
  echo
  echo "Plug it in, open the 'Mass Storage' app on the badge, then run this again."
  echo
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi

mkdir -p "$VOL/apps"
for app in apps/*/; do
  name=$(basename "$app")
  echo "installing $name"
  rm -rf "$VOL/apps/$name"
  cp -R "$app" "$VOL/apps/$name"
done

sync
find "$VOL" -name '._*' -delete 2>/dev/null
find "$VOL" -name '.DS_Store' -delete 2>/dev/null
sync

echo
echo "Done. Ejecting; the badge will reboot into its launcher."
diskutil eject "$VOL" >/dev/null 2>&1
echo
read -n 1 -s -r -p "Press any key to close."
"""


def stage(apps, tmp):
    out = os.path.join(tmp, "apps")
    os.makedirs(out, exist_ok=True)
    total = 0
    for name in apps:
        src = os.path.join(APPS, name)
        if not os.path.isdir(src):
            sys.exit("no such app: %s" % name)
        dst = os.path.join(out, name)
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns(*SKIP))
        for base, _dirs, files in os.walk(dst):
            for f in files:
                total += os.path.getsize(os.path.join(base, f))
    return out, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", nargs="*", help="default: every app")
    args = ap.parse_args()

    apps = args.apps or ["recon"]

    os.makedirs(DIST, exist_ok=True)
    tmp = os.path.join(DIST, ".stage")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    staged, raw = stage(apps, tmp)

    with open(os.path.join(tmp, "README.txt"), "w") as f:
        f.write(README)
    installer = os.path.join(tmp, "install-macos.command")
    with open(installer, "w") as f:
        f.write(INSTALLER)
    os.chmod(installer, 0o755)

    name = "tufty-recon-%s.zip" % time.strftime("%Y%m%d")
    path = os.path.join(DIST, name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(tmp):
            for f in files:
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, tmp))

    shutil.rmtree(tmp, ignore_errors=True)
    print("wrote %s" % os.path.relpath(path, ROOT))
    print("  %.1f KB unpacked, %.1f KB zipped  (%s)"
          % (raw / 1024, os.path.getsize(path) / 1024, ", ".join(apps)))


if __name__ == "__main__":
    main()
