#!/usr/bin/env bash
#
# Run an on-device script, locating the badge automatically.
#
#   tools/run.sh tools/smoke.py
#   tools/run.sh tools/stress_recon.py
#
# `mpremote devs` reports the USB manufacturer, so the badge is picked out from
# hubs, dongles and other boards rather than whatever serial device happens to
# sort first. Note that connecting halts the running app: the badge sits at a
# REPL with a dark screen until it is reset.

set -euo pipefail
cd "$(dirname "$0")/.."

MPR="${MPR:-.venv/bin/mpremote}"
[[ -x "$MPR" ]] || MPR="$(command -v mpremote || true)"
if [[ -z "$MPR" ]]; then
  echo "mpremote not found. Run: python3 -m venv .venv && .venv/bin/pip install mpremote" >&2
  exit 1
fi

PORT="${TUFTY_PORT:-$("$MPR" devs 2>/dev/null | grep -i -m1 'tufty' | cut -d' ' -f1)}"
if [[ -z "$PORT" ]]; then
  echo "No Tufty found. Plug it in, or set TUFTY_PORT." >&2
  "$MPR" devs 2>/dev/null | grep -v '0000:0000' >&2 || true
  exit 1
fi

"$MPR" connect "$PORT" run "$@"
echo
echo "The badge is now at a REPL with a blank screen. Reset it with:"
echo "  $MPR connect $PORT exec 'import machine; machine.reset()'"
