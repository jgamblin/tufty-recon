"""
Run the app for real, with the real radio, and report what it is doing.

Exported logs only show devices the badge had never seen before, so a silent
log is ambiguous: it means either the app has stopped collecting or the room
genuinely holds nothing new. This distinguishes them by watching the live set,
the flood state and the frame time while it runs.

    tools/run.sh tools/live_watch.py
"""

import badgeware  # noqa: F401
import builtins
import gc
import os
import sys
import time

SECONDS = 60
EVERY = 5

_cap = []


class _R:
    result = None


builtins.run = lambda u: (_cap.append(u), _R())[1]

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)
os.chdir(PATH)
m = __import__(PATH)
update = _cap[0]

print("watching the real radio for %ds" % SECONDS)
print("flood enter %d/s, exit %d/s, confirm %d windows" % (
    m.FLOOD_ENTER, m.FLOOD_EXIT, m.FLOOD_CONFIRM))
print()
print("%6s %7s %6s %7s %7s %7s %8s %9s" % (
    "t", "frame", "FLOOD", "rate/s", "tracked", "inbox", "logged", "free KB"))

t0 = time.ticks_ms()
last = t0
frames = 0
nxt = 0
while time.ticks_diff(time.ticks_ms(), t0) < SECONDS * 1000:
    f0 = time.ticks_ms()
    badge.poll()
    update()
    frames += 1
    el = time.ticks_diff(time.ticks_ms(), t0)
    if el >= nxt:
        nxt += EVERY * 1000
        span = time.ticks_diff(time.ticks_ms(), last)
        print("%5ds %6dms %6s %7d %7d %7d %8d %9d" % (
            el // 1000, span // max(1, frames), "YES" if m.flood else "no",
            m.flood_rate, len(m.ble), len(m.inbox), m.log.ble_count,
            gc.mem_free() // 1024))
        last = time.ticks_ms()
        frames = 0

print()
print("novelty history: %d recent + %d older addresses" % (
    len(m.seen_new), len(m.seen_old)))
print("rotating: %d   dropped by flood: %d" % (
    len(m.rotating) + m.rotating_overflow, m.flood_dropped))
print("wifi tracked: %d   log: %d APs, %d devices" % (
    len(m.wifi), m.log.wifi_count, m.log.ble_count))
print()
if m.flood:
    print("FLOOD IS LATCHED in this room. If this is a quiet room, that is the bug.")
elif len(m.ble) == 0:
    print("NOT TRACKING ANYTHING. The radio may not be delivering advertisements.")
else:
    print("Collecting normally: %d devices in range." % len(m.ble))
