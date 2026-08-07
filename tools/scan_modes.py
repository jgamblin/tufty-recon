"""
What does passive scanning cost, and what does active scanning buy?

recon ships with gap_scan(0, 30000, 30000, True): a 100% duty cycle, and
active. Active means the badge TRANSMITS a scan request to solicit a response,
which is how it obtains scan-response payloads, often where a device's name
lives. It also contradicts the README, which promises nothing that transmits.

The duty cycle matters separately: WiFi and BLE share one cyw43, and a listen
that never pauses is the most load that arrangement can carry. Interrupt rate
is the variable that tracks the freezes, and duty cycle is the direct control
over it.

Results are written to flash as each mode finishes, because the USB connection
on this badge drops often enough to lose a run otherwise. Read them back with
tools/read_modes.py even if the connection dies mid-test.

    tools/run.sh tools/scan_modes.py [seconds_per_mode]
    tools/run.sh tools/read_modes.py
"""

import bluetooth
import machine
import sys
import time

OUT = "/state/scan_modes.txt"
TRACE = "/state/hang_trace.txt"
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 15

_mode = "start"


def mark(step, extra=""):
    """Breadcrumb on flash. This script has stopped the badge three times out
    of three and console output dies with it, so the last thing written here
    is the only witness."""
    try:
        with open(TRACE, "w") as f:
            f.write("%d,%s,0,0,%s\n" % (time.ticks_ms(), step, extra))
    except OSError:
        pass

MODES = (
    ("active-100", 30000, 30000, True),
    ("passive-100", 30000, 30000, False),
    ("passive-50", 30000, 15000, False),
    ("passive-25", 30000, 7500, False),
)

seen = {}
adverts = 0
pending = []


def irq(event, data):
    """Deliberately minimal: queue the payload, decide nothing here."""
    global adverts
    if event != 5:
        return
    _at, addr, _t, _rssi, adv = data
    adverts += 1
    if len(pending) < 400:
        pending.append((bytes(addr), bytes(adv)))


def drain():
    """Names are parsed on the main loop, not in the interrupt."""
    while pending:
        a, b = pending.pop()
        named = False
        i = 0
        while i < len(b):
            ln = b[i]
            if ln == 0:
                break
            if i + 1 < len(b) and b[i + 1] in (0x08, 0x09):
                named = True
            i += ln + 1
        seen[a] = seen.get(a, False) or named


ble = bluetooth.BLE()
ble.active(True)
ble.irq(irq)

try:
    open(OUT, "w").close()
except OSError:
    pass

print("each mode listens for %ds in this room" % SECONDS)
print("%-14s %9s %8s %7s %7s" % ("mode", "adverts/s", "devices", "named", "named%"))

wdt = machine.WDT(timeout=8000)

for name, interval, window, active in MODES:
    _mode = name
    seen.clear()
    del pending[:]
    adverts = 0
    mark(name + ":stop")
    ble.gap_scan(None)
    mark(name + ":sleep")
    time.sleep_ms(300)
    mark(name + ":start")
    ble.gap_scan(0, interval, window, active)
    mark(name + ":listening")
    t0 = time.ticks_ms()
    n = 0
    while time.ticks_diff(time.ticks_ms(), t0) < SECONDS * 1000:
        wdt.feed()
        drain()
        time.sleep_ms(50)
        n += 1
        if n % 20 == 0:
            mark(name + ":listening",
                 "%ds seen=%d" % (time.ticks_diff(time.ticks_ms(), t0) // 1000,
                                  len(seen)))
    span = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
    mark(name + ":stopping")
    ble.gap_scan(None)
    drain()
    mark(name + ":writing")

    n = len(seen)
    named = sum(1 for v in seen.values() if v)
    line = "%s,%.1f,%d,%d" % (name, adverts / span, n, named)
    try:
        with open(OUT, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print("%-14s %9.1f %8d %7d %6d%%" % (
        name, adverts / span, n, named, (100 * named // n) if n else 0))

print()
print("results also on flash at %s" % OUT)
