"""
Does a busy-but-honest room trip the flood detector?

A flood is meant to mean "someone is generating fresh addresses at you". This
drives the app's own interrupt with a FIXED set of devices, each re-advertising
the way real hardware does, and asks whether the badge calls that a flood.

If it does, the detector is measuring advertisement volume rather than address
novelty, and a crowded room will latch it with nobody attacking.

    tools/run.sh tools/busy_room.py
"""

import badgeware  # noqa: F401
import builtins
import os
import random
import sys
import time

DEVICES = (50, 200, 600)     # distinct devices in the room
ADS_EACH = 4                 # advertisements per device per second, typical
SECONDS = 3

_cap = []


class _R:
    result = None


builtins.run = lambda u: (_cap.append(u), _R())[1]

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)
os.chdir(PATH)
m = __import__(PATH)
update = _cap[0]

try:
    m.ble_radio.gap_scan(None)      # the real radio would fight the measurement
except Exception:
    pass
m.WIFI_EVERY_MS = 3600_000

# What is under test is the flood detector, not the logger. Once the detector
# stopped dropping everything, the run started writing hundreds of records to
# flash and outlasted the USB connection.
m.log.add_ble = lambda *a: None
m.log.add_wifi = lambda *a: None

PAYLOAD = bytes((0x02, 0x01, 0x06, 0x03, 0x03, 0x0D, 0x18))   # a heart-rate belt


def room(n):
    """A fixed population. Public addresses, so nothing here rotates and
    nothing here is a spammer."""
    random.seed(n)
    return [bytes((0x00, 0x1A, 0x7D, random.getrandbits(8),
                   random.getrandbits(8), random.getrandbits(8)))
            for _ in range(n)]


print("busy room check  (enter %d/s, exit %d/s)" % (m.FLOOD_ENTER, m.FLOOD_EXIT))
print("every address below is a real device re-advertising; none are new.")
print()
print("%8s %8s %10s %8s %9s %8s" % (
    "devices", "ads/sec", "flood_rate", "FLOOD", "tracked", "logged"))

for n in DEVICES:
    addrs = room(n)
    m.flood = False
    m.flood_rate = 0
    m.flood_seen = 0
    m.ble.clear()
    del m.inbox[:]

    t0 = time.ticks_ms()
    frames = 0
    while time.ticks_diff(time.ticks_ms(), t0) < SECONDS * 1000:
        # One frame's worth of advertisements from the whole room.
        for a in addrs:
            for _ in range(ADS_EACH // 2 or 1):
                m._irq(5, (0, a, 0, -60, PAYLOAD))
        badge.poll()
        update()
        frames += 1

    span = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
    ads = n * (ADS_EACH // 2 or 1) * frames
    print("%8d %8d %10d %8s %9d %8d" % (
        n, int(ads / span), m.flood_rate, "YES" if m.flood else "no",
        len(m.ble), m.log.ble_count))

print()
print("a YES on any row means the detector counts advertisements, not devices.")

# Can it ever get out? Under flood the interrupt drops every address before it
# can be admitted, so the live set drains and then every advertisement in the
# room looks new all over again.
if m.flood:
    print()
    print("stuck test: flood is latched; feeding the SAME room for 6 more seconds")
    addrs = room(DEVICES[-1])
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 6000:
        for a in addrs:
            m._irq(5, (0, a, 0, -60, PAYLOAD))
        badge.poll()
        update()
    print("  after 6s more: flood=%s  rate=%d/s  tracked=%d" % (
        m.flood, m.flood_rate, len(m.ble)))
    print("  (if flood is still True with a fixed room, it cannot recover)")
