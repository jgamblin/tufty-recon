"""
Merge a week of daily exports into one report.

Each night's `export_log.py --erase` leaves a standalone day. This stitches
them back together, gives both week-unique and per-day views, and normalises
older days that were captured before the current counting rules existed.

    python3 tools/merge_week.py                  # every day in exports/
    python3 tools/merge_week.py --from 2026-08-03
    python3 tools/merge_week.py --out exports/week

Writes three things:

    week-wifi.csv     every access point, once, with the days it appeared
    week-ble.csv      every device, once, with the days it appeared
    week-summary.json aggregates only, no identifiers, safe to draft from

The CSVs carry MAC addresses and stay local. The summary is the one to write
from: MAC addresses are personal data, and a conference has international
attendees, so a published capture should stand on counts rather than on
identifiers.
"""

import argparse
import collections
import csv
import datetime
import json
import os
import sys

# A night out belongs to the day it started, not to 00:00 the next morning.
DAY_START_HOUR = 4

# Labels belonging to pairing schemes that re-randomise their address. The
# badge now refuses these at capture time (identify.ROTATING_SERVICES and the
# Microsoft company ID), but exports taken before that landed still carry them,
# so the same rule is applied here to keep every day counted the same way.
# Applying it to a current export changes nothing.
ROTATING_LABELS = (
    "Microsoft Swift Pair/CDP",
    "Google Fast Pair",
)

# Mirrors recon.LABEL_SHARE_MAX. A label only counts as identity while it still
# tells devices apart; past this many random addresses wearing it, it does not.
LABEL_SHARE_MAX = 8


def day_of(stamp):
    """The capture day a timestamp belongs to, or None if the clock was lost."""
    if not stamp or not stamp[0].isdigit():
        return None
    t = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    return (t - datetime.timedelta(hours=DAY_START_HOUR)).date().isoformat()


def load(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def find_days(root, since):
    """Group every exported record by the day it was actually captured.

    Deliberately keyed on first_seen rather than the export filename: an
    export taken at 06:51 holds the previous day's capture.
    """
    wifi, ble = collections.defaultdict(list), collections.defaultdict(list)
    files = 0
    for name in sorted(os.listdir(root)):
        if not name.startswith("recon-") or not name.endswith(".csv"):
            continue
        bucket = ble if name.endswith("-ble.csv") else wifi
        files += 1
        for row in load(os.path.join(root, name)):
            day = day_of(row.get("first_seen", ""))
            if day and (since is None or day >= since):
                bucket[day].append(row)
    return wifi, ble, files


def normalise_ble(rows):
    """Re-apply the current counting rules to one day of Bluetooth records.

    Per-day, because that is how the badge sees it: each night's erase starts
    a new session, and a label's share count starts over with it.
    """
    kept, dropped = [], collections.Counter()
    shares = {}
    for r in rows:
        random_addr = r.get("address_kind") != "public"
        label = r.get("label", "")
        if random_addr and label in ROTATING_LABELS:
            dropped["rotating scheme"] += 1
            continue
        if random_addr:
            # Public addresses are burned into the hardware, so identical
            # labels really are distinct machines and never count toward a
            # label's share.
            n = shares.get(label, 0) + 1
            shares[label] = n
            if n > LABEL_SHARE_MAX:
                dropped["shared label"] += 1
                continue
        kept.append(r)
    return kept, dropped


def merge(days, key):
    """Collapse per-day rows into one row per device, tracking which days it
    appeared on. The first sighting wins for every other field."""
    out = {}
    for day in sorted(days):
        for r in days[day]:
            k = r[key]
            got = out.get(k)
            if got is None:
                r = dict(r)
                r["days"] = [day]
                out[k] = r
            elif day not in got["days"]:
                got["days"].append(day)
    for r in out.values():
        r["days_seen"] = len(r["days"])
        r["first_day"] = r["days"][0]
        r["last_day"] = r["days"][-1]
        r["days"] = " ".join(r["days"])
    return out


def is_local_mac(mac):
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except ValueError:
        return False


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda r: (-r["days_seen"], r["first_day"])):
            w.writerow(r)


def top(rows, field, n=15, floor=1):
    c = collections.Counter(r[field] for r in rows if r.get(field))
    return [[k, v] for k, v in c.most_common(n) if v >= floor]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="exports", help="where the daily exports are")
    ap.add_argument("--from", dest="since", metavar="YYYY-MM-DD",
                    help="ignore days before this")
    ap.add_argument("--out", default="exports/week", help="where to write the report")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        sys.exit("no such directory: %s" % args.dir)
    wifi_days, ble_days, files = find_days(args.dir, args.since)
    if not wifi_days and not ble_days:
        sys.exit("no exported records found in %s" % args.dir)

    # Normalise every day to the current rules before anything is counted.
    dropped = collections.Counter()
    for day in list(ble_days):
        ble_days[day], d = normalise_ble(ble_days[day])
        dropped.update(d)

    wifi = merge(wifi_days, "bssid")
    ble = merge(ble_days, "address")
    days = sorted(set(wifi_days) | set(ble_days))

    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, "week-wifi.csv"), wifi.values(),
              ["bssid", "ssid", "channel", "security", "vendor", "virtual_bssid",
               "rssi_first_seen", "first_seen", "days_seen", "first_day",
               "last_day", "days"])
    write_csv(os.path.join(args.out, "week-ble.csv"), ble.values(),
              ["address", "address_kind", "label", "company", "mac_vendor",
               "rssi_first_seen", "first_seen", "days_seen", "first_day",
               "last_day", "days"])

    W, B = list(wifi.values()), list(ble.values())
    karma = [r for r in W if r["security"] == "WEP" and is_local_mac(r["bssid"])]

    summary = {
        "days": days,
        "sources": files,
        "totals": {
            "access_points": len(W),
            "devices": len(B),
            "returning_access_points": sum(1 for r in W if r["days_seen"] > 1),
            "returning_devices": sum(1 for r in B if r["days_seen"] > 1),
        },
        "normalised_out": dict(dropped),
        "per_day": {
            d: {"access_points": len(wifi_days.get(d, [])),
                "devices": len(ble_days.get(d, []))} for d in days
        },
        "security": dict(collections.Counter(r["security"] for r in W)),
        "channels": dict(collections.Counter(r["channel"] for r in W)),
        # Networks only, with a floor: a name carried by one access point is
        # usually somebody's phone hotspot, which is not infrastructure and
        # not something to publish.
        "networks": top(W, "ssid", 20, floor=3),
        "ap_vendors": top(W, "vendor", 20),
        "device_vendors": top(B, "mac_vendor", 20),
        "device_labels": top(B, "label", 20),
        "address_kinds": dict(collections.Counter(r["address_kind"] for r in B)),
        "rogue_aps": {
            "wep_locally_administered": len(karma),
            "distinct_ssids": len({r["ssid"] for r in karma if r["ssid"].strip("\x00")}),
            "channels": len({r["channel"] for r in karma}),
        },
    }
    with open(os.path.join(args.out, "week-summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)

    # ---- report
    print("Vegas week, %d days from %d export files" % (len(days), files))
    print()
    print("  %-12s %8s %8s" % ("day", "APs", "devices"))
    for d in days:
        print("  %-12s %8d %8d" % (d, len(wifi_days.get(d, [])), len(ble_days.get(d, []))))
    print("  %-12s %8d %8d   unique across the week" % ("total", len(W), len(B)))
    print()
    if dropped:
        print("normalised out of older exports (rules that did not exist yet):")
        for why, n in dropped.most_common():
            print("  %5d  %s" % (n, why))
        print()
    print("seen on more than one day: %d APs, %d devices"
          % (summary["totals"]["returning_access_points"],
             summary["totals"]["returning_devices"]))
    if karma:
        print("rogue APs (WEP on invented MACs): %d across %d channels, %d names"
              % (len(karma), summary["rogue_aps"]["channels"],
                 summary["rogue_aps"]["distinct_ssids"]))
    print()
    print("wrote %s/week-wifi.csv, week-ble.csv, week-summary.json" % args.out)
    print("the CSVs carry MAC addresses; draft the blog from week-summary.json")


if __name__ == "__main__":
    main()
