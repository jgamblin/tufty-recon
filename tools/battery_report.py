#!/usr/bin/env python3
"""
Pull recon's battery telemetry off the badge and turn it into a runtime figure.

recon appends a sample every couple of minutes while it runs. Left on battery
overnight, that gives a real discharge curve for the actual workload rather
than an estimate from a datasheet.

    python3 tools/battery_report.py
    python3 tools/battery_report.py --csv out.csv    # keep the raw samples

Columns on the badge: epoch, ticks_ms, millivolts, percent, usb, light.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
MPR = os.environ.get("MPR", os.path.join(ROOT, ".venv", "bin", "mpremote"))

# A single LiPo cell. EMPTY_MV is measured, not assumed: left to run itself
# down, this badge kept going to 2793mV before cutting out. Using a textbook
# 3300 under-reported a 9h 46m runtime as 6h 27m.
FULL_MV = 4200
EMPTY_MV = 2800

# An unset RTC reports 1970 or 2021 depending on how it was cleared, and the
# clock does not survive the battery dying, so samples with a nonsense date
# would otherwise wreck the wall-time arithmetic.
MIN_EPOCH = 1_735_689_600      # 2025-01-01


def port():
    if os.environ.get("TUFTY_PORT"):
        return os.environ["TUFTY_PORT"]
    try:
        out = subprocess.run([MPR, "devs"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "tufty" in line.lower():
                return line.split()[0]
    except OSError:
        pass
    sys.exit("No Tufty found. Plug it in, or set TUFTY_PORT.")


# The health log grew from 6 columns to 12 when app state was added to it. The
# first six are unchanged and are all this report needs, so both are read
# rather than one silently skipping the other's rows — which is exactly how a
# whole day of telemetry went missing once already.
WIDTHS = (6, 12)


def parse(raw):
    rows = []
    skipped = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) not in WIDTHS:
            skipped += 1
            continue
        try:
            row = tuple(int(p) for p in parts[:6])
        except ValueError:
            skipped += 1
            continue
        if row[0] and row[0] < MIN_EPOCH:
            row = (0,) + row[1:]        # clock was not set; keep the sample
        rows.append(row)
    if skipped:
        print("note: skipped %d unreadable lines" % skipped, file=sys.stderr)
    return rows


def segments(rows):
    """Split at reboots (ticks going backwards) and at USB transitions, so a
    charge period is never averaged together with a discharge one."""
    out, cur = [], []
    for r in rows:
        if cur:
            prev = cur[-1]
            if r[1] < prev[1] or r[4] != prev[4]:
                out.append(cur)
                cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


def hhmm(seconds):
    seconds = int(seconds)
    return "%dh %02dm" % (seconds // 3600, (seconds % 3600) // 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="also write the raw samples here")
    args = ap.parse_args()

    p = port()
    with tempfile.TemporaryDirectory() as tmp:
        # The badge rotates the log at its size cap, so the older generation
        # holds anything from before the rotation. Oldest first, so the samples
        # stay in order.
        raw = ""
        for remote in (":/state/battery.csv.1", ":/state/battery.csv"):
            local = os.path.join(tmp, remote.rsplit("/", 1)[-1])
            r = subprocess.run([MPR, "connect", p, "fs", "cp", remote, local],
                               capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(local):
                raw += open(local).read()
        if not raw:
            sys.exit("No battery log on the badge. Run recon for a while first.")

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("epoch,ticks_ms,millivolts,percent,usb,light,"
                    "flood,flood_rate,tracked,frame_ms,scan_restarts,scanning\n")
            f.write(raw)
        print("raw samples -> %s" % args.csv)

    rows = parse(raw)
    if len(rows) < 2:
        sys.exit("Only %d sample(s); let it run longer." % len(rows))

    print("%d samples\n" % len(rows))
    print("%-22s %-9s %-13s %-11s %s" % (
        "period", "power", "duration", "battery", "rate"))
    print("-" * 74)

    discharge = []
    for seg in segments(rows):
        if len(seg) < 2:
            continue
        first, last = seg[0], seg[-1]
        secs = (last[1] - first[1]) / 1000.0
        if secs <= 0:
            continue
        dmv = last[2] - first[2]
        # The badge's RTC holds local wall time, so its "epoch" is local
        # seconds. gmtime reads that back as the clock it was set to;
        # localtime would shift it by the host's timezone offset.
        start = time.strftime("%H:%M", time.gmtime(first[0])) if first[0] else "?"
        end = time.strftime("%H:%M", time.gmtime(last[0])) if last[0] else "?"
        rate = dmv / (secs / 3600.0)
        print("%-22s %-9s %-13s %3d%% -> %3d%%  %+.0f mV/h" % (
            "%s - %s" % (start, end), "USB" if first[4] else "battery",
            hhmm(secs), first[3], last[3], rate))
        if not first[4] and rate < 0:
            discharge.append((secs, dmv, rate, last))

    if not discharge:
        print("\nNo discharge period yet: unplug it and let recon run.")
        return

    total_s = sum(d[0] for d in discharge)
    total_mv = sum(d[1] for d in discharge)
    rate = total_mv / (total_s / 3600.0)
    last = discharge[-1][3]

    print("\n%s on battery, %d mV lost, %.0f mV/hour" % (
        hhmm(total_s), -total_mv, -rate))

    if rate < 0:
        remaining = max(0.0, (last[2] - EMPTY_MV) / -rate)
        full_life = (FULL_MV - EMPTY_MV) / -rate
        if last[2] <= EMPTY_MV + 50:
            print("it ran to %d mV and cut out: that is the whole battery" % last[2])
        else:
            print("at %d mV (%d%%), about %s left" % (
                last[2], last[3], hhmm(remaining * 3600)))
        print("from a full charge, roughly %s of continuous scanning" % hhmm(full_life * 3600))
        print("\nA LiPo curve is flat in the middle and steep at both ends, so a")
        print("short sample over-estimates. Trust this most after several hours.")


if __name__ == "__main__":
    main()
