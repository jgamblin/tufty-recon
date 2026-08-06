"""
Does the badge notice when its Bluetooth listen has died, and bring it back?

The WiFi scan has to pause the BLE listen, because the two share one radio, and
then re-arm it. That re-arm could fail, and the failure was swallowed: the app
carried on, the screen carried on, and the badge never heard another
advertisement until somebody restarted it. Nothing retried, and nothing said so.

The radio is faked here so the failure can be forced. What is under test is
whether the app reacts to it.

    tools/run.sh tools/verify_scan_revival.py
"""

import os
import sys
import time

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)
os.chdir(PATH)

import builtins  # noqa: E402

_cap = []


class _R:
    result = None


builtins.run = lambda u: (_cap.append(u), _R())[1]
import badgeware  # noqa: F401,E402
m = __import__(PATH)


class FakeRadio:
    """Stands in for the BLE radio so a re-arm can be made to fail on demand."""

    def __init__(self):
        self.starts = 0
        self.fail = False

    def gap_scan(self, *a):
        if a and a[0] is None:
            return                      # a pause always works
        if self.fail:
            raise OSError(5)
        self.starts += 1


fake = FakeRadio()
m.ble_radio = fake
m.wifi_busy = False

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %-4s %-46s got %s" % ("ok" if ok else "FAIL", name, got))


now = time.ticks_ms()

# A healthy radio should be left alone.
m.ble_scanning = True
m.last_adv = now
m.last_scan_try = time.ticks_add(now, -m.SCAN_RETRY_MS - 1)
before = fake.starts
m._ensure_ble_scan(now)
check("healthy listen is not restarted", fake.starts, before)

# A re-arm that raised leaves the flag down; that must be retried.
m.ble_scanning = False
m.last_scan_try = time.ticks_add(now, -m.SCAN_RETRY_MS - 1)
before = fake.starts
m._ensure_ble_scan(now)
check("a listen known to be down is restarted", fake.starts, before + 1)
check("and is marked healthy again", m.ble_scanning, True)

# Silence is the check that does not trust the API's return value.
m.ble_scanning = True
m.last_adv = time.ticks_add(now, -m.BLE_SILENCE_MS - 1)
m.last_scan_try = time.ticks_add(now, -m.SCAN_RETRY_MS - 1)
before = fake.starts
m._ensure_ble_scan(now)
check("silence restarts a listen that claims health", fake.starts, before + 1)

# Restarting must reset the silence clock, or it fires every frame from then on.
before = fake.starts
m._ensure_ble_scan(now)
check("does not restart again immediately", fake.starts, before)

# Retries are rate limited, so a sulking stack is not hammered.
m.ble_scanning = False
m.last_scan_try = now
before = fake.starts
m._ensure_ble_scan(now)
check("retry is rate limited", fake.starts, before)

# A failing radio must leave the flag down so the next window tries again.
fake.fail = True
m.ble_scanning = True
m.last_adv = time.ticks_add(now, -m.BLE_SILENCE_MS - 1)
m.last_scan_try = time.ticks_add(now, -m.SCAN_RETRY_MS - 1)
m._ensure_ble_scan(now)
check("a failed restart is recorded, not swallowed", m.ble_scanning, False)
fake.fail = False

# The WiFi scan owns the radio while it runs; touching it then would break it.
m.wifi_busy = True
m.ble_scanning = False
m.last_scan_try = time.ticks_add(now, -m.SCAN_RETRY_MS - 1)
before = fake.starts
m._ensure_ble_scan(now)
check("leaves the radio alone during a wifi scan", fake.starts, before)
m.wifi_busy = False

print()
print("restarts counted for the flags page: %d" % m.scan_restarts)
print("FAILURES: %d" % bad if bad else "the listen is now watched and revived")
