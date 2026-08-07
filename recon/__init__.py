"""
Recon: a WiFi and Bluetooth scanner that tells you what things actually are.

Listens to what the air is already carrying and names it. Every access point
beacon and BLE advertisement parsed here is a public broadcast; the app never
connects, transmits, associates, or captures traffic. It is the same
information your phone's WiFi list shows, plus the vendor and protocol
databases a phone hides from you.

  DASH     what is around you, counted by kind
  LIVE     everything in range, strongest first, identified
  DETAIL   everything known about one device
  FLAGS    open networks, WEP, possible evil twins, Find My beacons
  VENDORS  who makes the hardware in this room
  LOG      the persistent all-week tally
  SHARE    a QR and the URL, for when someone asks what it is

  B      next view          A      drill in / open detail / back
  UP/DN  scroll             C      clear filter, or cycle wifi/ble
  HOME   back to launcher

From DASH, pick a row and press A to see just those devices.

Holding UP+DOWN together for two seconds on LOG erases the log.
"""

import bluetooth
import machine
import network
import os
import qrcode
import sys
import time

APP_DIR = "/system/apps/recon"
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

import identify as ID
from store import Log

W, H = 160, 120

BILLBOARD, DASH, LIVE, FLAGS, VENDORS, LOGVIEW, SHARE = 0, 1, 2, 3, 4, 5, 6
N_VIEWS = 7

F_ALL, F_WIFI, F_BLE = 0, 1, 2
FILTER_NAME = ("ALL", "WIFI", "BLE")

_IRQ_SCAN_RESULT = 5

ROWS = 6
ROW_H = 14

WINDOW_MS = 45_000          # unheard for this long = no longer "in range"
WIFI_EVERY_MS = 20_000
FLUSH_EVERY_MS = 30_000
RESORT_MS = 900             # sorting thousands of entries per frame is not free
RESOLVE_PER_FRAME = 2       # a cold vendor lookup is ~4ms; two fits in a frame

# Hard ceiling on the live set. Measured: 1200 devices holds every view under
# a frame; past that, garbage collection over the object graph alone exceeds
# the frame budget. The persistent log
# holds far more (9557 devices), so this only bounds what is tracked as "in
# the room right now", which is all the live views claim to show.
MAX_LIVE_BLE = 1200

# BLE advertisement floods are a thing people do at conferences: a spammer
# throws out hundreds of advertisements a second, each from a fresh random
# address, to pop up pairing dialogs on nearby phones. Every one of those looks
# to this app like a brand-new device.
#
# Two defences. The interrupt stops queueing once a flood is detected, so it
# never spends the frame budget copying payloads for addresses that will never
# be seen twice. And admission is capped per frame regardless, because draining
# an unbounded queue in one frame is what locked the display up.
#
# What counts as "new" is the whole difficulty, and getting it wrong took the
# badge down four times in one day at BSides. The rate used to be measured
# against the live set: anything not currently tracked counted, every time it
# advertised. That is advertisement volume, not address novelty, and the two
# are nothing alike — fifty real devices re-advertising four times a second
# read as 500/second and latched it.
#
# It could then never let go. Under flood the interrupt admits nothing, so the
# live set goes stale and empties, and with it empty *every* device in the room
# looks new on every single advertisement. The measured rate climbed as the
# room drained. Entry was self-fulfilling and exit was unreachable.
#
# So novelty is tracked separately from the live set, in a set of addresses
# heard recently. A real device enters it once and stops counting no matter how
# often it speaks; a spammer's addresses are new every time by construction.
# Two generations, rotated when the newer fills, bound the memory while keeping
# roughly the last SEEN_CAP addresses of history.
ADMIT_PER_FRAME = 12
FLOOD_WINDOW_MS = 1000
# Measured, not chosen: 41 unfamiliar addresses a second already drags a frame
# out to 1.47 seconds, so a threshold of 60 sat above the point where the badge
# was useless and could only ever be reached by the old, inflated counter. For
# scale, the busiest honest hour ever captured on this badge — an airport
# concourse — averaged 0.17 unfamiliar addresses a second.
FLOOD_ENTER = 15
FLOOD_EXIT = 5
# Walking into a full hall really can present hundreds of unfamiliar addresses
# in one second, so entry has to persist rather than fire on a single window.
FLOOD_CONFIRM = 3
SEEN_CAP = 3000

# The billboard changes only when a count changes or the pulse ticks, so
# redrawing it 30 times a second burns the CPU for no visible gain. Sleeping
# between frames lets the core halt on a wait-for-event instead of spinning.
# Button latency rises to this figure, which is not noticeable in use.
BILLBOARD_IDLE_MS = 150

# Battery telemetry, so runtime is a measurement rather than a guess. One
# sample every few minutes is negligible next to the device log.
BATTERY_EVERY_MS = 120_000
BATTERY_PATH = "/state/battery.csv"
BATTERY_MAX_BYTES = 48 * 1024

# Stale entries are dropped inside the same pass that computes the aggregates,
# so the walk and its key snapshot are paid for once.

# Entry layout. Lists, not dicts: at conference scale the per-object overhead
# of a dict per device is the difference between fitting in RAM and not.
# BLE:  0 kind 1 rssi 2 first 3 last 4 label 5 detail 6 tags 7 company 8 raw 9 cat 10 vendor
# WIFI: 0 ssid 1 chan 2 rssi  3 sec  4 first 5 last  6 label 7 detail 8 tags  9 cat 10 vendor
#
# Vendor is resolved once, here, rather than when a view needs it. Looking it
# up per device per frame cost 437ms a frame in a 3000-device room.

SLATE = color.rgb(11, 15, 22)
FG = color.rgb(226, 238, 248)
DIM = color.rgb(226, 238, 248, 110)
FAINT = color.rgb(255, 255, 255, 26)
CYAN = color.rgb(46, 200, 224)
AMBER = color.rgb(246, 176, 40)
RED = color.rgb(232, 66, 58)
GREEN = color.rgb(30, 190, 120)
VIOLET = color.rgb(158, 122, 244)
PINK = color.rgb(236, 106, 178)
TEAL = color.rgb(64, 196, 168)

HEAD_BG = color.rgb(20, 30, 44)
SEL_BG = color.rgb(255, 255, 255, 30)
BAR_BG = color.rgb(46, 200, 224)
TRACK_BG = color.rgb(255, 255, 255, 34)

# ---- state ------------------------------------------------------------------

wifi = {}         # bssid -> list
ble = {}          # addr  -> list
# Addresses that re-randomise. Counted, never logged as identities. Capped
# because it is otherwise the one structure with no ceiling: a busy room
# produces new rotating addresses indefinitely, and at roughly 45 bytes each an
# unbounded set would outgrow the 8MB of PSRAM over a multi-day conference.
# Past the cap the count keeps rising, it just stops storing new ones.
MAX_ROTATING = 30_000
rotating = set()
rotating_overflow = 0


def _note_rotating(addr):
    global rotating_overflow
    if len(rotating) < MAX_ROTATING:
        rotating.add(addr)
    else:
        rotating_overflow += 1


# A label only identifies a device if it tells that device apart from the next
# one. identify.py can only see one advertisement at a time, so it cannot know
# that "T-Dongle Biscuit" arrived from 66 different addresses in a quarter of
# an hour; that is one spammer, and the name is the least useful thing about
# it. Counting how many random addresses have worn each label catches the
# general case, including schemes nobody has added to ROTATING_SERVICES yet.
#
# Public addresses are exempt and never counted here. They are burned into the
# hardware, so 271 machines all labelled "Matsushita Electronic" really are 271
# machines, and demoting them on a shared label would throw away a casino floor.
LABEL_SHARE_MAX = 8
MAX_LABELS = 4000
label_shares = {}
label_demoted = 0

# WEP access points on invented MAC addresses. One or two could be junk; a
# handful is somebody running a rogue-AP rig.
KARMA_MIN = 3


def _label_discriminates(label):
    """Record one more random address wearing this label, and say whether the
    label still tells devices apart. Demotion is one-way for the session."""
    global label_demoted
    if not label:
        return False
    n = label_shares.get(label)
    if n is None:
        if len(label_shares) >= MAX_LABELS:
            return True     # out of room to judge; trust the label
        label_shares[label] = 1
        return True
    if n > LABEL_SHARE_MAX:
        return False
    label_shares[label] = n + 1
    if n + 1 > LABEL_SHARE_MAX:
        label_demoted += 1
        return False
    return True

pending = []      # BLE addrs awaiting identification
log = Log()

view = BILLBOARD
filt = F_ALL
cat_filter = None   # set by drilling into a dashboard row
cat_cursor = 0
cursor = 0
top = 0
detail_open = False
order = []
evil = {}

# ticks_diff() is only meaningful for values that came from ticks_ms(), so
# timers are seeded one interval in the past rather than with a raw negative.
# A bare -20000 is undefined input and made intervals fire unpredictably.
_boot = time.ticks_ms()
last_sort = time.ticks_add(_boot, -RESORT_MS)
last_wifi = time.ticks_add(_boot, -WIFI_EVERY_MS)
last_flush = _boot
wifi_pending = False
wifi_busy = False
wipe_start = 0
started = time.ticks_ms()

flood = False
flood_rate = 0              # unfamiliar addresses/second, last full window
flood_seen = 0
flood_dropped = 0
flood_window = time.ticks_ms()
flood_hot = 0               # consecutive windows over FLOOD_ENTER

# The BLE listen has to be paused for every WiFi scan, because the two share
# one radio, and then re-armed. Re-arming can fail, and the failure used to be
# swallowed: the app kept running, the screen kept updating, and the badge
# quietly never heard another advertisement for the rest of the session. At one
# WiFi scan every 20 seconds that is a few thousand chances a day for a single
# transient error to end collection.
#
# So the scan is now treated as something that can die and must be revived.
# Silence is the check that does not depend on trusting the return value: any
# room has some Bluetooth traffic, so hearing nothing at all for this long
# means the listen is gone, whatever the API reported.
BLE_SILENCE_MS = 45_000
SCAN_RETRY_MS = 10_000

# How the listen is armed, in one place because it is armed from three.
#
# SCAN_ACTIVE is False, and that is a deliberate correction. An active scan
# transmits a scan request to solicit a response from every advertiser it
# hears, which is how a scanner obtains scan-response payloads. This app shipped
# doing that while its README promised "No deauthentication, injection, or
# anything else that transmits". Passive is what was always claimed, so passive
# is what it now does.
#
# It is also the cheaper of the two by some margin. Measured on the real radio,
# in the same room, back to back: active drew 57.6 advertisements a second,
# passive 34.5, and passive found 36 devices against active's 34. Interrupt rate
# is the load the radio hands the CPU, and it is the one variable that tracks
# the freezes, so a 40% cut costs nothing measurable and might matter.
#
# What passive gives up is the scan response, which is often where a device's
# name lives. That cost was not measurable here: every mode reported zero named
# devices, including active, so this room simply had none to find. If field
# captures show identification getting worse, this is the line to revisit.
SCAN_INTERVAL_US = 30_000
SCAN_WINDOW_US = 30_000
SCAN_ACTIVE = False
ble_scanning = True
last_adv = time.ticks_ms()
last_scan_try = time.ticks_ms()
scan_restarts = 0

frame_start = time.ticks_ms()
last_frame_ms = 0

# A hardware watchdog, because the failure it covers cannot be handled in
# Python. The badge was found at DEF CON with a frozen screen and a dead USB
# port, which is not an app that stopped collecting: a stalled app still
# services USB, since that runs independently of this code. Nothing was
# executing at all, so nothing written here could have noticed or recovered.
# The capture log puts that outage at 3h 40m.
#
# The watchdog does not prevent the hang; it ends it. A wedged badge reboots
# itself in eight seconds instead of staying dead until somebody notices, and
# a reboot is nearly free because the log is on flash and de-duplicates against
# what is already there.
#
# 8000ms is close to the RP2350's ceiling and about five times the worst frame
# ever measured (1.47s under a heavy flood). It is started only after the app
# has imported, which takes longer than the timeout itself. Set to 0 to
# disable; holding C during the boot countdown still reaches the launcher if a
# reboot loop ever needs breaking.
WATCHDOG_MS = 8000
_wdt = None
boot_reason = machine.reset_cause() if hasattr(machine, "reset_cause") else 0
WDT_RESET = getattr(machine, "WDT_RESET", 3)

# Addresses heard recently, kept apart from the live set so that pruning or
# draining `ble` cannot make familiar devices look new again. Two generations:
# when the newer one fills it becomes the older and a fresh one starts, which
# bounds memory at 2 * SEEN_CAP without ever clearing all history at once.
seen_new = set()
seen_old = set()
last_battery = time.ticks_add(time.ticks_ms(), -BATTERY_EVERY_MS)

wlan = network.WLAN(network.STA_IF)
wlan.active(True)


def _log_battery(frame_ms=0):
    """Append one health sample: power, and what the app was doing at the time.

    This is the only record of what the badge was doing when nobody was
    watching, and it is what identified the flood latch. It is deliberately
    more than battery now, because a stall that leaves no trace costs a day of
    conference to diagnose and there is only one conference.
    """
    try:
        if os.stat(BATTERY_PATH)[6] >= BATTERY_MAX_BYTES:
            # Rotate rather than stop. Stopping is silent, and this file went
            # quiet at 48KB on the one day its data was most needed: a whole
            # day of stalls with no heartbeat to localise them. One generation
            # back is kept, so the cap still bounds the space used.
            try:
                os.remove(BATTERY_PATH + ".1")
            except OSError:
                pass
            try:
                os.rename(BATTERY_PATH, BATTERY_PATH + ".1")
            except OSError:
                return      # could not rotate; better to skip than to grow
    except OSError:
        pass        # no file yet
    try:
        with open(BATTERY_PATH, "a") as f:
            # Wall clock first: ticks_ms restarts at zero if the battery dies
            # and the badge reboots, which would otherwise make an overnight
            # run impossible to read. ticks_ms second, because comparing the
            # two is what tells a reboot from a hang.
            f.write("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n" % (
                _now(), time.ticks_ms(), int(badge.battery_voltage() * 1000),
                badge.battery_level(), 1 if badge.usb_connected() else 0,
                badge.light_level(),
                # App state, so the next stall does not need a live badge to
                # explain it: was it flooding, was it hearing anything, was it
                # drawing slowly, and had the radio needed reviving.
                1 if flood else 0, flood_rate, len(ble), frame_ms,
                scan_restarts, 1 if ble_scanning else 0,
                # Why this boot happened, with a caveat: on the RP2350
                # machine.reset() is itself implemented with the watchdog, so
                # a deliberate reset and a watchdog-recovered hang both report
                # WDT_RESET and cannot be told apart by this value alone. What
                # distinguishes them is context — an unattended run that shows
                # a fresh boot nobody asked for is the interesting case.
                boot_reason))
    except OSError:
        pass        # a full disk must not take the app down


def _now():
    """Epoch seconds, or 0 when the RTC has never been set."""
    try:
        t = time.time()
    except OverflowError:
        return 0        # RTC never set
    # An unset RTC on this board reports 2021-01-01, which is comfortably
    # past a 2020 threshold and was being accepted as a real timestamp.
    return t if t > 1_735_689_600 else 0      # 2025-01-01


# ---- radios -----------------------------------------------------------------

# Devices the interrupt has heard but not yet inserted. The interrupt only
# appends here; the main loop does every insertion. Adding a key straight from
# the interrupt could resize `ble` midway through a loop walking it, and the
# interrupt is scheduled, so it fires between bytecodes: exactly during those
# walks. The odds scale with how busy the room is, so it would first go wrong
# at a conference.
inbox = []


def _irq(event, data):
    """Kept deliberately thin. Copying the advertising payload for a device we
    already know would allocate on every packet, and a busy room delivers
    hundreds a second."""
    global flood_seen, flood_dropped, last_adv
    if event != _IRQ_SCAN_RESULT:
        return
    addr_type, addr, _adv_type, rssi, adv = data
    a = bytes(addr)
    now = time.ticks_ms()
    # Proof of life for the radio, and the only such proof there is: everything
    # else the app does keeps working perfectly while the listen is dead.
    last_adv = now

    e = ble.get(a)
    if e is not None:
        # Rewriting slots of an existing entry cannot resize the dict, so this
        # is safe to do from here.
        e[1] = rssi
        e[3] = now
        return

    # Novelty, not volume. A device already heard from recently is familiar
    # however often it speaks, and stays familiar even after the live set has
    # pruned it. Only an address in neither generation counts toward the rate.
    if a not in seen_new and a not in seen_old:
        flood_seen += 1
        # The main loop only ever rebinds these names, never iterates them, so
        # adding here cannot disturb a walk in progress. A rotation landing
        # between the test and the add just misfiles one address.
        seen_new.add(a)

    if flood:
        # Count it and drop it. Not even the payload copy: under a flood these
        # are all fresh random addresses that will never be seen again, and
        # queueing them is what starves the display.
        flood_dropped += 1
        return

    if len(ble) + len(inbox) >= MAX_LIVE_BLE:
        return

    inbox.append((a, ID.ble_addr_kind(addr_type, addr), rssi, now, bytes(adv)))


def _admit_new():
    """Move what the interrupt heard into the live set, from the main loop.

    Capped per frame: draining the whole queue at once is what a flood turned
    into a locked-up display.
    """
    done = 0
    while inbox and done < ADMIT_PER_FRAME:
        done += 1
        a, kind, rssi, now, adv = inbox.pop()
        if a in ble:
            continue
        if kind not in ID.STABLE_KINDS:
            _note_rotating(a)
        ble[a] = [kind, rssi, _now(), now, None, "", (), None, adv,
                  ID.CAT_OTHER, None]
        pending.append(a)


ble_radio = bluetooth.BLE()
ble_radio.active(True)
ble_radio.irq(_irq)
ble_radio.gap_scan(0, SCAN_INTERVAL_US, SCAN_WINDOW_US, SCAN_ACTIVE)


def _ensure_ble_scan(now):
    """Bring the BLE listen back if it has died.

    Two triggers, because neither alone is enough. The flag catches a re-arm
    that raised, which is the failure that can be detected honestly. Silence
    catches the rest: a call that returned success but left the radio not
    delivering, which no return value would have revealed.
    """
    global ble_scanning, last_adv, last_scan_try, scan_restarts
    if wifi_busy:
        return          # the WiFi scan owns the radio right now, by design
    quiet = time.ticks_diff(now, last_adv) > BLE_SILENCE_MS
    if ble_scanning and not quiet:
        return
    if time.ticks_diff(now, last_scan_try) < SCAN_RETRY_MS:
        return          # do not hammer the stack while it is unhappy
    last_scan_try = now
    try:
        ble_radio.gap_scan(None)
    except OSError:
        pass
    try:
        ble_radio.gap_scan(0, SCAN_INTERVAL_US, SCAN_WINDOW_US, SCAN_ACTIVE)
        ble_scanning = True
        scan_restarts += 1
        # Restart the silence clock, or every frame for the next 45 seconds
        # counts as another failure and restarts the scan again.
        last_adv = now
    except OSError:
        ble_scanning = False


def _wifi_scan():
    global evil
    # The cyw43 shares one radio between WiFi and BLE. With BLE holding a
    # continuous scan, wlan.scan() still returns APs but every RSSI comes back
    # as 0, so the listen has to be paused for the duration.
    global ble_scanning
    try:
        ble_radio.gap_scan(None)
    except OSError:
        pass
    ble_scanning = False
    try:
        raw = wlan.scan()
    except OSError:
        return
    finally:
        # Not swallowed any more. If the listen does not come back the flag
        # stays down and _ensure_ble_scan() keeps trying, rather than the badge
        # running deaf until somebody notices and restarts it.
        try:
            ble_radio.gap_scan(0, SCAN_INTERVAL_US, SCAN_WINDOW_US, SCAN_ACTIVE)
            ble_scanning = True
        except OSError:
            pass
    now = time.ticks_ms()
    epoch = _now()
    for ssid_b, bssid_b, chan, rssi, sec, _hidden in raw:
        b = bytes(bssid_b)
        ssid = ssid_b.decode("utf-8", "replace") if ssid_b else ""
        e = wifi.get(b)
        if e is None:
            label, detail, tags = ID.describe_wifi(ssid, b, sec)
            wifi[b] = [ssid, chan, rssi, sec, epoch, now, label, detail, tags,
                       ID.classify_wifi(ssid),
                       ID.vendor_for_mac(b, virtual_ok=True)]
            log.add_wifi(b, ssid, chan, rssi, sec, epoch)
        else:
            e[2] = rssi
            e[5] = now
    # Only meaningful across the whole set, and cheap at WiFi scan cadence.
    evil = ID.find_evil_twins(
        {k: (v[0], v[1], v[2], v[3]) for k, v in wifi.items()})


# ---- identification ---------------------------------------------------------

def _resolve_some():
    """Name a couple of devices per frame. A cold vendor lookup costs about
    4ms, so resolving a backlog all at once would visibly stall."""
    done = 0
    while pending and done < RESOLVE_PER_FRAME:
        a = pending.pop()
        e = ble.get(a)
        if e is None or e[8] is None:
            continue
        try:
            adv = ID.parse_adv(e[8])
            label, detail, tags = ID.describe_ble(a, adv)
            e[4] = label
            e[5] = detail
            e[6] = tuple(tags)
            e[7] = adv.get("company")
            e[9] = ID.classify_ble(adv, label)
            company = e[7]
            e[10] = (ID.company_name(company) if company is not None else None) \
                or ID.vendor_for_mac(a)
            if ID.is_find_my(adv):
                e[6] = e[6] + ("findmy",)
        except Exception:  # noqa: BLE001 - never crash on a malformed payload
            e[4] = "%02X:%02X:%02X" % (a[3], a[4], a[5])
        e[8] = None  # drop the payload; it is the biggest part of the record
        # Decided from the payload, not just the address bits: a Find My
        # beacon looks static by its bits but rotates every ~15 minutes.
        #
        # The second test is the one that does not need to know the scheme:
        # a random address whose label is already worn by a crowd of other
        # random addresses is one device rotating, whoever built it. This is
        # also what keeps a slow flood out of the log, since a spammer under
        # the flood threshold still reaches this line.
        if ID.is_rotating(e[0], adv):
            _note_rotating(a)
        elif e[0] != ID.PUBLIC and not _label_discriminates(e[4]):
            _note_rotating(a)
        else:
            log.add_ble(a, e[0], e[1], e[7], e[2], e[4] or "")
        done += 1


# ---- live set ---------------------------------------------------------------

# Nobody scrolls past this on a six-row screen, and leaving it unbounded means
# a hostile room decides how much work every frame does.
LIVE_CAP = 400


def _live_rows():
    """Merged, filtered, signal-sorted view of what is in range right now.

    Bucketed by RSSI rather than sorted. RSSI is a small integer, so 101
    buckets give an O(n) ordering with no comparisons. The obvious
    sorted(key=lambda) cost 2.9 seconds at 3500 devices, because every
    comparison called back into Python.
    """
    now = time.ticks_ms()
    buckets = [None] * 101

    def put(rssi, kind, key):
        i = -rssi
        if i < 0:
            i = 0
        elif i > 100:
            i = 100
        b = buckets[i]
        if b is None:
            buckets[i] = b = []
        b.append((rssi, kind, key))

    if filt in (F_ALL, F_WIFI):
        for k, e in wifi.items():
            # APs are only re-heard on the WiFi scan cadence, so they get a
            # longer grace period than BLE.
            if time.ticks_diff(now, e[5]) <= WINDOW_MS * 3:
                if cat_filter is None or e[9] == cat_filter:
                    put(e[2], "W", k)
    if filt in (F_ALL, F_BLE):
        for k, e in ble.items():
            if time.ticks_diff(now, e[3]) <= WINDOW_MS:
                if cat_filter is None or e[9] == cat_filter:
                    put(e[1], "B", k)

    rows = []
    for i in range(101):
        b = buckets[i]
        if b:
            rows.extend(b)
            if len(rows) >= LIVE_CAP:
                return rows[:LIVE_CAP]
    return rows


def _entry(kind, key):
    return (wifi if kind == "W" else ble).get(key)


def _label_of(kind, key):
    e = _entry(kind, key)
    if e is None:
        return "?", "", ()
    if kind == "W":
        return e[6], e[7], e[8]
    return (e[4] or "identifying…"), e[5], e[6]


def _bars(rssi):
    if rssi >= -52:
        return 5
    if rssi >= -64:
        return 4
    if rssi >= -74:
        return 3
    if rssi >= -84:
        return 2
    if rssi >= -94:
        return 1
    return 0


def _tag_colour(tags):
    if "open" in tags or "wep" in tags or "suspicious" in tags:
        return RED
    if "tracker" in tags or "findmy" in tags:
        return AMBER
    if "virtual" in tags:
        return None
    return None


# ---- chrome -----------------------------------------------------------------

def _header(title, right=""):
    screen.pen = HEAD_BG
    screen.rectangle(0, 0, W, 13)
    screen.font = rom_font.winds
    screen.pen = CYAN
    screen.text(title, 4, 0)
    if right:
        screen.pen = DIM
        screen.text(right, W - screen.measure_text(right)[0] - 4, 0)


def _footer(left):
    screen.pen = FAINT
    screen.rectangle(0, H - 11, W, 11)
    screen.font = rom_font.winds
    screen.pen = DIM
    screen.text(left, 4, H - 12)
    if wifi_busy:
        screen.pen = CYAN
        screen.text("wifi", W - screen.measure_text("wifi")[0] - 4, H - 12)


def _scrollbar(n):
    if n <= ROWS:
        return
    track = ROWS * ROW_H
    h = max(6, int(track * ROWS / n))
    pos = int((track - h) * top / max(1, n - ROWS))
    screen.pen = color.rgb(255, 255, 255, 70)
    screen.rectangle(W - 2, 15 + pos, 2, h)


def _signal(x, y, rssi, pen):
    n = _bars(rssi)
    for b in range(5):
        screen.pen = pen if b < n else FAINT
        screen.rectangle(x + b * 4, y + 9 - (b + 1) * 2, 3, (b + 1) * 2)


# ---- views ------------------------------------------------------------------

CAT_COLOUR = None  # built on first draw, once the palette exists

# Aggregates are computed by walking every device, which is O(n) and therefore
# cannot happen per frame: at 3000 devices that alone was 50ms. Instead one
# pass is spread across frames in fixed-size chunks, so per-frame cost stays
# flat no matter how hostile the room gets. Views read the last completed pass.
STATS_CHUNK = 300
STATS_EVERY_MS = 2000

stats = {"counts": [0] * ID.N_CATS, "findmy": 0, "open": 0, "wep": 0,
         "karma": 0, "trackers": 0, "vendors": [], "log_kb": 0, "log_use": 0.0,
         "live": 0}

_acc = None
_acc_w = None
_acc_b = None
_acc_i = 0
_last_pass = time.ticks_add(time.ticks_ms(), -STATS_EVERY_MS)

pruned_total = 0


def _stats_step():
    """Advance the rolling pass by one chunk, aggregating and pruning together.

    These were two passes with two key snapshots. Each snapshot is a list as
    long as the device dict, and allocating two of them per cycle triggered
    garbage collection often enough to dominate the frame: at 1200 devices a
    single gc.collect() costs 70ms. Sharing one walk halved the churn and took
    the frame back from 80ms to 10ms.

    Two things here are load-bearing. The snapshots are plain lists of existing
    key objects, not freshly built tuples. And a pass only starts on a timer,
    so a crowded room does not put the badge in a permanent scanning loop.
    """
    global _acc, _acc_w, _acc_b, _acc_i, _last_pass, stats, pruned_total

    now = time.ticks_ms()
    if _acc is None:
        if time.ticks_diff(now, _last_pass) < STATS_EVERY_MS:
            return
        _acc = {"counts": [0] * ID.N_CATS, "findmy": 0, "open": 0, "wep": 0,
                "karma": 0, "trackers": 0, "vendors": {},
                # Two os.stat calls; cheap once a pass, 20ms a frame otherwise.
                "log_kb": log.bytes_used() // 1024, "log_use": log.usage(),
                "live": 0}
        # The BLE dict is mutated from an interrupt, so it cannot be iterated
        # directly across frames.
        _acc_w = list(wifi)
        _acc_b = list(ble)
        _acc_i = 0

    n_w = len(_acc_w)
    total = n_w + len(_acc_b)
    end = _acc_i + STATS_CHUNK
    if end > total:
        end = total

    counts = _acc["counts"]
    vendors = _acc["vendors"]

    for i in range(_acc_i, end):
        if i < n_w:
            e = wifi.get(_acc_w[i])
            if e is None or time.ticks_diff(now, e[5]) > WINDOW_MS * 3:
                continue
            if e[3] == 0:
                _acc["open"] += 1
            elif e[3] == 1:
                _acc["wep"] += 1
                # WEP has been broken since 2001 and ships on nothing current,
                # so a WEP beacon from an invented MAC is not old hardware. A
                # karma rig beacons a stock list of hotspot names to see who
                # bites; one BSides capture had 77 of these across all 13
                # channels, 76 of them locally administered.
                if ID.is_locally_administered(_acc_w[i]):
                    _acc["karma"] += 1
        else:
            key = _acc_b[i - n_w]
            e = ble.get(key)
            if e is None:
                continue
            if time.ticks_diff(now, e[3]) > WINDOW_MS:
                # Stale. Drop it here rather than in a second pass: without
                # this the live set only grows, so every view gets slower all
                # conference and the counts drift from the actual room.
                # Anything still awaiting identification is kept.
                if e[8] is None:
                    del ble[key]
                    pruned_total += 1
                continue
            if "findmy" in e[6]:
                _acc["findmy"] += 1
            if e[9] == ID.CAT_TRACKER:
                _acc["trackers"] += 1

        counts[e[9]] += 1
        _acc["live"] += 1
        v = e[10]
        if v:
            vendors[v] = vendors.get(v, 0) + 1

    _acc_i = end
    if _acc_i >= total:
        _acc["vendors"] = sorted(vendors.items(), key=lambda kv: -kv[1])[:6]
        stats = _acc
        _acc = None
        _acc_w = None
        _acc_b = None
        _last_pass = now


def _draw_no_database():
    """The most likely install mistake is copying recon/ without recon/data/,
    which otherwise looks like a working scanner that recognises nothing."""
    _header("RECON")
    screen.font = rom_font.winds
    screen.pen = RED
    screen.text("vendor database missing", 8, 22)
    screen.pen = DIM
    for i, line in enumerate((
            "recon/data/ did not come",
            "along with the app.",
            "",
            "Re-copy the whole recon",
            "folder into TUFTY/apps.")):
        screen.text(line, 8, 40 + i * 12)


# Worn facing outward, the badge is read from a metre or two away by people
# walking behind. Everything else in this app is designed for arm's length: at
# 12px a capital subtends about 7 arcminutes at 2m, under the ~10 needed to
# read at a glance. This view exists to be legible from back there, so it
# carries one number at 60px (about 34 arcminutes) and almost nothing else.
# Sized to the digit count rather than fixed, so a three-digit total is not
# needlessly small just because a five-digit one has to fit.
BILLBOARD_SIZES = (76, 66, 58, 50, 44)
LABEL_SIZE = 19

# Measured on this face: the ink starts 0.367 of the nominal size below the y
# you pass, and is 0.767 of it tall. Positioning against the em box instead
# clipped the digits off the top of the screen.
INK_TOP = 0.367
_big = None


def _draw_billboard():
    global _big
    if _big is None:
        _big = load_font("MonaSans-Medium")

    total = log.wifi_count + log.ble_count
    s = str(total)
    screen.font = _big

    size = BILLBOARD_SIZES[-1]
    for candidate in BILLBOARD_SIZES:
        if screen.measure_text(s, candidate)[0] <= W - 14:
            size = candidate
            break

    w = screen.measure_text(s, size)[0]
    # The em box sits well above the ink, hence the negative offset.
    screen.pen = CYAN
    screen.text(s, (W - w) / 2, 8 - size * INK_TOP, size)

    # The label is set in the vector face too: at 13px it was about 8
    # arcminutes from two metres, which is decoration rather than text.
    #
    # "RADIOS", not "DEVICES": the number above is access points plus
    # Bluetooth devices, and on one capture 703 of 1,748 were access points.
    # This is the view strangers read, so the caption has to be true.
    label = "RADIOS SEEN"
    lw = screen.measure_text(label, LABEL_SIZE)[0]
    screen.pen = FG
    screen.text(label, (W - lw) / 2, 74 - LABEL_SIZE * INK_TOP, LABEL_SIZE)

    screen.font = rom_font.winds
    if flood:
        # Being spammed is a finding, not just a condition to survive.
        msg = "BLE FLOOD  %d/sec" % flood_rate
        screen.pen = RED
        screen.rectangle(0, 96, W, 13)
        screen.pen = SLATE
        screen.text(msg, (W - screen.measure_text(msg)[0]) / 2, 97)
    else:
        # Rotating addresses are shown alongside, so a small device count does
        # not read as "nothing here" when the air is actually busy.
        alive = "%d live  %d rotating" % (
            stats["live"], len(rotating) + rotating_overflow)
        screen.pen = DIM
        screen.text(alive, (W - screen.measure_text(alive)[0]) / 2, 99)

    # A slow pulse, so a glance says it is still running rather than frozen on
    # a number from an hour ago.
    beat = (badge.ticks // 600) % 2
    screen.pen = GREEN if beat else color.rgb(30, 190, 120, 60)
    screen.rectangle(6, H - 10, 7, 7)


REPO_URL = "https://github.com/jgamblin/tufty-recon"
REPO_TEXT = "github.com/jgamblin"
REPO_TEXT2 = "/tufty-recon"
_qr = None


def _draw_share():
    """Someone asks what the thing on your bag is. This is the answer, at
    arm's length: a code they can scan and the URL written out for when a
    camera will not focus or a phone is in a pocket."""
    global _qr
    if _qr is None:
        _qr = qrcode.QRCode()
        _qr.set_text(REPO_URL)

    n = _qr.get_size()[0]
    scale = 3
    span = n * scale
    ox, oy = (W - span) / 2, 4

    # Quiet zone stays white whatever the theme; scanners need the border.
    screen.pen = color.rgb(255, 255, 255)
    screen.rectangle(ox - 4, oy - 4, span + 8, span + 8)
    screen.pen = color.rgb(0, 0, 0)
    for qy in range(n):
        for qx in range(n):
            if _qr.get_module(qx, qy):
                screen.rectangle(ox + qx * scale, oy + qy * scale, scale, scale)

    # Two lines: the whole URL on one is about 180px against a 160px screen.
    screen.font = rom_font.winds
    y = oy + span + 4
    for line, pen in ((REPO_TEXT, FG), (REPO_TEXT2, CYAN)):
        w = screen.measure_text(line)[0]
        screen.pen = pen
        screen.text(line, (W - w) / 2, y)
        y += 11


def _draw_dash():
    global CAT_COLOUR
    if CAT_COLOUR is None:
        # Cyan is reserved for WiFi across every view, so Access Points is
        # the only category that gets it. Trackers and Find My are deliberately
        # adjacent hues, being the same kind of finding.
        CAT_COLOUR = (CYAN, VIOLET, GREEN, TEAL, PINK, RED, AMBER, DIM)

    counts = stats["counts"]
    _header("RECON", "%d live" % stats["live"])

    # 2 columns x 4 rows. Reading order, so UP/DOWN still walks it linearly.
    CW, CH, TOP = 80, 23, 15
    for i in range(ID.N_CATS):
        cx = (i % 2) * CW
        cy = TOP + (i // 2) * CH
        n = counts[i]

        if i == cat_cursor:
            screen.pen = SEL_BG
            screen.rectangle(cx, cy, CW - 1, CH - 1)

        screen.pen = CAT_COLOUR[i] if n else DIM
        screen.rectangle(cx + 5, cy + 4, 4, 12)

        screen.font = rom_font.smart
        screen.pen = FG if n else DIM
        screen.text(str(n), cx + 13, cy - 1)

        screen.font = rom_font.winds
        screen.pen = DIM
        screen.text(ID.CAT_NAME[i], cx + 13, cy + 12)


def _draw_live():
    shown = len(order)
    _header(ID.CAT_NAME[cat_filter].upper() if cat_filter is not None else "LIVE",
            "%d+" % shown if shown >= LIVE_CAP else "%d" % shown)
    screen.font = rom_font.winds

    if not order:
        screen.pen = DIM
        screen.text("listening...", 46, 52)
        return

    for i in range(ROWS):
        idx = top + i
        if idx >= len(order):
            break
        rssi, kind, key = order[idx]
        y = 15 + i * ROW_H
        if idx == cursor:
            screen.pen = SEL_BG
            screen.rectangle(0, y - 1, W, ROW_H)

        label, _detail, tags = _label_of(kind, key)
        # Cyan bars mean WiFi, violet mean Bluetooth, the same as everywhere
        # else. A "W"/"B" letter only repeated that, and cost 9px of a name
        # that was already being truncated.
        _signal(3, y, rssi, CYAN if kind == "W" else VIOLET)

        screen.pen = _tag_colour(tags) or FG
        if len(label) > 18:
            label = label[:17] + "…"
        screen.text(label, 27, y)

    _scrollbar(len(order))


def _draw_detail():
    rssi, kind, key = order[cursor]
    e = _entry(kind, key)
    if e is None:
        return
    label, detail, tags = _label_of(kind, key)

    _header("WIFI AP" if kind == "W" else "BLE DEVICE", "%d dBm" % rssi)
    screen.font = rom_font.winds

    screen.pen = FG
    screen.text(label[:24], 4, 15)

    rows = [("mac", ":".join("%02X" % b for b in key))]
    if kind == "W":
        rows.append(("vendor", e[10] or "unknown"))
        rows.append(("channel", "%d  %s" % (e[1], ID.SECURITY.get(e[3], "?"))))
        if detail:
            rows.append(("looks like", detail))
    else:
        rows.append(("address", ID.ADDR_KIND_NAME[e[0]]))
        rows.append(("company", e[10] or "unknown"))
        if detail:
            rows.append(("protocol", detail))

    y = 29
    for k, v in rows:
        screen.pen = DIM
        screen.text(k, 4, y)
        screen.pen = FG
        screen.text(str(v)[:19], 52, y)
        y += 13

    if tags:
        screen.pen = DIM
        screen.text("flags", 4, H - 25)
        screen.pen = _tag_colour(tags) or GREEN
        screen.text(" ".join(tags)[:19], 52, H - 25)


def _draw_flags():
    _header("FLAGS", "%d live" % stats["live"])
    screen.font = rom_font.winds

    open_aps = stats["open"]
    wep_aps = stats["wep"]
    karma = stats["karma"]
    trackers = stats["trackers"]
    findmy = stats["findmy"]

    rows = (
        ("open networks", open_aps, RED if open_aps else GREEN),
        ("WEP (ancient)", wep_aps, RED if wep_aps else GREEN),
        ("possible twins", len(evil), AMBER if evil else GREEN),
        ("trackers", trackers, AMBER if trackers else GREEN),
        ("find my", findmy, DIM),
        ("ble flood /sec", flood_rate, RED if flood else GREEN),
    )
    y = 16
    for name, n, col in rows:
        screen.pen = DIM
        screen.text(name, 8, y)
        screen.pen = col
        screen.text(str(n), 120, y)
        y += 13

    # Name the most interesting finding; a count alone is not actionable.
    # Exactly one line fits between the last row and the footer bar at H-11,
    # so the loudest finding wins and says everything on that line. A second
    # line here draws underneath the footer text.
    #
    # A deaf radio outranks everything: every other number on this page is
    # describing a room the badge has stopped listening to.
    if not ble_scanning or time.ticks_diff(time.ticks_ms(), last_adv) > BLE_SILENCE_MS:
        screen.pen = RED
        screen.text("BLE LISTEN DEAD (%d)" % scan_restarts, 8, y + 4)
    elif flood:
        screen.pen = RED
        screen.text("FLOODING: %d dropped" % flood_dropped, 8, y + 4)
    elif karma >= KARMA_MIN:
        screen.pen = RED
        screen.text("KARMA RIG: %d fake APs" % karma, 8, y + 4)
    elif evil:
        ssid = list(evil.keys())[0]
        _why, count = evil[ssid]
        screen.pen = AMBER
        screen.text("twin? %s x%d" % (ssid[:12], count), 8, y + 4)
    else:
        screen.pen = DIM
        screen.text("find my = phones too", 8, y + 4)


def _draw_vendors():
    _header("VENDORS", "%d live" % stats["live"])
    screen.font = rom_font.winds

    rows = stats["vendors"]
    if not rows:
        screen.pen = DIM
        screen.text("identifying...", 44, 52)
        return

    peak = rows[0][1]
    y = 15
    for name, n in rows:
        screen.pen = FG
        screen.text(name[:20], 4, y)
        screen.pen = DIM
        s = str(n)
        screen.text(s, W - screen.measure_text(s)[0] - 4, y)

        # Magnitude as a rule beneath the row. A filled block behind the text
        # reads as a half-selected row at this size, not as a quantity.
        screen.pen = TRACK_BG
        screen.rectangle(4, y + 12, 152, 2)
        screen.pen = BAR_BG
        screen.rectangle(4, y + 12, max(1, int(152 * n / peak)), 2)
        y += 15


def _draw_log():
    _header("LOG", "%d KB" % stats["log_kb"])
    screen.font = rom_font.winds

    mins = time.ticks_diff(time.ticks_ms(), started) // 60000
    use = stats["log_use"]
    rows = [
        ("access points", str(log.wifi_count), FG),
        ("ble devices", str(log.ble_count), FG),
        ("rotating", "~%d" % (len(rotating) + rotating_overflow), DIM),
        ("bluetooth held", "%d" % len(ble), DIM),
        ("in range now", str(stats["live"]), FG),
        ("session", "%dm" % mins, DIM),
    ]
    if log.unsaved:
        rows.append(("NOT SAVED", str(log.unsaved), RED))
    if not _now():
        # A flat battery resets the RTC, and every record logged afterwards
        # carries no timestamp. The capture itself is fine, but nothing can
        # place it on a day, so merge_week has no day to file it under and a
        # whole conference day drops out of the report. Cheap to notice here,
        # expensive to discover a week later.
        rows.append(("NO CLOCK", "unset", RED))
    y = 15
    for name, val, col in rows:
        screen.pen = DIM
        screen.text(name, 8, y)
        screen.pen = col
        screen.text(val, 104, y)
        y += 12

    # Capacity meter. The filesystem is 1MB and shared, so filling it silently
    # is a real way to lose a day of collection.
    bar_y = y + 3
    meter = RED if log.full else (AMBER if use > 0.75 else GREEN)
    screen.pen = FAINT
    screen.rectangle(8, bar_y, 144, 7)
    screen.pen = meter
    if use > 0:
        screen.rectangle(8, bar_y, max(1, int(144 * use)), 7)

    screen.pen = meter
    if log.full:
        msg = "LOG FULL - export now"
    elif use > 0.75:
        msg = "%d%% full - export soon" % (use * 100)
    else:
        msg = "%d KB used  (%d%%)" % (stats["log_kb"], use * 100)
    screen.text(msg, 8, bar_y + 10)

    if wipe_start:
        held = badge.ticks - wipe_start
        screen.pen = RED
        screen.rectangle(0, H - 9, int(W * min(1.0, held / 2000)), 9)
        screen.pen = FG
        screen.text("hold to erase", 40, H - 11)


# ---- main loop --------------------------------------------------------------

def update():
    global view, filt, cursor, top, order, last_sort, last_wifi, last_flush
    global wifi_pending, wifi_busy, detail_open, wipe_start
    global cat_filter, cat_cursor, last_battery
    global flood, flood_rate, flood_seen, flood_window, label_demoted
    global flood_hot, seen_new, seen_old, last_frame_ms, frame_start, _wdt

    now_ms = time.ticks_ms()
    # How long the previous frame took, recorded into the health log. A badge
    # that has slowed to one frame a second is indistinguishable from a frozen
    # one at arm's length, and the two need different fixes.
    last_frame_ms = time.ticks_diff(now_ms, frame_start)

    # Armed on the first frame rather than at import, because importing takes
    # longer than the timeout. From here on, anything that stops this loop for
    # eight seconds reboots the badge instead of ending its day.
    if _wdt is None:
        if WATCHDOG_MS:
            try:
                _wdt = machine.WDT(timeout=WATCHDOG_MS)
            except Exception:   # noqa: BLE001 - a badge without one still runs
                _wdt = False
        else:
            _wdt = False
    elif _wdt:
        _wdt.feed()

    # ---- input
    if badge.pressed(BUTTON_B) and not detail_open:
        view = (view + 1) % N_VIEWS
        cursor = top = 0
        if view == LIVE:
            last_sort = time.ticks_add(time.ticks_ms(), -RESORT_MS)
        if view == DASH:
            cat_filter = None
            last_sort = time.ticks_add(time.ticks_ms(), -RESORT_MS)

    if badge.pressed(BUTTON_A):
        if view == DASH:
            # Drill into the selected bucket.
            cat_filter = cat_cursor
            filt = F_ALL
            view = LIVE
            cursor = top = 0
            last_sort = time.ticks_add(time.ticks_ms(), -RESORT_MS)
        elif view == LIVE and order:
            detail_open = not detail_open

    if badge.pressed(BUTTON_C) and not detail_open:
        if view == LIVE and cat_filter is not None:
            cat_filter = None          # first press clears the drill-down
        else:
            filt = (filt + 1) % 3
        cursor = top = 0
        last_sort = time.ticks_add(time.ticks_ms(), -RESORT_MS)

    if not detail_open:
        if view == DASH:
            if badge.pressed(BUTTON_DOWN):
                cat_cursor = (cat_cursor + 1) % ID.N_CATS
            if badge.pressed(BUTTON_UP):
                cat_cursor = (cat_cursor - 1) % ID.N_CATS
        else:
            if badge.pressed(BUTTON_DOWN) and cursor < len(order) - 1:
                cursor += 1
                if cursor >= top + ROWS:
                    top = cursor - ROWS + 1
            if badge.pressed(BUTTON_UP) and cursor > 0:
                cursor -= 1
                if cursor < top:
                    top = cursor

    if view == LOGVIEW and badge.held(BUTTON_UP) and badge.held(BUTTON_DOWN):
        if not wipe_start:
            wipe_start = badge.ticks
        elif badge.ticks - wipe_start > 2000:
            log.erase()
            wifi.clear()
            ble.clear()
            rotating.clear()
            label_shares.clear()
            label_demoted = 0
            seen_new.clear()
            seen_old.clear()
            flood_hot = 0
            del inbox[:]
            wipe_start = 0
    else:
        wipe_start = 0

    # ---- flood state, once a second
    if time.ticks_diff(now_ms, flood_window) >= FLOOD_WINDOW_MS:
        elapsed = time.ticks_diff(now_ms, flood_window)
        flood_rate = flood_seen * 1000 // max(1, elapsed)
        if flood_rate >= FLOOD_ENTER:
            flood_hot += 1
        else:
            flood_hot = 0
        if flood:
            # Exit is reachable now only because the rate is measured against
            # addresses heard recently rather than against the live set: a room
            # the badge has stopped admitting still reads as familiar.
            if flood_rate < FLOOD_EXIT:
                flood = False
        elif flood_hot >= FLOOD_CONFIRM:
            flood = True
            del inbox[:]        # whatever is queued is already spam
        flood_seen = 0
        flood_window = now_ms

        # Rotate the novelty history once the newer generation fills, so the
        # badge remembers roughly the last SEEN_CAP addresses without the set
        # growing all conference.
        if len(seen_new) >= SEEN_CAP:
            seen_old = seen_new
            seen_new = set()

    # ---- work
    _ensure_ble_scan(now_ms)
    _admit_new()
    _resolve_some()
    _stats_step()

    # Only the live list and its detail page read `order`, so only they pay
    # to rebuild it. The billboard runs all day and never looks at it.
    if (view == LIVE or detail_open) \
            and time.ticks_diff(now_ms, last_sort) >= RESORT_MS:
        last_sort = now_ms
        order = _live_rows()
        if cursor >= len(order):
            cursor = max(0, len(order) - 1)
        if top > cursor:
            top = cursor

    if time.ticks_diff(now_ms, last_wifi) >= WIFI_EVERY_MS:
        last_wifi = now_ms
        wifi_pending = True

    if log.dirty and time.ticks_diff(now_ms, last_flush) >= FLUSH_EVERY_MS:
        last_flush = now_ms
        log.flush(_now())

    if time.ticks_diff(now_ms, last_battery) >= BATTERY_EVERY_MS:
        last_battery = now_ms
        _log_battery(last_frame_ms)

    # ---- draw
    frame_start = now_ms
    screen.pen = SLATE
    screen.clear()

    if not ID.db_ready():
        _draw_no_database()
        _footer("HOME to exit")
        return

    if detail_open and view == LIVE and order:
        _draw_detail()
        _footer("A back")
    elif view == BILLBOARD:
        _draw_billboard()
    elif view == DASH:
        _draw_dash()
        _footer("A drill in   B views")
    elif view == LIVE:
        _draw_live()
        _footer("A detail  C %s" % (
            "all" if cat_filter is not None else FILTER_NAME[filt]))
    elif view == FLAGS:
        _draw_flags()
        _footer("B next")
    elif view == VENDORS:
        _draw_vendors()
        _footer("B next")
    elif view == LOGVIEW:
        _draw_log()
        _footer("UP+DN erase")
    else:
        _draw_share()

    if wifi_pending:
        # Show the marker before the scan blocks for a couple of seconds.
        wifi_busy = True
        badge.update()
        _wifi_scan()
        wifi_pending = False
        wifi_busy = False

    if view == BILLBOARD and not detail_open:
        # Nothing on this screen moves faster than the pulse.
        time.sleep_ms(BILLBOARD_IDLE_MS)



def on_exit():
    log.flush(_now())
    try:
        ble_radio.gap_scan(None)
    except OSError:
        pass
    ble_radio.active(False)
    wlan.active(False)


run(update)
