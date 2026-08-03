"""
Identification engine for the recon app.

Turns raw radio observations into something a human can read:

  * MAC prefix  -> manufacturer, via binary search over the IEEE MA-L registry
  * BLE payload -> product and protocol, via advertisement parsing
  * SSID        -> device class or ISP, via naming conventions

The vendor tables are fixed-width and sorted, so lookup is a binary search.
The 195KB key index is held in RAM (the badge has 8MB of PSRAM and uses almost
none of it), which takes a lookup from 11ms on flash to 1.8ms. The 460KB name
table stays on flash and is read one 24-byte record at a time, only on a cache
miss.

1.8ms is still an eighth of a frame, so callers resolve lazily rather than
inline: see the resolve queue in the app.

Nothing here transmits. Every signal parsed is one the device broadcast openly.
"""

import struct

DATA = "/system/apps/recon/data"
NAME_LEN = 24

# ---- BLE address kinds ------------------------------------------------------

PUBLIC, STATIC, RESOLVABLE, NON_RESOLVABLE = 0, 1, 2, 3

ADDR_KIND_NAME = ("public", "static random", "private (rotates)",
                  "private (rotates)")

# Only public and static-random addresses identify a device over time. The
# other two are re-randomised every ~15 minutes.
STABLE_KINDS = (PUBLIC, STATIC)


def ble_addr_kind(addr_type, addr):
    """Classify a BLE address. `addr` is display order, most significant first."""
    if addr_type == 0:
        return PUBLIC
    top = addr[0] & 0xC0
    if top == 0xC0:
        return STATIC
    if top == 0x40:
        return RESOLVABLE
    return NON_RESOLVABLE


def is_locally_administered(mac):
    """Bit 1 of the first octet: the address was assigned by software, so the
    OUI is meaningless. Randomised WiFi MACs set this."""
    return bool(mac[0] & 0x02)


# ---- vendor tables ----------------------------------------------------------

class Table:
    """Sorted fixed-width key/name-index pairs. Keys in RAM, names on flash."""

    def __init__(self, stem, key_size):
        self.key_size = key_size
        self.rec = key_size + 2
        self.str_path = "%s/%s_str.bin" % (DATA, stem)
        self.cache = {}
        try:
            with open("%s/%s.bin" % (DATA, stem), "rb") as f:
                self.keys = f.read()
        except OSError:
            self.keys = b""
        self.count = len(self.keys) // self.rec

    def lookup(self, key):
        if not self.count:
            return None
        got = self.cache.get(key)
        if got is not None:
            return got or None

        ks, rec, blob = self.key_size, self.rec, self.keys
        idx = None
        lo, hi = 0, self.count - 1
        while lo <= hi:
            mid = (lo + hi) >> 1
            off = mid * rec
            k = blob[off:off + ks]
            if k == key:
                idx = struct.unpack(">H", blob[off + ks:off + rec])[0]
                break
            if k < key:
                lo = mid + 1
            else:
                hi = mid - 1

        name = None
        if idx is not None:
            try:
                with open(self.str_path, "rb") as f:
                    f.seek(idx * NAME_LEN)
                    name = f.read(NAME_LEN).rstrip(b"\x00").decode("utf-8", "replace")
            except OSError:
                pass

        # Cache misses too, as empty, so a crowded room does not re-search.
        if len(self.cache) < 4000:
            self.cache[key] = name or ""
        return name


_oui = None
_btco = None


def _tables():
    global _oui, _btco
    if _oui is None:
        _oui = Table("oui", 3)
        _btco = Table("btco", 2)
    return _oui, _btco


def vendor_for_mac(mac, virtual_ok=False):
    """Manufacturer for a 6-byte MAC, or None when it cannot be known.

    An access point running several SSIDs derives extra BSSIDs from its real
    one by setting the locally-administered bit, so 2E:94:01 is 2C:94:01
    wearing a hat. With virtual_ok, that bit is cleared and the base OUI is
    looked up, which recovers the vendor for every guest and secondary
    network. Client devices set the same bit for privacy randomisation, where
    the base OUI is meaningless, so callers must opt in."""
    oui, _ = _tables()
    if not is_locally_administered(mac):
        return oui.lookup(bytes(mac[:3]))
    if not virtual_ok:
        return None

    # Vendors derive extra BSSIDs by twiddling the low bits of the first
    # octet, but not all the same way: Google flips exactly the
    # locally-administered bit (7C -> 7E), Netgear sets three (28 -> 2E). The
    # top five bits survive either way, so try all eight candidates that share
    # them. This recovers most virtual BSSIDs; the rest are left unknown
    # rather than guessed, because a confidently wrong vendor is worse than
    # none on a tool people use to spot rogue hardware.
    head = mac[0] & 0xF8
    for low in range(8):
        got = oui.lookup(bytes([head | low, mac[1], mac[2]]))
        if got:
            return got
    return None


def company_name(cid):
    _, btco = _tables()
    return btco.lookup(struct.pack(">H", cid))


def db_ready():
    oui, btco = _tables()
    return oui.count > 0 and btco.count > 0


# ---- BLE advertisement parsing ---------------------------------------------

AD_FLAGS = 0x01
AD_UUID16_PART = 0x02
AD_UUID16_ALL = 0x03
AD_UUID128_PART = 0x06
AD_UUID128_ALL = 0x07
AD_NAME_SHORT = 0x08
AD_NAME_FULL = 0x09
AD_TX_POWER = 0x0A
AD_SVC_DATA16 = 0x16
AD_APPEARANCE = 0x19
AD_MANUFACTURER = 0xFF

APPLE = 0x004C
MICROSOFT = 0x0006

# Apple's Continuity protocol multiplexes many features onto one company ID.
# The byte after the company ID says which, and it is the single most useful
# identifier in a room full of Apple hardware.
APPLE_TYPE = {
    0x02: "iBeacon",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "AirPods/pairing",
    0x08: "Hey Siri",
    0x09: "AirPlay target",
    0x0A: "AirPlay source",
    0x0B: "MagicSwitch",
    0x0C: "Handoff",
    0x0D: "Tethering target",
    0x0E: "Tethering source",
    0x0F: "Nearby action",
    0x10: "Nearby info",
    0x12: "Find My",
}

# 16-bit service UUIDs worth calling out by name.
SERVICE = {
    0x1802: "Immediate Alert",
    0x180A: "Device Info",
    0x180D: "Heart Rate",
    0x180F: "Battery",
    0x1812: "HID",
    0x1816: "Cycling Speed",
    0x1818: "Cycling Power",
    0x181A: "Environmental",
    0x1826: "Fitness Machine",
    0x1827: "Mesh Provisioning",
    0x1828: "Mesh Proxy",
    0xFD5A: "Samsung SmartTag",
    0xFD6F: "Exposure Notify",
    0xFDF0: "Google",
    0xFE2C: "Google Fast Pair",
    0xFE95: "Xiaomi",
    0xFE9F: "Google",
    0xFEAA: "Eddystone",
    0xFEED: "Tile",
    0xFEF3: "Google",
}

# Things that follow a person around. Worth surfacing on their own screen.
TRACKERS = {
    0xFD5A: "Samsung SmartTag",
    0xFEED: "Tile",
    0xFD84: "Tile",
}

# GAP appearance, top 10 bits are the category.
APPEARANCE = {
    1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock", 5: "Display",
    6: "Remote", 7: "Glasses", 8: "Tag", 9: "Keyring", 10: "Media Player",
    11: "Barcode Scanner", 12: "Thermometer", 13: "Heart Rate", 14: "Blood Pressure",
    15: "HID", 16: "Glucose Meter", 17: "Running Sensor", 18: "Cycling",
    19: "Control Device", 20: "Network Device", 21: "Sensor", 22: "Light",
    23: "Fan", 24: "HVAC", 25: "Air Purifier", 26: "Vehicle", 27: "Gaming",
    49: "Pulse Oximeter", 50: "Weight Scale", 51: "Personal Mobility",
    81: "Earbuds", 82: "Headset",
}


def parse_adv(data):
    """Walk the length/type/value structures of an advertising payload."""
    out = {"name": None, "company": None, "mfg": None, "services": [],
           "svc_data": [], "appearance": None, "tx_power": None, "flags": None}
    i, n = 0, len(data)
    while i + 1 < n:
        ln = data[i]
        if ln == 0 or i + ln >= n + 1:
            break
        t = data[i + 1]
        v = data[i + 2:i + 1 + ln]
        if t in (AD_NAME_SHORT, AD_NAME_FULL):
            try:
                out["name"] = bytes(v).decode("utf-8").strip()
            except UnicodeError:
                pass  # a device advertising a name that is not valid UTF-8
        elif t == AD_MANUFACTURER and len(v) >= 2:
            out["company"] = v[0] | (v[1] << 8)
            out["mfg"] = bytes(v[2:])
        elif t in (AD_UUID16_PART, AD_UUID16_ALL):
            for j in range(0, len(v) - 1, 2):
                out["services"].append(v[j] | (v[j + 1] << 8))
        elif t == AD_SVC_DATA16 and len(v) >= 2:
            out["svc_data"].append((v[0] | (v[1] << 8), bytes(v[2:])))
        elif t == AD_APPEARANCE and len(v) >= 2:
            out["appearance"] = v[0] | (v[1] << 8)
        elif t == AD_TX_POWER and len(v) >= 1:
            out["tx_power"] = v[0] - 256 if v[0] > 127 else v[0]
        elif t == AD_FLAGS and len(v) >= 1:
            out["flags"] = v[0]
        i += ln + 1
    return out


def describe_ble(addr, adv):
    """(label, detail, tags) for a parsed advertisement.

    label  short, for the list
    detail longer, for the detail page
    tags   notable findings, e.g. "tracker"
    """
    tags = []
    detail = []

    company = adv.get("company")
    vendor = company_name(company) if company is not None else None
    if vendor is None:
        vendor = vendor_for_mac(addr)

    # Apple Continuity is the richest signal in most rooms.
    apple_kind = None
    if company == APPLE and adv.get("mfg"):
        apple_kind = APPLE_TYPE.get(adv["mfg"][0])
        if apple_kind:
            detail.append(apple_kind)
            if adv["mfg"][0] == 0x12:
                tags.append("findmy")
            if adv["mfg"][0] == 0x02:
                tags.append("ibeacon")

    if company == MICROSOFT:
        detail.append("Swift Pair/CDP")

    for uuid, blob in adv.get("svc_data", []):
        known = SERVICE.get(uuid)
        if known:
            detail.append(known)
        if uuid in TRACKERS:
            tags.append("tracker")
        if uuid == 0xFEAA and blob:
            tags.append("beacon")

    for uuid in adv.get("services", []):
        known = SERVICE.get(uuid)
        if known and known not in detail:
            detail.append(known)
        if uuid in TRACKERS and "tracker" not in tags:
            tags.append("tracker")

    ap = adv.get("appearance")
    if ap:
        cat = APPEARANCE.get(ap >> 6)
        if cat and cat not in detail:
            detail.append(cat)

    name = adv.get("name")
    if name:
        label = name
    elif apple_kind and vendor:
        label = "%s %s" % (vendor, apple_kind)
    elif detail and vendor:
        label = "%s %s" % (vendor, detail[0])
    elif vendor:
        label = vendor
    elif detail:
        label = detail[0]
    else:
        label = "%02X:%02X:%02X" % (addr[3], addr[4], addr[5])

    return label, ", ".join(detail), tags


# ---- what kind of thing is it ----------------------------------------------

CAT_AP, CAT_PHONE, CAT_COMPUTER, CAT_WEARABLE, CAT_AUDIO, CAT_TRACKER, \
    CAT_FINDMY, CAT_OTHER = range(8)

CAT_NAME = ("Access Pts", "Phones", "Computers", "Wearables",
            "Audio / TV", "Trackers", "Find My", "Other")

N_CATS = 8

# GAP appearance category -> our bucket.
_APPEARANCE_CAT = {
    1: CAT_PHONE, 2: CAT_COMPUTER, 3: CAT_WEARABLE, 5: CAT_AUDIO,
    8: CAT_TRACKER, 9: CAT_TRACKER, 10: CAT_AUDIO, 13: CAT_WEARABLE,
    15: CAT_COMPUTER, 17: CAT_WEARABLE, 18: CAT_WEARABLE,
    49: CAT_WEARABLE, 50: CAT_WEARABLE, 81: CAT_AUDIO, 82: CAT_AUDIO,
}

# Apple Continuity subtype -> bucket. Find My is deliberately not a tracker
# here: iPhones and Macs emit it too, so counting it as one would report a
# room full of phones as a room full of AirTags.
_APPLE_CAT = {
    0x05: CAT_PHONE,     # AirDrop
    0x07: CAT_AUDIO,     # proximity pairing, i.e. AirPods
    0x09: CAT_AUDIO,     # AirPlay target
    0x0A: CAT_PHONE,     # AirPlay source
    0x0C: CAT_PHONE,     # Handoff
    0x0D: CAT_PHONE,     # tethering target
    0x0E: CAT_PHONE,     # tethering source
    0x10: CAT_PHONE,     # Nearby info
}

# Dedicated trackers only: things whose whole purpose is to be findable.
_TRACKER_SERVICES = (0xFEED, 0xFD5A, 0xFD84)

_NAME_CAT = (
    ("airtag", CAT_TRACKER), ("smarttag", CAT_TRACKER), ("tile", CAT_TRACKER),
    ("airpods", CAT_AUDIO), ("buds", CAT_AUDIO), ("beats", CAT_AUDIO),
    ("bose", CAT_AUDIO), ("jbl", CAT_AUDIO), ("soundcore", CAT_AUDIO),
    ("sonos", CAT_AUDIO), ("echo", CAT_AUDIO), ("chromecast", CAT_AUDIO),
    ("roku", CAT_AUDIO), ("shield", CAT_AUDIO), ("soundbar", CAT_AUDIO),
    (" tv", CAT_AUDIO), ("bravia", CAT_AUDIO),
    ("watch", CAT_WEARABLE), ("band", CAT_WEARABLE), ("fitbit", CAT_WEARABLE),
    ("garmin", CAT_WEARABLE), ("whoop", CAT_WEARABLE), ("oura", CAT_WEARABLE),
    ("polar", CAT_WEARABLE),
    ("iphone", CAT_PHONE), ("pixel", CAT_PHONE), ("galaxy", CAT_PHONE),
    ("oneplus", CAT_PHONE),
    ("macbook", CAT_COMPUTER), ("imac", CAT_COMPUTER), ("ipad", CAT_COMPUTER),
    ("thinkpad", CAT_COMPUTER), ("surface", CAT_COMPUTER),
    ("keyboard", CAT_COMPUTER), ("mouse", CAT_COMPUTER),
)


def classify_ble(adv, label):
    """Bucket a BLE device for the dashboard. Most specific signal wins."""
    for uuid in adv.get("services", []):
        if uuid in _TRACKER_SERVICES:
            return CAT_TRACKER
    for uuid, _blob in adv.get("svc_data", []):
        if uuid in _TRACKER_SERVICES:
            return CAT_TRACKER

    low = (label or "").lower()
    for needle, cat in _NAME_CAT:
        if needle in low:
            return cat

    ap = adv.get("appearance")
    if ap:
        got = _APPEARANCE_CAT.get(ap >> 6)
        if got is not None:
            return got

    # Checked after names and appearance so a device that says "AirTag" is
    # filed as a tracker, but before the generic Apple handling so the rest do
    # not silently become "Phones".
    if is_find_my(adv):
        return CAT_FINDMY

    company = adv.get("company")
    mfg = adv.get("mfg")
    if company == APPLE and mfg:
        got = _APPLE_CAT.get(mfg[0])
        if got is not None:
            return got
    if company == MICROSOFT:
        return CAT_COMPUTER

    for uuid in adv.get("services", []):
        if uuid == 0x1812:
            return CAT_COMPUTER
        if uuid in (0x180D, 0x1816, 0x1818, 0x1826):
            return CAT_WEARABLE

    return CAT_OTHER


def is_find_my(adv):
    """Apple's offline-finding beacon. Emitted by AirTags and by every iPhone
    and Mac with Find My enabled, so it is reported separately rather than
    counted as a tracker."""
    return (adv.get("company") == APPLE and adv.get("mfg")
            and adv["mfg"][0] == 0x12)


def is_apple_continuity(adv):
    """Any of Apple's Continuity advertisements.

    The whole suite is built on privacy-rotating addresses, so a Continuity
    beacon never identifies a device for longer than its rotation period.
    """
    return adv.get("company") == APPLE and bool(adv.get("mfg"))


def has_identity(adv):
    """True when the payload carries something that could name this device
    again after its address changes: a name, a company, or a service."""
    return bool(adv.get("name") or adv.get("company") is not None
                or adv.get("services") or adv.get("svc_data"))


def is_rotating(kind, adv):
    """True when this address will not identify the device again later.

    The address bits are not sufficient on their own, and this took two
    corrections against real data to get right.

    A public address is burned into the hardware and never rotates. Resolvable
    and non-resolvable private addresses always do. The hard case is *static
    random*, which by the bits looks permanent but is what Apple advertises
    Continuity from: an overnight capture logged 99 distinct addresses labelled
    "AirPods", which is a handful of earbuds re-randomising, not 99 pairs in
    adjacent rooms. An earlier two-hour capture did the same with 356 Find My
    addresses that were roughly 55 devices.

    A static-random address that advertises *nothing* is the other half of the
    same problem. Overnight in a hotel room the badge logged a near-constant 20
    new such addresses every hour, 05:00 included, which is not a stationary
    room meeting new devices; it is privacy rotation. And an address with no
    payload cannot be re-identified later even in principle, so counting it as
    a device makes the number mean nothing.

    So a static-random address counts as a device only when the payload gives
    some way to know it again, and does not belong to a rotating scheme.
    """
    if kind == PUBLIC:
        return False        # burned into the hardware; never rotates
    if kind not in STABLE_KINDS:
        return True
    if is_apple_continuity(adv):
        return True
    return not has_identity(adv)


# ---- WiFi -------------------------------------------------------------------

# cyw43 reports an auth-mode index. 0 is reliably open; the rest are labels.
SECURITY = {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA/2", 5: "WPA2",
            6: "WPA3", 7: "WPA2/3"}

# SSID naming conventions, checked as lowercase substrings and prefixes.
# (needle, prefix_only, label)
SSID_HINTS = (
    ("xfinitywifi", False, "Comcast public"),
    ("xfinity", False, "Comcast"),
    ("attwifi", False, "AT&T public"),
    ("att", True, "AT&T"),
    ("spectrumsetup", True, "Spectrum"),
    ("spectrum", False, "Spectrum"),
    ("verizon", False, "Verizon"),
    ("fios", False, "Verizon FiOS"),
    ("netgear", True, "Netgear"),
    ("ntgr_", True, "Netgear"),
    ("orbi", True, "Netgear Orbi"),
    ("linksys", True, "Linksys"),
    ("dlink", True, "D-Link"),
    ("tp-link", True, "TP-Link"),
    ("asus", True, "ASUS"),
    ("eero", False, "eero mesh"),
    ("setup-", True, "Pace/2Wire"),
    ("hp-print", True, "HP printer"),
    ("officejet", False, "HP printer"),
    ("envy", True, "HP printer"),
    ("direct-", True, "WiFi Direct"),
    ("chromecast", False, "Chromecast"),
    ("roku", False, "Roku"),
    ("bravia", False, "Sony TV"),
    ("samsung", False, "Samsung"),
    ("lg_", True, "LG"),
    ("tesla", False, "Tesla"),
    ("iphone", False, "iPhone hotspot"),
    ("android", True, "Android hotspot"),
    ("pixel", True, "Pixel hotspot"),
    ("galaxy", True, "Galaxy hotspot"),
    ("gopro", True, "GoPro"),
    ("dji", True, "DJI drone"),
    ("ring", True, "Ring"),
    ("nest", True, "Nest"),
    ("wyze", True, "Wyze"),
    ("sonos", True, "Sonos"),
    ("defcon", False, "DEF CON"),
    ("dc34", False, "DEF CON"),
    ("bsides", False, "BSides"),
    ("pineapple", False, "WiFi Pineapple"),
    ("karma", False, "Karma"),
    ("free wifi", False, "open bait"),
    ("free public", False, "open bait"),
)


def describe_wifi(ssid, bssid, sec):
    """(label, detail, tags) for an access point."""
    tags = []
    detail = []

    virtual = is_locally_administered(bssid)
    vendor = vendor_for_mac(bssid, virtual_ok=True)
    if vendor:
        detail.append(vendor)
    if virtual:
        # Extremely common and entirely normal: it is how one radio hosts a
        # guest network. Worth showing, not worth alarming about.
        detail.append("virtual AP")
        tags.append("virtual")

    low = (ssid or "").lower()
    for needle, prefix_only, label in SSID_HINTS:
        hit = low.startswith(needle) if prefix_only else (needle in low)
        if hit:
            detail.append(label)
            if label in ("WiFi Pineapple", "Karma", "open bait"):
                tags.append("suspicious")
            break

    if sec == 0:
        tags.append("open")
        detail.append("OPEN")
    elif sec == 1:
        tags.append("wep")
        detail.append("WEP")

    if not ssid:
        tags.append("hidden")

    label = ssid or "<hidden>"
    return label, ", ".join(detail), tags


def classify_wifi(ssid):
    """An access point is an access point, unless it is a phone pretending."""
    low = (ssid or "").lower()
    for needle in ("iphone", "android", "pixel", "galaxy", "'s hotspot"):
        if needle in low:
            return CAT_PHONE
    return CAT_AP


def find_evil_twins(aps):
    """SSIDs advertised by several BSSIDs from different vendors, or offered
    both open and secured. Neither proves an attack (large venues run many
    real APs per SSID) but both are what a rogue looks like, so they are worth
    a human glance.

    `aps` maps bssid -> (ssid, chan, rssi, sec).
    """
    by_ssid = {}
    for bssid, (ssid, _chan, _rssi, sec) in aps.items():
        if not ssid:
            continue
        by_ssid.setdefault(ssid, []).append((bssid, sec))

    flagged = {}
    for ssid, entries in by_ssid.items():
        if len(entries) < 2:
            continue
        secs = {s for _b, s in entries}
        # Only count vendors actually identified. Virtual BSSIDs whose OUI
        # could not be recovered would otherwise each look like a distinct
        # unknown vendor, and flag every multi-SSID home router as a rogue.
        vendors = set()
        for bssid, _s in entries:
            v = vendor_for_mac(bssid, virtual_ok=True)
            if v:
                vendors.add(v)
        mixed_security = 0 in secs and len(secs) > 1
        if mixed_security:
            flagged[ssid] = ("open + secured", len(entries))
        elif len(vendors) > 1:
            flagged[ssid] = ("%d vendors" % len(vendors), len(entries))
    return flagged
