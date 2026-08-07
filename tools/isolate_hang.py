"""
Narrow down a reproducible hard hang to the exact call that causes it.

tools/scan_modes.py killed the badge at the same point twice: during the first
gap_scan. Console output is useless here because the USB dies with the badge,
so every step is written to flash first and read back afterwards.

    tools/run.sh tools/isolate_hang.py
    tools/run.sh tools/read_trace.py

A watchdog is armed so the badge recovers by itself.
"""

import bluetooth
import machine
import time

TRACE = "/state/hang_trace.txt"
rnd = 0
adverts = 0


def mark(step, extra=""):
    try:
        with open(TRACE, "w") as f:
            f.write("%d,%s,%d,%d,%s\n" % (
                time.ticks_ms(), step, rnd, adverts, extra))
    except OSError:
        pass


def plain_irq(event, data):
    """Deliberately trivial: counts and returns, allocates nothing."""
    global adverts
    if event == 5:
        adverts += 1


def busy_irq(event, data):
    """What scan_modes.py used: walks the payload and writes a dict, from
    inside an interrupt. Both are the kind of thing that is unsafe there."""
    global adverts
    if event != 5:
        return
    _at, addr, _t, _rssi, adv = data
    adverts += 1
    a = bytes(addr)
    b = bytes(adv)
    i = 0
    named = False
    while i + 1 < len(b):
        ln = b[i]
        if ln == 0:
            break
        if b[i + 1] in (0x08, 0x09):
            named = True
        i += ln + 1
    store[a] = store.get(a, False) or named


store = {}
wdt = machine.WDT(timeout=8000)
mark("start")

ble = bluetooth.BLE()
mark("ble_active")
ble.active(True)

print("stepping through each call, breadcrumbs to flash")

STEPS = (
    ("irq_plain", lambda: ble.irq(plain_irq)),
    ("stop_when_not_started", lambda: ble.gap_scan(None)),
    ("sleep_300", lambda: time.sleep_ms(300)),
    ("start_active_100", lambda: ble.gap_scan(0, 30000, 30000, True)),
    ("listen_10s_plain", lambda: _listen(10)),
    ("stop_after_active", lambda: ble.gap_scan(None)),
    ("irq_busy", lambda: ble.irq(busy_irq)),
    ("start_active_again", lambda: ble.gap_scan(0, 30000, 30000, True)),
    ("listen_10s_busy", lambda: _listen(10)),
    ("stop_final", lambda: ble.gap_scan(None)),
)


def _listen(secs):
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < secs * 1000:
        wdt.feed()
        time.sleep_ms(50)


for i, (name, fn) in enumerate(STEPS, 1):
    rnd = i
    wdt.feed()
    mark(name)
    t = time.ticks_ms()
    try:
        fn()
        d = time.ticks_diff(time.ticks_ms(), t)
        print("  ok   %-24s %5dms  adverts=%d" % (name, d, adverts))
    except Exception as e:      # noqa: BLE001 - reporting is the whole point
        mark(name + "_RAISED", repr(e))
        print("  RAISED %-22s %r" % (name, e))

mark("done")
print()
print("survived every step. adverts heard: %d" % adverts)
print("dict entries built from the interrupt: %d" % len(store))
