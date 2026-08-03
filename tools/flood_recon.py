"""
Simulate a BLE advertisement flood against recon. Runs on the badge.

People spam Continuity advertisements at conferences to pop pairing dialogs on
nearby phones: hundreds a second, each from a fresh random address. Every one
looks to this app like a brand-new device, which is what locked the display up
before the defence existed.

This drives the app's own interrupt handler at a chosen rate and reports frame
time, so the defence can be checked rather than assumed.

    .venv/bin/mpremote connect $PORT run tools/flood_recon.py
"""

import badgeware  # noqa: F401
import builtins
import os
import random
import sys
import time

RATES = (0, 30, 120, 400, 900)     # advertisements per second to inject
FRAMES = 40

_cap = []


class _R:
    result = None


builtins.run = lambda u: (_cap.append(u), _R())[1]

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)
os.chdir(PATH)
m = __import__(PATH)
update = _cap[0]

# The real radio would fight the measurement.
try:
    m.ble_radio.gap_scan(None)
except Exception:
    pass
m.WIFI_EVERY_MS = 3600_000

# A Continuity-shaped payload: company 0x004C, subtype 0x07, which is what the
# common spammers emit.
PAYLOAD = bytes((0x02, 0x01, 0x06, 0x0A, 0xFF, 0x4C, 0x00, 0x07,
                 0x19, 0x01, 0x02, 0x20, 0x75, 0xAA, 0x30))


def spam(n):
    """Deliver n fresh random advertisements through the app's own interrupt."""
    for _ in range(n):
        addr = bytes((0xC0 | random.getrandbits(6), random.getrandbits(8),
                      random.getrandbits(8), random.getrandbits(8),
                      random.getrandbits(8), random.getrandbits(8)))
        m._irq(5, (1, addr, 0, -55, PAYLOAD))


print("flood defence check  (enter %d/s, exit %d/s, admit %d/frame)"
      % (m.FLOOD_ENTER, m.FLOOD_EXIT, m.ADMIT_PER_FRAME))
print("%-12s %10s %8s %9s %8s" % ("injected", "ms/frame", "flood", "tracked", "dropped"))

for rate in RATES:
    m.view = m.BILLBOARD
    per_frame = rate // 30 if rate else 0
    for _ in range(6):                 # settle, and let flood state latch
        spam(per_frame)
        badge.poll()
        update()
    before = m.flood_dropped
    t = time.ticks_ms()
    for _ in range(FRAMES):
        spam(per_frame)
        badge.poll()
        update()
    ms = time.ticks_diff(time.ticks_ms(), t) / FRAMES
    print("%-12s %9.1f %8s %9d %8d" % (
        "%d/s" % rate, ms, "YES" if m.flood else "no",
        len(m.ble), m.flood_dropped - before))

print()
print("the billboard sleeps %dms a frame by design, so that is the floor."
      % m.BILLBOARD_IDLE_MS)
print("what matters is that it does not climb with the injection rate.")
