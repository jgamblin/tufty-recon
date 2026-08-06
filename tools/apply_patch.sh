#!/usr/bin/env bash
#
# Deploy the pending fix and verify it, in one go. Run this with the badge
# plugged in.
#
#   tools/apply_patch.sh
#
# Deploys, then runs every check that can be run without waiting for a
# conference: the new BLE-listen revival logic, the flood rules that were
# fixed yesterday, and a smoke run of the whole app. Stops at the first
# failure rather than reporting success it has not earned.

set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "Pulling the health log before anything is deployed"
# Whatever is on there now describes the stalls that prompted this patch, and a
# deploy is a good way to lose it.
mkdir -p exports
.venv/bin/python tools/health_report.py > "exports/health-before-patch.txt" 2>&1 || \
  echo "(no readable health log yet; continuing)"
tail -n 20 "exports/health-before-patch.txt" || true

step "Deploying"
tools/deploy.sh

step "Verifying the BLE listen is watched and revived"
tools/run.sh tools/verify_scan_revival.py

step "Verifying the flood rules still behave"
tools/run.sh tools/busy_room.py
tools/run.sh tools/flood_recovery.py

step "Smoke test"
tools/run.sh tools/smoke.py

step "Restarting the badge into recon"
PORT="$(.venv/bin/mpremote devs 2>/dev/null | grep -i -m1 tufty | cut -d' ' -f1 || true)"
if [[ -n "$PORT" ]]; then
  .venv/bin/mpremote connect "$PORT" exec 'import machine; machine.reset()' >/dev/null 2>&1 || true
  echo "reset sent"
fi

printf '\n\033[1mAll checks passed.\033[0m The badge is running the patched build.\n'
echo "Watch it live for a minute with:  tools/run.sh tools/live_watch.py"
