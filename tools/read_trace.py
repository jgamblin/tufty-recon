"""
Read the breadcrumb left by tools/hunt_hang.py.

After a hang the badge cannot tell you anything, but the last thing it wrote to
flash before freezing can. Run this once it is back.

    tools/run.sh tools/read_trace.py
"""

TRACE = "/state/hang_trace.txt"

WHERE = {
    "boot": "the hunt had only just started",
    "gap_scan_stop": "stopping the BLE listen, with the WiFi scan next",
    "wlan_scan": "inside wlan.scan(), holding the radio",
    "gap_scan_start": "re-arming the BLE listen after a WiFi scan",
    "gap_scan_start_FAILED": "the re-arm raised, which the app used to swallow",
    "idle": "between rounds, not inside a radio call",
    "done": "it finished every round without hanging",
}

try:
    raw = open(TRACE).read().strip()
except OSError:
    raise SystemExit("No trace file. Run tools/hunt_hang.py first.")

if not raw:
    raise SystemExit("Trace file is empty.")

parts = raw.split(",")
ticks, step, rnd, adverts = parts[0], parts[1], parts[2], parts[3]
extra = parts[4] if len(parts) > 4 else ""

print("last breadcrumb before the badge stopped writing:")
print("  round      %s" % rnd)
print("  step       %s" % step)
print("  ticks_ms   %s" % ticks)
print("  adverts    %s" % adverts)
if extra.strip():
    print("  detail     %s" % extra.strip())
print()
print("  meaning:   %s" % WHERE.get(step, "unrecognised step"))
print()
if step == "done":
    print("No hang was reproduced in that run.")
elif step == "idle":
    print("It stopped between radio calls, so the radio handover is not the")
    print("culprit. Look elsewhere: the display, the filesystem, or PSRAM.")
else:
    print("It stopped INSIDE a radio call. That is the handover race, and it")
    print("is a firmware-level block that no Python-level guard can prevent.")
