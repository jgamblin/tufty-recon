"""
Do flash writes and BLE interrupts collide?

On the RP2 family a flash write cannot run from flash: XIP is disabled for the
duration and the writing code executes from RAM. An interrupt that lands in
that window, and whose handler is not also in RAM, is a well known way to hang
the chip hard, taking USB with it.

recon writes flash on timers while the BLE radio is delivering advertisements
continuously: the capture log every 30 seconds, the health log every 2 minutes.
In a quiet room the collision window is microseconds against about 60
interrupts a second. On a DEF CON floor the interrupt rate is orders of
magnitude higher, and so is the chance of landing in it, which matches where
the freezes happened and why they will not reproduce on a desk.

This compresses that odds calculation by writing flash as fast as possible with
the radio live. Breadcrumbs record progress, and a watchdog means a hang ends
in a reboot rather than a dead badge.

    tools/run.sh tools/flash_vs_irq.py [rounds]
    tools/run.sh tools/read_trace.py

If it hangs, the trace will say how many writes it managed. If it survives
thousands, flash contention is not the mechanism.
"""

import bluetooth
import machine
import sys
import time

TRACE = "/state/hang_trace.txt"
SCRATCH = "/state/flash_churn.txt"
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 1500

adverts = 0
rnd = 0


def mark(step, extra=""):
    try:
        with open(TRACE, "w") as f:
            f.write("%d,%s,%d,%d,%s\n" % (
                time.ticks_ms(), step, rnd, adverts, extra))
    except OSError:
        pass


def irq(event, data):
    global adverts
    if event == 5:
        adverts += 1


ble = bluetooth.BLE()
ble.active(True)
ble.irq(irq)
ble.gap_scan(0, 30000, 30000, True)

wdt = machine.WDT(timeout=8000)
mark("flash_start")

print("writing flash %d times with the radio live" % ROUNDS)
print("advertisement rate here is what limits how hard this can push")

t0 = time.ticks_ms()
payload = "x" * 200
worst = 0
for rnd in range(1, ROUNDS + 1):
    wdt.feed()
    mark("writing")
    t = time.ticks_ms()
    try:
        with open(SCRATCH, "w") as f:
            f.write("%d,%d,%s\n" % (rnd, adverts, payload))
    except OSError as e:
        mark("write_FAILED", repr(e))
    d = time.ticks_diff(time.ticks_ms(), t)
    if d > worst:
        worst = d
    if rnd % 250 == 0:
        el = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
        print("  %5d writes  %6.1fs  %6d adverts  %5.1f adv/s  worst write %dms"
              % (rnd, el, adverts, adverts / el, worst))

mark("flash_done")
span = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
ble.gap_scan(None)

print()
print("survived %d flash writes in %.0fs with %d advertisements arriving"
      % (ROUNDS, span, adverts))
print("worst single write: %dms" % worst)
print()
print("that is %.0f writes/minute against %.0f interrupts/minute."
      % (ROUNDS / (span / 60.0), adverts / (span / 60.0)))
print("recon in normal use writes about 2/minute, so this is far harsher")
print("in write rate, and far gentler in interrupt rate than a conference.")
