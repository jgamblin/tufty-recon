"""
Read the scan-mode comparison off flash.

tools/scan_modes.py writes each result as it completes, so a dropped USB
connection costs at most the mode that was running.

    tools/run.sh tools/read_modes.py
"""

OUT = "/state/scan_modes.txt"

try:
    rows = [ln.strip().split(",") for ln in open(OUT) if ln.strip()]
except OSError:
    raise SystemExit("No results. Run tools/scan_modes.py first.")

if not rows:
    raise SystemExit("Results file is empty.")

print("%-14s %9s %8s %7s %7s" % ("mode", "adverts/s", "devices", "named", "named%"))
data = {}
for r in rows:
    if len(r) != 4:
        continue
    name, rate, dev, named = r[0], float(r[1]), int(r[2]), int(r[3])
    data[name] = (rate, dev, named)
    print("%-14s %9.1f %8d %7d %6d%%" % (
        name, rate, dev, named, (100 * named // dev) if dev else 0))

print()
if "active-100" in data and "passive-100" in data:
    a, p = data["active-100"], data["passive-100"]
    print("active vs passive, same duty cycle:")
    print("  interrupt rate  %.1f/s -> %.1f/s" % (a[0], p[0]))
    print("  devices found   %d -> %d" % (a[1], p[1]))
    print("  names resolved  %d -> %d" % (a[2], p[2]))
    lost = a[2] - p[2]
    if lost > 0:
        print("  passive costs %d name%s: that is what the scan request buys."
              % (lost, "" if lost == 1 else "s"))
    else:
        print("  passive costs no names here, so the transmission buys nothing")
        print("  that this room can show.")
if "passive-100" in data and "passive-25" in data:
    f, q = data["passive-100"], data["passive-25"]
    print()
    print("duty cycle, passive throughout:")
    print("  100%% -> 25%%: interrupt rate %.1f/s -> %.1f/s, devices %d -> %d"
          % (f[0], q[0], f[1], q[1]))
    print("  a lower duty cycle samples the room instead of hearing all of it;")
    print("  devices advertise repeatedly, so most are still found.")
