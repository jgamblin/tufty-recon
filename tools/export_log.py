#!/usr/bin/env python3
"""
Pull the recon log off the badge and write CSV.

The badge stores fixed-width binary records to save space on a 1MB
filesystem. This decodes them and resolves vendors against the same IEEE and
Bluetooth SIG tables the badge uses, so the CSV is readable without the badge.

    python3 tools/export_log.py                 # -> exports/recon-<date>.csv
    python3 tools/export_log.py --out /tmp/x    # writes x-wifi.csv, x-ble.csv
    python3 tools/export_log.py --keep          # leave the log on the badge

By default the log is left alone; --erase clears it after a successful export,
for starting a fresh day.
"""

import argparse
import csv
import os
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
MPR = os.environ.get("MPR", os.path.join(ROOT, ".venv", "bin", "mpremote"))
DATA = os.path.join(ROOT, "recon", "data")

# Records are length-prefixed rather than fixed-width: the two text fields
# vary enough that padding them to a fixed size was 45% of the log.
MAGIC = b"RCN\x02"
WIFI_HEAD = "<6sBbBI"
WIFI_HEAD_SIZE = struct.calcsize(WIFI_HEAD)
BLE_HEAD = "<6sBbHI"
BLE_HEAD_SIZE = struct.calcsize(BLE_HEAD)
NAME_LEN = 24
NO_COMPANY = 0xFFFF


def records(blob, head_fmt, head_size):
    """Walk a length-prefixed log. Stops cleanly at a truncated tail rather
    than misparsing everything after it."""
    if not blob.startswith(MAGIC):
        raise ValueError(
            "not a recon v2 log (missing header). An older fixed-width log "
            "cannot be read by this version.")
    i, n = len(MAGIC), len(blob)
    while i < n:
        ln = blob[i]
        i += 1
        if ln < head_size or i + ln > n:
            return          # truncated tail
        body = blob[i:i + ln]
        i += ln
        yield struct.unpack(head_fmt, body[:head_size]), body[head_size:]

SECURITY = {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA/2", 5: "WPA2",
            6: "WPA3", 7: "WPA2/3"}
ADDR_KIND = ("public", "static-random", "resolvable-private",
             "non-resolvable-private")


class Table:
    """Host-side mirror of the badge's lookup, so the CSV names things too."""

    def __init__(self, stem, key_size):
        self.key_size = key_size
        self.rec = key_size + 2
        try:
            self.keys = open(os.path.join(DATA, stem + ".bin"), "rb").read()
            self.names = open(os.path.join(DATA, stem + "_str.bin"), "rb").read()
        except OSError:
            self.keys = self.names = b""
        self.count = len(self.keys) // self.rec

    def lookup(self, key):
        lo, hi = 0, self.count - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            off = mid * self.rec
            k = self.keys[off:off + self.key_size]
            if k == key:
                i = struct.unpack(">H", self.keys[off + self.key_size:off + self.rec])[0]
                return self.names[i * NAME_LEN:(i + 1) * NAME_LEN].rstrip(b"\x00").decode("utf-8", "replace")
            if k < key:
                lo = mid + 1
            else:
                hi = mid - 1
        return ""


def vendor(oui_table, mac):
    if mac[0] & 0x02:
        head = mac[0] & 0xF8
        for low in range(8):
            got = oui_table.lookup(bytes([head | low, mac[1], mac[2]]))
            if got:
                return got
        return ""
    return oui_table.lookup(bytes(mac[:3]))


def port():
    """Locate the badge.

    `mpremote devs` reports the USB manufacturer, so the badge can be picked
    out from hubs, dongles and other boards rather than grabbing whatever
    serial device happens to sort first.
    """
    if os.environ.get("TUFTY_PORT"):
        return os.environ["TUFTY_PORT"]
    try:
        listing = subprocess.run([MPR, "devs"], capture_output=True, text=True).stdout
        for line in listing.splitlines():
            if "tufty" in line.lower():
                return line.split()[0]
    except OSError:
        pass
    for name in sorted(os.listdir("/dev")):
        if name.startswith(("cu.usbmodem", "ttyACM")):
            return "/dev/" + name
    sys.exit("No Tufty found. Plug it in, or set TUFTY_PORT.")


def pull(p, remote, local):
    r = subprocess.run([MPR, "connect", p, "fs", "cp", ":" + remote, local],
                       capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(local)


def stamp(epoch):
    if not epoch:
        return ""
    # The badge's RTC holds local wall time, so its "epoch" is already local
    # seconds. gmtime reads that back as the clock it was set to; localtime
    # would shift every timestamp by the host's timezone offset.
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="output path stem (default exports/recon-<date>)")
    ap.add_argument("--erase", action="store_true",
                    help="clear the log on the badge after a successful export")
    args = ap.parse_args()

    stem = args.out or os.path.join(
        ROOT, "exports", "recon-" + time.strftime("%Y%m%d-%H%M"))
    os.makedirs(os.path.dirname(os.path.abspath(stem)), exist_ok=True)

    p = port()
    oui = Table("oui", 3)
    btco = Table("btco", 2)
    if not oui.count:
        print("warning: vendor tables missing; run tools/build_id_db.py", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        wifi_raw = os.path.join(tmp, "w.bin")
        ble_raw = os.path.join(tmp, "b.bin")
        got_wifi = pull(p, "/state/recon_ap.bin", wifi_raw)
        got_ble = pull(p, "/state/recon_dev.bin", ble_raw)

        if not got_wifi and not got_ble:
            sys.exit("No log on the badge. Run the recon app first.")

        n_wifi = n_ble = 0

        if got_wifi:
            data = open(wifi_raw, "rb").read()
            path = stem + "-wifi.csv"
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["bssid", "ssid", "channel", "security", "vendor",
                            "virtual_bssid", "rssi_first_seen", "first_seen"])
                for (bssid, chan, rssi, sec, first), ssid in records(
                        data, WIFI_HEAD, WIFI_HEAD_SIZE):
                    w.writerow([
                        ":".join("%02X" % b for b in bssid),
                        ssid.decode("utf-8", "replace"),
                        chan, SECURITY.get(sec, str(sec)),
                        vendor(oui, bssid), "yes" if bssid[0] & 0x02 else "no",
                        rssi, stamp(first),
                    ])
                    n_wifi += 1
            print("wrote %s  (%d access points)" % (os.path.relpath(path, ROOT), n_wifi))

        if got_ble:
            data = open(ble_raw, "rb").read()
            path = stem + "-ble.csv"
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["address", "address_kind", "label", "company",
                            "mac_vendor", "rssi_first_seen", "first_seen"])
                for (addr, kind, rssi, company, first), label in records(
                        data, BLE_HEAD, BLE_HEAD_SIZE):
                    comp = ""
                    if company != NO_COMPANY:
                        comp = btco.lookup(struct.pack(">H", company))
                    w.writerow([
                        ":".join("%02X" % b for b in addr),
                        ADDR_KIND[kind] if kind < len(ADDR_KIND) else str(kind),
                        label.decode("utf-8", "replace"),
                        comp, vendor(oui, addr), rssi, stamp(first),
                    ])
                    n_ble += 1
            print("wrote %s  (%d bluetooth devices)" % (os.path.relpath(path, ROOT), n_ble))

    if args.erase:
        for f in ("/state/recon_ap.bin", "/state/recon_dev.bin",
                  "/state/recon_meta.bin"):
            subprocess.run([MPR, "connect", p, "fs", "rm", ":" + f],
                           capture_output=True)
        print("log erased on the badge")


if __name__ == "__main__":
    main()
