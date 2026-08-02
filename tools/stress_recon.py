"""
Stress recon at conference scale. Runs on the badge.

A suburban house shows ~45 devices. A DEF CON hall is two orders of magnitude
worse, and the failure modes that matter (a per-frame O(n) scan, a sort that
blows the frame budget, RAM exhaustion) only appear at that scale.

Injects synthetic devices directly into the app's state, then measures each
view's frame time and the memory in use.

    .venv/bin/mpremote connect $PORT run tools/stress_recon.py
"""

import badgeware  # noqa: F401
import builtins
import gc
import os
import sys
import time

# Top level is the shipped cap, so the test proves the shipped config.
LEVELS = ((50, 10), (400, 120), (1200, 300), (1800, 500), (3000, 500))

_cap = []


class _R:
    result = None


builtins.run = lambda u: (_cap.append(u), _R())[1]

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)
os.chdir(PATH)
m = __import__(PATH)
update = _cap[0]

# Keep the real radios from fighting the measurement.
try:
    m.ble_radio.gap_scan(None)
except Exception:
    pass
m.WIFI_EVERY_MS = 3600_000


def synth(n_ble, n_wifi):
    m.ble.clear()
    m.wifi.clear()
    m.rotating.clear()
    del m.pending[:]

    for i in range(n_ble):
        addr = bytes((0xC0 | (i & 0x3F), (i >> 6) & 0xFF, (i >> 14) & 0xFF,
                      i & 0xFF, (i >> 8) & 0xFF, (i >> 16) & 0xFF))
        # Spread across buckets the way a real room does.
        cat = i % m.ID.N_CATS
        # Entry layout must match the app, including the vendor slot.
        m.ble[addr] = [0, -30 - (i % 70), 0, time.ticks_ms(),
                       "Device %d" % i, "detail", (), 0x004C, None, cat,
                       "Vendor %d" % (i % 40)]
    for i in range(n_wifi):
        bssid = bytes((0x28, 0x94, (i >> 8) & 0xFF, i & 0xFF,
                       (i >> 4) & 0xFF, (i >> 12) & 0xFF))
        m.wifi[bssid] = ["net-%d" % i, 1 + (i % 13), -30 - (i % 70), 3, 0,
                         time.ticks_ms(), "net-%d" % i, "NETGEAR", (),
                         m.ID.CAT_AP, "Vendor %d" % (i % 40)]


def refresh():
    """Mark every synthetic device as heard just now.

    Without this they all expire together mid-run, the pruner deletes them
    while the measurement is in flight, and each view ends up timed against a
    different population. The worst case worth measuring is "all of these are
    live right now".
    """
    now = time.ticks_ms()
    for e in m.ble.values():
        e[3] = now
    for e in m.wifi.values():
        e[5] = now


def bench(view, frames=25):
    m.view = view
    refresh()
    # ticks_diff needs a real ticks value; a raw negative is undefined and
    # silently skips the work being measured.
    m.last_sort = time.ticks_add(time.ticks_ms(), -m.RESORT_MS)
    m._last_pass = time.ticks_add(time.ticks_ms(), -m.STATS_EVERY_MS)
    # The aggregate pass is spread over frames; let one complete first.
    for _ in range(3 + (len(m.ble) + len(m.wifi)) // m.STATS_CHUNK):
        badge.poll()
        update()
    refresh()
    t = time.ticks_ms()
    for _ in range(frames):
        badge.poll()
        update()
    return time.ticks_diff(time.ticks_ms(), t) / frames


# Names come from the app so adding a view cannot silently shift the columns,
# which is exactly what happened when BILLBOARD was added in front of DASH.
VIEWS = [("BILL", m.BILLBOARD), ("DASH", m.DASH), ("LIVE", m.LIVE),
         ("FLAGS", m.FLAGS), ("VNDRS", m.VENDORS), ("LOG", m.LOGVIEW)]

print("recon stress test  (live-set cap is %d BLE)" % m.MAX_LIVE_BLE)
print("BILL sleeps %dms a frame on purpose; it is not meant to be fast."
      % m.BILLBOARD_IDLE_MS)
print("%-14s %s  %s" % ("devices",
      " ".join("%7s" % n for n, _ in VIEWS), "RAM"))

for n_ble, n_wifi in LEVELS:
    synth(n_ble, n_wifi)
    gc.collect()
    before = gc.mem_free()
    times = [bench(v) for _n, v in VIEWS]
    gc.collect()
    used = (before - gc.mem_free()) // 1024
    # Judge on the interactive views; the billboard's sleep is by design.
    worst = max(t for (n, _), t in zip(VIEWS, times) if n != "BILL")
    if n_ble > m.MAX_LIVE_BLE:
        # Injected directly, bypassing the cap the interrupt enforces. Shown to
        # prove where the cliff is, not as a state the app can reach.
        flag = "  (above cap, unreachable)"
    else:
        flag = "" if worst < 34 else ("  SLOW" if worst < 100 else "  UNUSABLE")
    print("%-14s %s  %4dKB free%s" % (
        "%d ble/%d ap" % (n_ble, n_wifi),
        " ".join("%7.1f" % t for t in times), gc.mem_free() // 1024, flag))

print()
print("frame budget is 16.7ms of drawing; badge.update() adds ~10ms.")
print("anything over ~34ms/frame is below 30fps.")
