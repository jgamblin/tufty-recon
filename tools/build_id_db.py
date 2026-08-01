#!/usr/bin/env python3
"""
Compile the vendor databases recon uses to name what it sees.

Pulls the IEEE MA-L registry (~40k MAC prefixes) and the Bluetooth SIG company
identifier list (~4k), then writes fixed-width binary tables that a
microcontroller can binary-search straight off flash without parsing anything
or loading it into RAM.

    python3 tools/build_id_db.py            # cached downloads
    python3 tools/build_id_db.py --refresh  # re-download

Output, into recon/data/:

    oui.bin     5 bytes/record, sorted:  3-byte OUI + uint16 name index
    oui_str.bin NAME_LEN bytes/record, indexed directly
    btco.bin    4 bytes/record, sorted:  uint16 company ID + uint16 name index
    btco_str.bin

All integers are big-endian so that comparing raw key bytes gives the same
order as comparing the numbers. Little-endian keys sort differently as bytes
than as integers, which silently breaks the binary search.

Sorted plus fixed-width means lookup is a binary search over seek/read, which
costs about 16 reads for the OUI table. No index, no hash, no RAM.
"""

import argparse
import json
import os
import re
import struct
import sys
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
OUT = os.path.join(ROOT, "recon", "data")
CACHE = os.path.join(ROOT, ".cache")

OUI_URL = "https://standards-oui.ieee.org/oui/oui.csv"
BTCO_URL = ("https://raw.githubusercontent.com/NordicSemiconductor/"
            "bluetooth-numbers-database/master/v1/company_ids.json")

# The badge is 160px wide; this is about as much as fits in the small font.
NAME_LEN = 24

# Corporate noise that costs characters and adds nothing on a 160px screen.
SUFFIXES = re.compile(
    r"[,\s]*\b("
    r"inc|inc\.|incorporated|llc|l\.l\.c\.|ltd|ltd\.|limited|co|co\.|corp|corp\."
    r"|corporation|company|gmbh|ag|a/s|ab|as|oy|bv|b\.v\.|nv|n\.v\.|sa|s\.a\."
    r"|sas|s\.a\.s\.|srl|s\.r\.l\.|spa|s\.p\.a\.|plc|pty|pte|lp|llp|kg|kft"
    r"|technologies|technology|tech|electronics|electronic|international"
    r")\b\.?\s*$", re.I)


def fetch(url, name, refresh=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and not refresh:
        print("  cached %s (%.1f MB)" % (name, os.path.getsize(path) / 1e6))
        return open(path, "rb").read()
    print("  downloading %s ..." % name)
    req = urllib.request.Request(url, headers={"User-Agent": "tufty-badge-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)
    print("  got %s (%.1f MB)" % (name, len(data) / 1e6))
    return data


def tidy(name):
    """Trim a legal entity name down to something readable at 160px."""
    s = " ".join(name.split())
    s = s.strip(' "')
    # Strip suffixes repeatedly: "Shenzhen Foo Technology Co., Ltd." has three.
    for _ in range(4):
        new = SUFFIXES.sub("", s).strip(" ,.")
        if new == s or not new:
            break
        s = new
    s = s.strip(" ,.")
    return _fit(s) or "?"


def _fit(s, limit=NAME_LEN):
    """Trim so the UTF-8 encoding fits in `limit` bytes without splitting a
    character. Counting characters instead of bytes leaves a mangled trailing
    byte on any name with an accent or a CJK glyph."""
    if len(s.encode("utf-8")) <= limit:
        return s
    ellipsis = "…"
    room = limit - len(ellipsis.encode("utf-8"))
    cut = s.encode("utf-8")[:room].decode("utf-8", "ignore")
    return cut.rstrip(" ,.-") + ellipsis


def write_table(records, key_fmt, key_size, names, stem):
    """records: list of (key_bytes_or_int, name). Writes <stem>.bin/<stem>_str.bin."""
    # Deduplicate names so 40k OUIs do not mean 40k stored strings.
    index = {}
    order = []
    for _k, n in records:
        if n not in index:
            index[n] = len(order)
            order.append(n)

    with open(os.path.join(OUT, stem + "_str.bin"), "wb") as f:
        for n in order:
            b = _fit(n).encode("utf-8")
            f.write(b + b"\x00" * (NAME_LEN - len(b)))

    records.sort(key=lambda kv: kv[0])
    with open(os.path.join(OUT, stem + ".bin"), "wb") as f:
        for k, n in records:
            if key_fmt:
                f.write(struct.pack(key_fmt, k))
            else:
                f.write(k)
            f.write(struct.pack(">H", index[n]))

    rec = key_size + 2
    print("  %-12s %6d records x %d B = %6.1f KB   names %5d x %d B = %5.1f KB"
          % (stem, len(records), rec, len(records) * rec / 1024,
             len(order), NAME_LEN, len(order) * NAME_LEN / 1024))
    return len(records), len(order)


def build_oui(refresh):
    raw = fetch(OUI_URL, "oui.csv", refresh).decode("utf-8", "replace")
    import csv
    import io

    records = []
    seen = set()
    reader = csv.reader(io.StringIO(raw))
    next(reader, None)  # header
    for row in reader:
        if len(row) < 3:
            continue
        assign, org = row[1].strip(), row[2].strip()
        if len(assign) != 6:
            continue
        try:
            key = bytes.fromhex(assign)
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        records.append((key, tidy(org)))
    return write_table(records, None, 3, None, "oui")


def build_btco(refresh):
    raw = fetch(BTCO_URL, "company_ids.json", refresh).decode("utf-8", "replace")
    data = json.loads(raw)
    records = []
    seen = set()
    for item in data:
        code = item.get("code")
        name = item.get("name")
        if code is None or name is None or code in seen or code > 0xFFFF:
            continue
        seen.add(code)
        records.append((code, tidy(name)))
    return write_table(records, ">H", 2, None, "btco")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download sources")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    print("IEEE MA-L (WiFi/BLE MAC vendors)")
    build_oui(args.refresh)
    print("Bluetooth SIG company identifiers")
    build_btco(args.refresh)

    total = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
    print("\ntotal on badge: %.1f KB" % (total / 1024))


if __name__ == "__main__":
    main()
