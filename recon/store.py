"""
Persistent, de-duplicated log for the recon app.

Append-only length-prefixed binary records on the badge's internal filesystem.
/system is read-only from MicroPython, so this lives on the ~1MB LittleFS root.

The fields that vary are the two text ones, and they vary a lot: measured over
a travel-day capture, the mean SSID was 7.7 characters against a 32-byte field
and the mean device label 10 against 26. Padding them to a fixed width made
**45% of the log zeroes**. Length-prefixing instead roughly halves it, which
matters because the budget below is the real constraint on how long a
conference can run before the log has to be emptied.

    header  4 bytes, once per file: b"RCN" + format version
    record  1 byte payload length, then the payload

    ap      13 bytes + SSID   (0-32)   -> ~22 typical, was 45
    device  14 bytes + label  (0-26)   -> ~25 typical, was 40

The length prefix is what makes a truncated tail safe. Power can be lost
part-way through an append, and the reader simply stops at the first record
whose payload is short: everything before it is intact, and nothing after it
is misparsed. Corruption in the middle is not a case worth designing for,
since LittleFS checksums blocks.

Only stable BLE addresses are written. Resolvable-private addresses rotate
every ~15 minutes, so logging them would fill the disk with one phone.

Records are de-duplicated against what is already on disk at startup, so
restarting the app mid-conference does not double-count. Pull the log off with
tools/export_log.py.
"""

import os
import struct

DIR = "/state"

# Deliberately not the old names. A v1 fixed-width file left on a badge would
# otherwise be read as v2 and produce confident nonsense; under new names it is
# simply ignored.
WIFI_PATH = DIR + "/recon_ap.bin"
BLE_PATH = DIR + "/recon_dev.bin"
META_PATH = DIR + "/recon_meta.bin"

MAGIC = b"RCN\x02"

# Fixed part of each record; the text tail is length-prefixed on top.
WIFI_HEAD = "<6sBbBI"                     # bssid, channel, rssi, security, first
WIFI_HEAD_SIZE = struct.calcsize(WIFI_HEAD)   # 13
BLE_HEAD = "<6sBbHI"                      # addr, kind, rssi, company, first
BLE_HEAD_SIZE = struct.calcsize(BLE_HEAD)     # 14

MAX_SSID = 32
MAX_LABEL = 26

NO_COMPANY = 0xFFFF

# The LittleFS root is 1MB and is shared with every other app's state.
BUDGET = 560 * 1024        # recon's share
RESERVE = 64 * 1024        # never consume the last of the filesystem

# Record counts are now only a guard on the in-RAM de-duplication sets; the
# byte budget above is what actually bounds the file. Sized from the measured
# typical record so they land near the same place the budget does.
MAX_WIFI = 8000
MAX_BLE = 18000


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


def _keys(path, head_size):
    """Yield the 6-byte key of every intact record in a log file.

    Reads the whole file at once: the budget caps it at 560KB and the badge has
    megabytes of PSRAM free, so walking a buffer beats re-reading the file in
    chunks and stitching records across the seams.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return
    if not blob.startswith(MAGIC):
        return          # foreign or older format; ignore rather than misread
    i, n = len(MAGIC), len(blob)
    while i < n:
        ln = blob[i]
        i += 1
        if ln < head_size or i + ln > n:
            return      # truncated tail: everything before this stands
        yield blob[i:i + 6]
        i += ln


def _ble_walk():
    """Yield (key, address_kind, label) for every intact Bluetooth record.

    One walk, three facts. Layout is BLE_HEAD then the label: addr(6) kind(1)
    rssi(1) company(2) first(4), so the kind sits one byte in and the label is
    whatever follows the fixed head.
    """
    try:
        with open(BLE_PATH, "rb") as f:
            blob = f.read()
    except OSError:
        return
    if not blob.startswith(MAGIC):
        return
    i, n = len(MAGIC), len(blob)
    while i < n:
        ln = blob[i]
        i += 1
        if ln < BLE_HEAD_SIZE or i + ln > n:
            return
        # The label stays raw. Decoding here cost real time for nothing: four
        # out of five records on a conference log are public addresses, whose
        # labels the caller throws away, and decoding all of them added about
        # three seconds to every startup — paid again on each of the ~36
        # watchdog restarts a day.
        yield blob[i:i + 6], blob[i + 6], blob[i + BLE_HEAD_SIZE:i + ln]
        i += ln


class Log:
    def __init__(self):
        self.wifi_seen = set()
        self.ble_seen = set()
        # label -> how many non-public addresses have worn it on disk
        self.label_counts = {}
        self.pending_wifi = []
        self.pending_ble = []
        self.first_seen = 0
        self.full = False
        self.unsaved = 0        # seen but not written, once full
        self.load()
        self._check_space()

    # ---- load ---------------------------------------------------------------

    def load(self):
        """Read back the keys already on disk so a restart does not duplicate.

        The Bluetooth pass also tallies labels per non-public address, because
        the share rule that caps a rotating label at eight lives in RAM and the
        watchdog now restarts the app every fifteen minutes or so. Doing it in
        this walk rather than a second one matters: a separate pass took import
        from 4.5s to 9s, and every one of those restarts pays it.
        """
        for key in _keys(WIFI_PATH, WIFI_HEAD_SIZE):
            self.wifi_seen.add(key)
        counts = self.label_counts
        for key, kind, raw in _ble_walk():
            self.ble_seen.add(key)
            if kind == 0 or not raw:        # public addresses never count
                continue
            try:
                label = bytes(raw).decode("utf-8").rstrip("\x00")
            except UnicodeError:
                continue                    # a damaged label is not counted
            if label:
                counts[label] = counts.get(label, 0) + 1
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
        body = struct.pack(WIFI_HEAD, bssid, chan & 0xFF,
                           max(-128, min(127, rssi)), sec & 0xFF, first)
        body += _trunc(ssid, MAX_SSID)
        self.pending_wifi.append(bytes([len(body)]) + body)
        return True

    def add_ble(self, addr, kind, rssi, company, first, label):
        if addr in self.ble_seen:
            return False
        self.ble_seen.add(addr)
        if self.full or len(self.ble_seen) > MAX_BLE:
            self.unsaved += 1
            return False
        body = struct.pack(BLE_HEAD, addr, kind & 0xFF,
                           max(-128, min(127, rssi)),
                           NO_COMPANY if company is None else company & 0xFFFF,
                           first)
        body += _trunc(label, MAX_LABEL)
        self.pending_ble.append(bytes([len(body)]) + body)
        return True

    @property
    def dirty(self):
        return bool(self.pending_wifi or self.pending_ble)

    # ---- flush --------------------------------------------------------------

    def _append(self, path, records):
        exists = True
        try:
            os.stat(path)
        except OSError:
            exists = False
        with open(path, "ab") as f:
            if not exists:
                f.write(MAGIC)
            for rec in records:
                f.write(rec)

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
                self._append(WIFI_PATH, self.pending_wifi)
            if self.pending_ble:
                self._append(BLE_PATH, self.pending_ble)
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
        # label -> how many non-public addresses have worn it on disk
        self.label_counts = {}
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
