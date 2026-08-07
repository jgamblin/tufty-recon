"""
Hunt the firmware hang by accelerating its most likely trigger.

The badge froze twice at DEF CON with a dead USB port, which means no code was
executing at all. The prime suspect is the one place both radios are handled in
sequence: WiFi and BLE share a single cyw43, so every WiFi scan stops the BLE
listen, scans, and re-arms it. That runs every 20 seconds, about 4,300 times a
day, and a race there would be rare per attempt and near-certain over a day.

This does the same sequence back to back, so a day of attempts takes minutes,
and drops a breadcrumb on flash before every step. If the badge hangs, the
breadcrumb says which call it died inside. A watchdog is armed here so the
badge recovers by itself rather than needing a hard reset.

    tools/run.sh tools/hunt_hang.py
    tools/run.sh tools/read_trace.py      # afterwards, to see where it stopped

Expect the connection to drop if it reproduces. That is the result.
"""

import bluetooth
import machine
import network
import sys
import time

TRACE = "/state/hang_trace.txt"
ROUNDS = 400            # a day of scans in a few minutes
WDT_MS = 8000

adverts = 0
last_adv = 0


def mark(step, extra=""):
    """Record where we are, on flash, before doing the risky thing."""
    try:
        with open(TRACE, "w") as f:
            f.write("%d,%s,%d,%d,%s\n" % (
                time.ticks_ms(), step, rnd, adverts, extra))
    except OSError:
        pass


def irq(event, data):
    global adverts, last_adv
    if event == 5:
        adverts += 1
        last_adv = time.ticks_ms()


print("hunting the hang: %d WiFi scans with BLE live" % ROUNDS)
print("breadcrumbs -> %s" % TRACE)

rnd = 0
mark("boot")

ble = bluetooth.BLE()
ble.active(True)
ble.irq(irq)
ble.gap_scan(0, 30000, 30000, True)

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

wdt = machine.WDT(timeout=WDT_MS)

worst_scan = 0
worst_stop = 0
worst_start = 0
slow = 0

print("%5s %7s %7s %7s %6s %8s" % (
    "round", "stop_ms", "scan_ms", "arm_ms", "aps", "adverts"))

t_all = time.ticks_ms()
for rnd in range(1, ROUNDS + 1):
    wdt.feed()

    # 1. Stop the BLE listen. An advertisement can be in flight right now.
    mark("gap_scan_stop")
    t = time.ticks_ms()
    try:
        ble.gap_scan(None)
    except OSError:
        pass
    d_stop = time.ticks_diff(time.ticks_ms(), t)

    # 2. Scan WiFi. This is the long one and it owns the radio.
    wdt.feed()
    mark("wlan_scan")
    t = time.ticks_ms()
    try:
        aps = len(wlan.scan())
    except OSError:
        aps = -1
    d_scan = time.ticks_diff(time.ticks_ms(), t)

    # 3. Re-arm the BLE listen.
    wdt.feed()
    mark("gap_scan_start")
    t = time.ticks_ms()
    try:
        ble.gap_scan(0, 30000, 30000, True)
    except OSError as e:
        mark("gap_scan_start_FAILED", str(e))
    d_arm = time.ticks_diff(time.ticks_ms(), t)

    mark("idle")
    if d_stop > worst_stop:
        worst_stop = d_stop
    if d_scan > worst_scan:
        worst_scan = d_scan
    if d_arm > worst_start:
        worst_start = d_arm
    # Anything approaching the watchdog window is a finding on its own: it
    # would reboot the badge in the field even without a true hang.
    if max(d_stop, d_scan, d_arm) > WDT_MS // 2:
        slow += 1
        print("%5d %7d %7d %7d %6d %8d   <-- slow" % (
            rnd, d_stop, d_scan, d_arm, aps, adverts))
    elif rnd % 25 == 0:
        print("%5d %7d %7d %7d %6d %8d" % (
            rnd, d_stop, d_scan, d_arm, aps, adverts))

mark("done")
span = time.ticks_diff(time.ticks_ms(), t_all) / 1000.0
print()
print("survived %d rounds in %.0fs" % (ROUNDS, span))
print("worst: stop %dms, scan %dms, re-arm %dms" % (
    worst_stop, worst_scan, worst_start))
print("rounds with a step over %dms: %d" % (WDT_MS // 2, slow))
print("advertisements heard: %d" % adverts)
if worst_scan > WDT_MS:
    print()
    print("NOTE: a WiFi scan alone exceeded the watchdog window. In the app")
    print("that would reboot the badge, and would look exactly like a hang.")
