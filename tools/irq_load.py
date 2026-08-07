"""
Is the interrupt handler's own workload what wedges the firmware?

recon's IRQ runs on every advertisement and does real work: allocates a bytes
for the address, looks it up in a dict, tests two sets, adds to one, and for a
new device allocates a copy of the whole payload. This room delivers about 46
advertisements a second. A DEF CON floor delivers orders of magnitude more, and
that is the variable that tracks the freezes.

Two handlers are compared over the same continuous listen: one that only counts,
and one that does recon-shaped work. Breadcrumbs go to flash because a hang
takes USB with it.

    tools/run.sh tools/irq_load.py [seconds]
    tools/run.sh tools/read_trace.py

A watchdog is armed, so a hang ends in a reboot rather than a dead badge.
"""

import bluetooth
import gc
import machine
import sys
import time

TRACE = "/state/hang_trace.txt"
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 90

rnd = 0
adverts = 0
live = {}
seen_a = set()
seen_b = set()
inbox = []


def mark(step, extra=""):
    try:
        with open(TRACE, "w") as f:
            f.write("%d,%s,%d,%d,%s\n" % (
                time.ticks_ms(), step, rnd, adverts, extra))
    except OSError:
        pass


def light_irq(event, data):
    """Counts and returns. No allocation, no containers."""
    global adverts
    if event == 5:
        adverts += 1


def recon_irq(event, data):
    """The same shape of work recon does per advertisement."""
    global adverts
    if event != 5:
        return
    _at, addr, _t, rssi, adv = data
    adverts += 1
    a = bytes(addr)                     # allocation, every packet
    e = live.get(a)
    if e is not None:
        e[1] = rssi
        e[3] = time.ticks_ms()
        return
    if a not in seen_a and a not in seen_b:
        seen_a.add(a)                   # set growth, from an interrupt
    if len(live) + len(inbox) >= 1200:
        return
    inbox.append((a, 0, rssi, time.ticks_ms(), bytes(adv)))   # payload copy


def drain():
    """Stands in for the main loop admitting what the interrupt queued."""
    n = 0
    while inbox and n < 12:
        item = inbox.pop(0)
        live[item[0]] = [0, item[2], 0, item[3], None]
        n += 1


ble = bluetooth.BLE()
ble.active(True)
wdt = machine.WDT(timeout=8000)

print("each handler listens for %ds on the real radio" % SECONDS)
print()
print("%-12s %9s %9s %8s %9s %9s" % (
    "handler", "adverts", "adverts/s", "tracked", "free KB", "result"))

for name, handler in (("light", light_irq), ("recon-shaped", recon_irq)):
    rnd += 1
    adverts = 0
    live.clear()
    seen_a.clear()
    seen_b.clear()
    del inbox[:]
    gc.collect()

    mark(name + "_arm")
    ble.irq(handler)
    ble.gap_scan(0, 30000, 30000, True)
    mark(name + "_listening")

    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < SECONDS * 1000:
        wdt.feed()
        if handler is recon_irq:
            drain()
        time.sleep_ms(30)
    span = time.ticks_diff(time.ticks_ms(), t0) / 1000.0

    mark(name + "_stopping")
    ble.gap_scan(None)
    mark(name + "_done")
    gc.collect()
    print("%-12s %9d %9.1f %8d %9d %9s" % (
        name, adverts, adverts / span, len(live), gc.mem_free() // 1024, "ok"))

mark("done")
print()
print("Both handlers survived. The interrupt's workload alone is not enough to")
print("wedge it at this room's rate of roughly %d advertisements a second."
      % (adverts / max(1.0, SECONDS)))
