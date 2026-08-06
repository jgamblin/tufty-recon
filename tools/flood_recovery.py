"""
Once a flood latches, does the badge ever come back?

This is the failure that took recon down four times in one day at BSides. The
detector measured novelty against the live set, so latching drained that set,
and a drained set made every device in the room look new again. The rate went
up as the room emptied, exit was unreachable, and collection stayed dead until
someone restarted the app by hand.

The sequence here is spam, then silence, then an ordinary room, and the only
thing that matters is that the middle column returns to "no" and the badge
starts tracking devices again on its own.

    tools/run.sh tools/flood_recovery.py
"""

import badgeware  # noqa: F401
import builtins
import os
import random
import sys
import time

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
    m.ble_radio.gap_scan(None)
except Exception:
    pass
m.WIFI_EVERY_MS = 3600_000
m.log.add_ble = lambda *a: None       # the logger is not what is under test
m.log.add_wifi = lambda *a: None

SPAM = bytes((0x06, 0xFF, 0x06, 0x00, 0x03, 0x00, 0x80))    # Swift Pair spam
REAL = bytes((0x02, 0x01, 0x06, 0x03, 0x03, 0x0D, 0x18))    # a heart-rate belt

random.seed(7)
ROOM = [bytes((0x00, 0x1A, 0x7D, random.getrandbits(8),
               random.getrandbits(8), random.getrandbits(8)))
        for _ in range(40)]


def spam(n):
    for _ in range(n):
        m._irq(5, (1, bytes((0xC0 | random.getrandbits(6),
                             random.getrandbits(8), random.getrandbits(8),
                             random.getrandbits(8), random.getrandbits(8),
                             random.getrandbits(8))), 0, -55, SPAM))


def room():
    """The same forty devices every time, re-advertising."""
    for a in ROOM:
        m._irq(5, (0, a, 0, -60, REAL))


def phase(name, seconds, feed):
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < seconds * 1000:
        feed()
        badge.poll()
        update()
    print("  %-26s flood=%-5s rate=%4d/s  tracked=%4d" % (
        name, m.flood, m.flood_rate, len(m.ble)))
    return m.flood


print("flood recovery  (enter %d/s, exit %d/s)" % (m.FLOOD_ENTER, m.FLOOD_EXIT))
print()

phase("1. quiet room, 40 devices", 4, room)
latched = phase("2. spammer arrives", 6, lambda: spam(40))
if not latched:
    print()
    print("FAIL: the spam never registered as a flood; nothing else is meaningful")
    raise SystemExit

still = phase("3. spammer leaves, same room", 8, room)
tracked = len(m.ble)

print()
if still:
    print("FAIL: still flooding with only 40 familiar devices present.")
    print("      This is the BSides lockup: latched, and no way back.")
elif tracked < len(ROOM) // 2:
    print("PARTIAL: flood cleared, but only %d entries are tracked and the room"
          % tracked)
    print("         has %d devices in it." % len(ROOM))
else:
    # Higher than the room size is expected and fine: the addresses the
    # spammer got in before the latch are still tracked, and age out of the
    # live set once they go unheard for WINDOW_MS.
    print("PASS: flood cleared on its own, %d entries tracked (%d real devices"
          % (tracked, len(ROOM)))
    print("      plus spam admitted before the latch, which ages out).")
