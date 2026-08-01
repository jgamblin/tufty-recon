"""
Persistent, de-duplicated log for the recon app.

Append-only fixed-width binary records on the badge's internal filesystem.
/system is read-only from MicroPython, so this lives on the ~1MB LittleFS root;
fixed-width records keep a four-day conference comfortably inside that.

    wifi  48 bytes/AP      ~3000 APs    = 144KB
    ble   40 bytes/device  ~8000 stable = 320KB

Only stable BLE addresses are written. Resolvable-private addresses rotate
every ~15 minutes, so logging them would fill the disk with one phone.

Records are de-duplicated against what is already on disk at startup, so
restarting the app mid-conference does not double-count. Pull the log off with
tools/export_log.py.
"""

import os
import struct

DIR = "/state"
WIFI_PATH = DIR + "/recon_wifi.bin"
BLE_PATH = DIR + "/recon_ble.bin"
META_PATH = DIR + "/recon_meta.bin"

# < little-endian, no alignment padding.
WIFI_FMT = "<6sBbBI32s"
WIFI_SIZE = struct.calcsize(WIFI_FMT)     # 48
BLE_FMT = "<6sBbHI26s"
BLE_SIZE = struct.calcsize(BLE_FMT)       # 40

NO_COMPANY = 0xFFFF

# The LittleFS root is 1MB and is shared with every other app's state. These
# caps are a byte budget rather than a record count, because the record count
# that fits depends on what else is on the disk.
BUDGET = 560 * 1024        # recon's share
RESERVE = 64 * 1024        # never consume the last of the filesystem

MAX_WIFI = BUDGET // 3 // WIFI_SIZE     # ~3900
MAX_BLE = BUDGET * 2 // 3 // BLE_SIZE   # ~9500


def free_bytes():
    try:
        s = os.statvfs("/")
        return s[0] * s[3]
    except OSError:
        return 0


def _ensure_dir():
    try:
        os.mkdir(DIR)
    except OSError:
        pass  # already there


def _trunc(s, n):
    """Cut a string to n bytes without splitting a UTF-8 character."""
    if not s:
        return b""
    b = s.encode("utf-8")
    if len(b) <= n:
        return b
    return b[:n].decode("utf-8", "ignore").encode("utf-8")


class Log:
    def __init__(self):
        self.wifi_seen = set()
        self.ble_seen = set()
        self.pending_wifi = []
        self.pending_ble = []
        self.first_seen = 0
        self.full = False
        self.unsaved = 0        # seen but not written, once full
        self.load()
        self._check_space()

    # ---- load ---------------------------------------------------------------

    def load(self):
        """Read back the keys already on disk so a restart does not duplicate."""
        for path, size, target in ((WIFI_PATH, WIFI_SIZE, self.wifi_seen),
                                   (BLE_PATH, BLE_SIZE, self.ble_seen)):
            try:
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(size * 64)
                        if not chunk:
                            break
                        for i in range(0, len(chunk) - size + 1, size):
                            target.add(chunk[i:i + 6])
            except OSError:
                pass
        try:
            with open(META_PATH, "rb") as f:
                self.first_seen = struct.unpack("<I", f.read(4))[0]
        except (OSError, ValueError):
            self.first_seen = 0

    # ---- record -------------------------------------------------------------

    def _check_space(self):
        """Stop writing before the filesystem is gone. A logger that silently
        drops everything once the disk fills is worse than one that says so."""
        if self.bytes_used() >= BUDGET or free_bytes() <= RESERVE:
            self.full = True
        return not self.full

    def add_wifi(self, bssid, ssid, chan, rssi, sec, first):
        if bssid in self.wifi_seen:
            return False
        self.wifi_seen.add(bssid)
        if self.full or len(self.wifi_seen) > MAX_WIFI:
            # Still counted for the session, just not committed to disk.
            self.unsaved += 1
            return False
        self.pending_wifi.append(struct.pack(
            WIFI_FMT, bssid, chan & 0xFF, max(-128, min(127, rssi)),
            sec & 0xFF, first, _trunc(ssid, 32)))
        return True

    def add_ble(self, addr, kind, rssi, company, first, label):
        if addr in self.ble_seen:
            return False
        self.ble_seen.add(addr)
        if self.full or len(self.ble_seen) > MAX_BLE:
            self.unsaved += 1
            return False
        self.pending_ble.append(struct.pack(
            BLE_FMT, addr, kind & 0xFF, max(-128, min(127, rssi)),
            NO_COMPANY if company is None else company & 0xFFFF,
            first, _trunc(label, 26)))
        return True

    @property
    def dirty(self):
        return bool(self.pending_wifi or self.pending_ble)

    # ---- flush --------------------------------------------------------------

    def flush(self, now=0):
        """Append what is new. Called on a timer, not per sighting, to spare
        the flash. A failed write is dropped rather than retried forever."""
        if not self.dirty and self.first_seen:
            return
        if not self._check_space():
            self.unsaved += len(self.pending_wifi) + len(self.pending_ble)
            self.pending_wifi = []
            self.pending_ble = []
            return
        try:
            _ensure_dir()
            if self.pending_wifi:
                with open(WIFI_PATH, "ab") as f:
                    for rec in self.pending_wifi:
                        f.write(rec)
            if self.pending_ble:
                with open(BLE_PATH, "ab") as f:
                    for rec in self.pending_ble:
                        f.write(rec)
            if not self.first_seen and now:
                self.first_seen = now
            with open(META_PATH, "wb") as f:
                f.write(struct.pack("<I", self.first_seen))
        except OSError:
            # Out of space despite the check, or a bad write. Either way stop
            # pretending the log is being kept.
            self.full = True
            self.unsaved += len(self.pending_wifi) + len(self.pending_ble)
        self.pending_wifi = []
        self.pending_ble = []

    # ---- erase --------------------------------------------------------------

    def erase(self):
        self.wifi_seen = set()
        self.ble_seen = set()
        self.pending_wifi = []
        self.pending_ble = []
        self.first_seen = 0
        self.full = False
        self.unsaved = 0
        for path in (WIFI_PATH, BLE_PATH, META_PATH):
            try:
                os.remove(path)
            except OSError:
                pass

    # ---- reporting ----------------------------------------------------------

    @property
    def wifi_count(self):
        return len(self.wifi_seen)

    @property
    def ble_count(self):
        return len(self.ble_seen)

    def usage(self):
        """0.0-1.0 of the byte budget, for the meter on the LOG view."""
        return min(1.0, self.bytes_used() / BUDGET)

    def bytes_used(self):
        total = 0
        for path in (WIFI_PATH, BLE_PATH):
            try:
                total += os.stat(path)[6]
            except OSError:
                pass
        return total
