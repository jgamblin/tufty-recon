"""
Pin down the reproducible hang from tools/scan_modes.py.

That script has stopped the badge three times out of three, always inside its
first mode. It is a real stop rather than a lost connection: the script writes
its result to flash when a mode completes, and nothing was ever written, so the
badge was not still running.

The differences from tools/irq_load.py, which survives the same radio settings
for 90 seconds, are narrow:

  - the interrupt appends a tuple of two freshly allocated bytes objects
  - the main loop drains that list completely, every 50ms, with `while pending`
  - both touch the same list, one from an interrupt and one from the main loop

Each is switched on in turn here, with a breadcrumb on flash before every stage,
so whichever one stops the badge names itself.

    tools/run.sh tools/pin_hang.py
    tools/run.sh tools/read_trace.py
"""

import bluetooth
import machine
import time

TRACE = "/state/hang_trace.txt"
SECS = 20

rnd = 0
adverts = 0
pending = []
stage = "none"


def mark(step, extra=""):
    try:
        with open(TRACE, "w") as f:
            f.write("%d,%s,%d,%d,%s\n" % (
                time.ticks_ms(), step, rnd, adverts, extra))
    except OSError:
        pass


def irq_count(event, data):
    global adverts
    if event == 5:
        adverts += 1


def irq_queue(event, data):
    """Allocates two objects per advertisement and appends from the interrupt."""
    global adverts
    if event != 5:
        return
    _at, addr, _t, _rssi, adv = data
    adverts += 1
    if len(pending) < 400:
        pending.append((bytes(addr), bytes(adv)))


def drain_bounded(n=16):
    got = 0
    while pending and got < n:
        pending.pop()
        got += 1


def drain_unbounded():
    """What scan_modes.py did: empty the whole list while the interrupt is
    still appending to it."""
    while pending:
        pending.pop()


ble = bluetooth.BLE()
ble.active(True)
wdt = machine.WDT(timeout=8000)

STAGES = (
    ("count-only", irq_count, None),
    ("queue+bounded-drain", irq_queue, drain_bounded),
    ("queue+unbounded-drain", irq_queue, drain_unbounded),
)

print("each stage runs %ds with an active 100%% listen" % SECS)
print("a stage that stops the badge is the one that matters")
print()

for i, (name, handler, drain) in enumerate(STAGES, 1):
    rnd = i
    stage = name
    adverts = 0
    del pending[:]
    mark("arming:" + name)
    ble.irq(handler)
    ble.gap_scan(0, 30000, 30000, True)
    mark("running:" + name)

    t0 = time.ticks_ms()
    ticks = 0
    while time.ticks_diff(time.ticks_ms(), t0) < SECS * 1000:
        wdt.feed()
        if drain:
            drain()
        time.sleep_ms(50)
        ticks += 1
        if ticks % 20 == 0:
            mark("running:" + name, "%ds" % (time.ticks_diff(time.ticks_ms(), t0) // 1000))

    mark("stopping:" + name)
    ble.gap_scan(None)
    mark("survived:" + name)
    print("  ok  %-24s adverts=%-6d queued=%d" % (name, adverts, len(pending)))

mark("done")
print()
print("every stage survived; the difference is elsewhere")
