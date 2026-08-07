"""
Explain what the badge was doing when nobody was watching.

Reads the health log, which samples every two minutes, and answers the two
questions an exported capture cannot: when did it stop collecting, and what
was true at the time. A silent capture is ambiguous on its own, because the log
only records devices the badge had never seen before, so an empty hour might be
a stall or might be a quiet room.

    python3 tools/health_report.py              # pull from the badge
    python3 tools/health_report.py --file f.csv # read one already pulled
"""

import argparse
import datetime
import os
import subprocess
import sys
import tempfile

MPR = ".venv/bin/mpremote"
MIN_EPOCH = 1_735_689_600           # 2025-01-01; below this the RTC was unset
GAP_MIN = 4.0                       # sampling is every 2 minutes

COLS = ("epoch", "ticks", "mv", "pct", "usb", "light",
        "flood", "rate", "tracked", "frame_ms", "restarts", "scanning",
        "boot", "died_in")

# Row widths this log has had. Six was battery only; twelve added app state;
# thirteen added the reset cause. All three are read, because a reader that
# skips what it does not recognise is how a day of telemetry went missing.
WIDTHS = {6: 6, 12: 12, 13: 13, 14: 14}
WDT_RESET = 3
# What the previous run was doing when it stopped writing, from recon's
# PHASE_CODES. This is the whole point of the 14th column: it separates a hang
# inside a blocking radio call from one that happened while merely running.
PHASE = {0: "unknown (or a clean first boot)",
         1: "running normally, not in a long call",
         2: "inside a WiFi scan and the radio handover",
         3: "flushing the log to flash",
         4: "drawing"}


def port():
    out = subprocess.run([MPR, "devs"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "tufty" in line.lower():
            return line.split()[0]
    sys.exit("No Tufty found. Plug it in.")


def fetch():
    p = port()
    raw = ""
    with tempfile.TemporaryDirectory() as tmp:
        for remote in (":/state/battery.csv.1", ":/state/battery.csv"):
            local = os.path.join(tmp, remote.rsplit("/", 1)[-1])
            r = subprocess.run([MPR, "connect", p, "fs", "cp", remote, local],
                               capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(local):
                raw += open(local).read()
    if not raw:
        sys.exit("No health log on the badge yet.")
    return raw


def parse(raw):
    rows, old = [], 0
    for line in raw.splitlines():
        parts = line.strip().split(",")
        state = True
        if len(parts) not in WIDTHS:
            continue
        if len(parts) == 6:
            old += 1
            state = False
        parts = parts + ["0"] * (len(COLS) - len(parts))
        try:
            r = dict(zip(COLS, (int(p) for p in parts)))
        except ValueError:
            continue
        # Padding is not a measurement. Without this the report cheerfully
        # states that an old sample had flood=0 and tracked=0, which is not
        # something that log ever knew.
        r["state"] = state
        rows.append(r)
    return rows, old


def when(r):
    if r["epoch"] < MIN_EPOCH:
        return "  (no clock)  "
    return datetime.datetime.fromtimestamp(r["epoch"], datetime.timezone.utc).strftime("%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="read a pulled log instead of the badge")
    ap.add_argument("--gap", type=float, default=GAP_MIN,
                    help="report gaps longer than this many minutes")
    args = ap.parse_args()

    raw = open(args.file).read() if args.file else fetch()
    rows, old = parse(raw)
    if not rows:
        sys.exit("No readable samples.")
    print("%d samples%s" % (
        len(rows), ", %d from before app state was recorded" % old if old else ""))
    noclock = sum(1 for r in rows if r["epoch"] < MIN_EPOCH)
    if noclock:
        print("%d samples have no clock: the RTC was unset for that stretch, so "
              "those\nrecords carry no time of day." % noclock)
    print()

    print("=== stalls (no sample for over %.0f minutes) ===" % args.gap)
    found = 0
    for a, b in zip(rows, rows[1:]):
        if a["epoch"] < MIN_EPOCH or b["epoch"] < MIN_EPOCH:
            continue
        mins = (b["epoch"] - a["epoch"]) / 60.0
        if mins <= args.gap:
            continue
        found += 1
        # ticks_ms is a free-running hardware counter, so it advances whether
        # or not any code is executing. Going backwards is the one thing only a
        # reboot can do.
        kind = "REBOOTED" if b["ticks"] < a["ticks"] else "no reboot: it was up and not sampling"
        print("  %s  ->  %s   %5.1f min   %s" % (when(a), when(b), mins, kind))
        if a["state"]:
            print("      before: flood=%d rate=%d tracked=%d frame=%dms "
                  "scanning=%d usb=%d %dmV"
                  % (a["flood"], a["rate"], a["tracked"], a["frame_ms"],
                     a["scanning"], a["usb"], a["mv"]))
        else:
            print("      before: usb=%d %dmV (this sample predates app state)"
                  % (a["usb"], a["mv"]))
    if not found:
        print("  none")
    print()

    live = [r for r in rows if r["frame_ms"]]
    if live:
        slow = [r for r in live if r["frame_ms"] > 500]
        worst = max(live, key=lambda r: r["frame_ms"])
        print("=== frame time ===")
        print("  worst %dms at %s (tracked %d, flood %d)" % (
            worst["frame_ms"], when(worst), worst["tracked"], worst["flood"]))
        print("  over 500ms in %d of %d samples" % (len(slow), len(live)))
        print("  a badge at one frame a second looks frozen but is not")
        print()
        fl = [r for r in live if r["flood"]]
        print("=== flood ===")
        print("  latched in %d of %d samples" % (len(fl), len(live)))
        if fl:
            print("  peak rate %d/sec" % max(r["rate"] for r in fl))
        print()
        print("=== radio ===")
        print("  scan restarts: %d" % max(r["restarts"] for r in live))
        deaf = [r for r in live if not r["scanning"]]
        print("  samples with the listen down: %d" % len(deaf))
        if deaf:
            print("  first at %s" % when(deaf[0]))
        print()

    # Boots are counted by ticks_ms going backwards, which only a restart can
    # do. reset_cause is reported too, but it cannot carry this on its own: on
    # the RP2350 machine.reset() is implemented with the watchdog, so a
    # deliberate reset is indistinguishable from a watchdog-recovered hang by
    # that value. An unattended run with restarts nobody asked for is the
    # signal; the cause code only narrows it.
    boots = [b for a, b in zip(rows, rows[1:]) if b["ticks"] < a["ticks"]]
    if rows:
        print("=== restarts ===")
        print("  %d restarts across %d samples" % (len(boots), len(rows)))
        wdt = [r for r in rows if r.get("boot") == WDT_RESET]
        if wdt:
            print("  %d samples report WDT_RESET, which covers both a watchdog"
                  % len(wdt))
            print("  recovery and a deliberate machine.reset() on this board")
        if boots:
            print("  most recent at %s" % when(boots[-1]))
            print("  a restart nobody asked for means the watchdog ended a hang")
        # Each restart's first sample carries what the run before it died in.
        after = [b for a, b in zip(rows, rows[1:]) if b["ticks"] < a["ticks"]]
        died = [r.get("died_in", 0) for r in after]
        if died:
            print()
            print("  what the run before each restart was doing when it stopped:")
            for code in sorted(set(died)):
                n = died.count(code)
                print("    %3d x  %s" % (n, PHASE.get(code, "code %d" % code)))
            wifi = died.count(2)
            if wifi and wifi >= len(died) * 0.6:
                print()
                print("  Most restarts happened inside the WiFi scan. That points at a")
                print("  blocking call running past the watchdog window rather than a")
                print("  true firmware hang, and the fix is to bound the scan, not to")
                print("  reboot faster.")
            elif died.count(1) >= len(died) * 0.6:
                print()
                print("  Most restarts happened while merely running, with no long call")
                print("  in progress. That is a genuine hang, and the watchdog is doing")
                print("  the only thing that can be done about it from up here.")


if __name__ == "__main__":
    main()
