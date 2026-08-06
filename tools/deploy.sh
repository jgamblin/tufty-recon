#!/usr/bin/env bash
#
# Push recon to a connected Tufty 2350.
#
#   tools/deploy.sh
#   TUFTY_PORT=/dev/cu.usbmodem1101 tools/deploy.sh
#
# /system is read-only from MicroPython, so installing means putting the badge
# into USB mass storage mode and writing to the mounted volume. This does that,
# copies, then ejects, which reboots the badge into its launcher.
#
# macOS mounts removable volumes under /Volumes and Linux usually under
# /run/media or /media, so both are handled. On Windows, use the drag-and-drop
# zip from Releases instead.

set -euo pipefail

cd "$(dirname "$0")/.."

MPR="${MPR:-.venv/bin/mpremote}"
[[ -x "$MPR" ]] || MPR="$(command -v mpremote || true)"

find_volume() {
  for v in /Volumes/TUFTY "/run/media/$USER/TUFTY" "/media/$USER/TUFTY" /media/TUFTY; do
    [[ -d "$v" ]] && { echo "$v"; return 0; }
  done
  # Not mounted is an ordinary outcome, not a failure. Falling off the end of
  # the loop returns the failed test's status, which under `set -e` kills the
  # caller silently at `VOL="$(find_volume)"`.
  return 0
}

eject_volume() {
  local v="$1"
  if command -v diskutil >/dev/null 2>&1; then
    diskutil eject "$v" >/dev/null
  elif command -v udisksctl >/dev/null 2>&1; then
    udisksctl unmount -b "$(findmnt -no SOURCE "$v")" >/dev/null
  else
    sync
    echo "Could not eject automatically. Unmount $v yourself, then the badge reboots."
    return
  fi
}

VOL="$(find_volume)"

if [[ -z "$VOL" ]]; then
  if [[ -z "$MPR" || ! -x "$MPR" ]]; then
    echo "The badge is not mounted, and mpremote is not installed to mount it." >&2
    echo "Either open the badge's 'Mass Storage' app yourself, or:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install mpremote" >&2
    exit 1
  fi
  # `mpremote devs` reports the USB manufacturer, so the badge can be picked
  # out from hubs, dongles and other boards rather than grabbing whatever
  # serial device happens to sort first.
  PORT="${TUFTY_PORT:-$("$MPR" devs 2>/dev/null | grep -i -m1 'tufty' | cut -d' ' -f1)}"
  if [[ -z "$PORT" ]]; then
    echo "No Tufty found. Plug it in, or set TUFTY_PORT." >&2
    echo "Connected devices:" >&2
    "$MPR" devs 2>/dev/null | grep -v '0000:0000' >&2 || true
    exit 1
  fi
  echo "Found badge on $PORT"

  echo "Switching badge to mass storage mode..."
  # This call never returns: the device re-enumerates as a USB disk while the
  # REPL is still open. Fire it off and stop waiting once the volume appears.
  "$MPR" connect "$PORT" exec "import _msc" >/dev/null 2>&1 &
  trigger=$!
  for _ in $(seq 1 30); do
    VOL="$(find_volume)"
    [[ -n "$VOL" ]] && break
    sleep 1
  done
  kill "$trigger" 2>/dev/null || true
  wait "$trigger" 2>/dev/null || true

  if [[ -z "$VOL" ]]; then
    echo "Badge did not appear as a TUFTY volume." >&2
    echo "Open the 'Mass Storage' app on the badge and re-run." >&2
    exit 1
  fi
fi

echo "Deploying to $VOL"
mkdir -p "$VOL/apps"
# Replace rather than merge, so a file left behind by an older layout cannot
# still be imported.
rm -rf "${VOL:?}/apps/recon"
cp -R recon "$VOL/apps/recon"

sync
# macOS writes resource forks onto FAT volumes; they waste space and clutter
# the launcher's directory scan.
command -v dot_clean >/dev/null 2>&1 && dot_clean "$VOL" 2>/dev/null || true
find "$VOL" -name '._*' -delete 2>/dev/null || true
find "$VOL" -name '.DS_Store' -delete 2>/dev/null || true
sync

# Verify against the volume before ejecting. A copy onto a stale mount left by
# an earlier run, or a partial write, otherwise reports success and leaves the
# badge running the previous build. That has happened twice, and both times it
# was found by noticing a constant had not changed.
if ! diff -r recon "$VOL/apps/recon" >/dev/null 2>&1; then
  echo "Copy did not land: $VOL/apps/recon differs from ./recon" >&2
  diff -rq recon "$VOL/apps/recon" >&2 || true
  exit 1
fi
echo "Verified on the volume."

echo "Ejecting (the badge will reboot into the launcher)..."
eject_volume "$VOL"

# And verify again once it is back, because the check above can be satisfied by
# the page cache while the eject fails to flush to the card.
if [[ -n "$MPR" && -x "$MPR" ]]; then
  echo "Waiting for the badge to come back..."
  PORT=""
  for _ in $(seq 1 30); do
    sleep 1
    # `|| true` is load-bearing twice over. While the badge is still rebooting
    # grep matches nothing and exits 1, which `pipefail` turns into a failed
    # assignment and `set -e` turns into a silent exit — the exact failure this
    # whole verification block exists to catch. And the test must not be the
    # loop's last command, or a final miss makes the loop itself return 1.
    PORT="${TUFTY_PORT:-$("$MPR" devs 2>/dev/null | grep -i -m1 'tufty' | cut -d' ' -f1 || true)}"
    if [[ -n "$PORT" ]]; then
      break
    fi
  done
  if [[ -z "$PORT" ]]; then
    echo "Badge did not re-enumerate, so the install could not be confirmed." >&2
    echo "Re-run once it is back on USB." >&2
    exit 1
  fi
  # Sum the bytes of every deployed source file and compare. Sizes alone would
  # miss an edit that happens to preserve length, which is exactly the shape of
  # a changed threshold. Both sides sum and count the same bytes, so the order
  # files are visited in does not matter.
  GOT="$("$MPR" connect "$PORT" exec "
import os
def walk(d):
    for e in sorted(os.listdir(d)):
        p = d + '/' + e
        if os.stat(p)[0] & 0x4000:
            for q in walk(p): yield q
        elif e.endswith('.py'):
            yield p
n = 0
data = bytearray()
for p in walk('/system/apps/recon'):
    b = open(p, 'rb').read()
    data.extend(b); n += len(b)
print(sum(data), n)
" 2>/dev/null | tr -d '\r' | tail -1)"
  WANT="$(find recon -name '*.py' | sort | xargs cat | \
          python3 -c "import sys; d=sys.stdin.buffer.read(); print(sum(d), len(d))")"
  if [[ "$GOT" != "$WANT" ]]; then
    echo "Install did not stick: the badge reports [$GOT], expected [$WANT]." >&2
    echo "The eject probably beat the flush. Re-run the deploy." >&2
    exit 1
  fi
  echo "Confirmed on the badge: $GOT"
fi
echo "Done."
