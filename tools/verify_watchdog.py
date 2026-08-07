"""
Prove the watchdog ends a hang instead of the day ending it.

The badge was found at DEF CON with a frozen screen and a dead USB port. That
is not an app that stopped collecting: a stalled app still services USB,
because that runs independently of this code. Nothing was executing, so no
Python-level defence could have noticed it, and the capture log puts the
outage at 3 hours 40 minutes.

This deliberately wedges the main loop the same way and checks the badge comes
back by itself.

    tools/run.sh tools/verify_watchdog.py

Expect this to LOSE the connection: that is the pass condition. mpremote will
report the port disappearing, then the badge reboots into recon on its own.
Confirm afterwards with:

    python3 tools/health_report.py      # look for a watchdog-recovered boot
"""

import builtins
import os
import sys
import time

_cap = []


class _R:
    result = None


builtins.run = lambda u: (_cap.append(u), _R())[1]

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)
os.chdir(PATH)
import badgeware  # noqa: F401,E402
m = __import__(PATH)
update = _cap[0]

print("watchdog set to %dms" % m.WATCHDOG_MS)
if not m.WATCHDOG_MS:
    print("WATCHDOG_MS is 0, so it is disabled and nothing here can pass.")
    raise SystemExit

print()
print("running normally for 12s, which is longer than the timeout.")
print("if the badge reboots during this, the watchdog is too aggressive")
print("and would reboot-loop in the field.")
t0 = time.ticks_ms()
frames = 0
worst = 0
while time.ticks_diff(time.ticks_ms(), t0) < 12000:
    f = time.ticks_ms()
    badge.poll()
    update()
    frames += 1
    d = time.ticks_diff(time.ticks_ms(), f)
    if d > worst:
        worst = d
print("  survived %d frames, worst frame %dms" % (frames, worst))
print("  headroom: %dx" % (m.WATCHDOG_MS // max(1, worst)))

print()
print("now hanging the main loop on purpose. the badge should reboot in ~%ds."
      % (m.WATCHDOG_MS // 1000))
print("this connection is about to die, and that is the pass.")
time.sleep_ms(50)       # let the print reach the host before the loop stops

# The real hang was inside a driver call with no Python executing. A bare busy
# loop is the same thing from the watchdog's point of view: nothing feeds it.
while True:
    pass
