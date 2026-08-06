# Engineering notes

Detail that would drown the [README](README.md): what broke, what it cost, and
why things are built the way they are. Written down mostly because the
measurements were surprising.

## Firmware baseline

Built against Pimoroni badgeware **v2.0.2**. That release renamed the injected
`io` global to `badge` and turned button state into calls
(`badge.pressed(BUTTON_B)`), so apps written for 2.0.x will not run on 1.x and
vice versa.

`/system` is read-only from MicroPython. Installing means putting the badge into
USB mass storage mode and writing to the mounted volume, which is why
`tools/deploy.sh` exists and why `mpremote fs cp` does not work for this.

`mpremote` halts `main.py` when it connects, so any diagnostic run leaves the
badge at a REPL with a dark screen until something resets it.

`time.ticks_diff()` is only meaningful for values that came from `ticks_ms()`.
Seeding timers with raw negatives like `-20000` is undefined input and made
intervals fire unpredictably; a batch of performance measurements was nonsense
until that was found.

## Scale

A conference hall is two orders of magnitude busier than a quiet room, and the
failure modes only appear there, so `tools/stress_recon.py` injects synthetic
devices. Every view now holds under 11ms a frame up to 1800 devices, against a
shipped live-set cap of 1200.

Measured at 3000 devices, before any of this:

| Problem | Cost |
| --- | --- |
| `sorted(key=lambda)` on the live list | **2.9 s** at 3500 devices |
| Vendor tally recomputed per frame | **437 ms** a frame |
| Two passes with two key snapshots | **80 ms** a frame from GC alone |

Three fixes:

- **Counting sort.** Every comparison in `sorted(key=lambda)` calls back into
  Python. RSSI is a small integer, so the live list is bucketed into 101 slots
  instead, and capped at 400 rows because nobody scrolls further on a six-row
  screen.
- **One rolling aggregate pass.** Category counts, vendor tallies and flag
  counts were each recomputed per frame per view. They are now a single pass
  spread across frames in fixed chunks, and vendors resolve once at insert
  rather than per view per frame.
- **One key snapshot, not two.** Aggregating and pruning each snapshotted the
  device dict. Allocating two lists that size per cycle triggered garbage
  collection often enough to dominate the frame: at 1200 devices a single
  `gc.collect()` costs 70ms, because GC cost scales with live object count.
  Sharing one walk took the frame from 80ms back to 10ms.

Stale devices are dropped inside that same pass. Without it the live set only
grows, so every view gets slower all conference and the dashboard drifts from
the room you are actually standing in.

For context, `badge.update()` costs about 10ms on top of a 16.7ms vsync period,
so the real drawing budget for 59fps is roughly 6.7ms. Pimoroni's own stock
apps measure 51fps (clock), 27fps (tennis) and 11fps (the stock badge).

## Identification

`recon/data/` holds two tables, both sorted fixed-width records so lookup is a
binary search with no parsing and no index:

| File | Contents |
| --- | --- |
| `oui.bin` | 39,877 records: 3-byte MAC prefix + uint16 name index |
| `oui_str.bin` | 19,634 unique vendor names, 24 bytes each |
| `btco.bin` | 3,988 records: uint16 company ID + uint16 name index |
| `btco_str.bin` | 3,975 unique names |

All integers are big-endian so comparing raw key bytes gives the same order as
comparing the numbers. Little-endian keys sort differently as bytes than as
integers, which silently breaks the binary search — that bug made every
Bluetooth company lookup return nothing.

Names are truncated to 24 bytes on a UTF-8 character boundary. Counting
characters instead of bytes left a mangled trailing byte on any name with an
accent or a CJK glyph.

The 195KB key index is read into RAM at startup (the badge has 8MB of PSRAM
doing nothing), which takes a lookup from 11ms on flash to 1.8ms. Names stay on
flash and are read one 24-byte record at a time on a cache miss.

Beyond the tables, `identify.py` carries hand-written maps for Apple Continuity
subtypes, GAP appearance categories, notable 16-bit service UUIDs, and SSID
naming conventions.

## The log on disk

Length-prefixed binary records on the LittleFS root, measured at **21.7 bytes
per access point and 26.3 per device** on live capture.

They used to be fixed-width at 45 and 40, which padded the two text fields to
32 and 26 bytes. Measured across a travel-day capture the mean SSID was 7.7
characters and the mean device label 10, so **45% of the log was zeroes**.
Length-prefixing roughly halves it and so roughly doubles how long a
conference can run before the budget is reached.

Each record is one length byte then the payload, after a four-byte file header
(`RCN\x02`). The length prefix is what makes a truncated tail safe: power can
be lost part-way through an append, and the reader stops at the first record
whose payload is short, leaving everything before it intact and misparsing
nothing after it. Verified by truncating a log at three different offsets: each
lost only the cut record and raised nothing. A file without the header is
ignored rather than read, so a log from the previous format cannot be decoded
as confident nonsense.

**It cannot fill the disk.** The root is 1024KB and shared with every other
app's state, so recon takes a 560KB budget and stops there, never letting free
space fall below a 64KB reserve. The byte budget is what bounds the file; the
record counts are now only a guard on the in-RAM de-duplication sets.

The LOG view carries a capacity meter that turns amber past 75% and red at
full, and once full it counts what it is seeing but not saving as "NOT SAVED".
That matters because the first version swallowed the out-of-space error, so a
full disk would have looked exactly like a working logger right up until you
exported nothing.

Records are de-duplicated against what is already on disk at startup, so
restarting mid-conference does not double-count.

## Counting Bluetooth honestly

Address kinds, from the top bits of the first octet:

| Kind | Rotates? | Counted as a device? |
| --- | --- | --- |
| public | no | yes |
| static random | usually not | yes, unless the payload says otherwise |
| resolvable private | ~15 min | no |
| non-resolvable private | yes | no |

Static random is the hard case, and it took three passes against real captures
to get right. By the bits it looks permanent; in practice it is what most
privacy-rotating stacks advertise from.

- **Apple Find My.** A two-hour outing logged 356 distinct Find My addresses in
  96 minutes, 116 inside a single 10-minute window: roughly 55 real devices.
- **The rest of Apple Continuity.** An overnight capture in a hotel room logged
  99 addresses labelled "AirPods". That is a handful of earbuds
  re-randomising, not 99 pairs in adjacent rooms. The whole Continuity suite
  rotates, not just Find My.
- **Anything advertising no identity at all.** The same night logged a
  near-constant 20 new bare addresses every hour, 05:00 included, from a badge
  sitting still in one room. Real devices would show a day/night curve. And an
  address with no name, company or service cannot be re-identified later even
  in principle, so counting it as a device makes the number mean nothing.
- **Apple is not special.** Microsoft's Connected Devices Platform, the beacon
  behind the Windows "add a device" popup, and Google's Fast Pair rotate the
  same way and carry a company ID or a named service, so both cleared every
  test above. A BSides floor logged 153 addresses announcing Swift Pair inside
  fifteen minutes. `ROTATING_SERVICES` and a check on the Microsoft company ID
  put them in the same bucket as Continuity.

A static-random address is therefore counted as a device only when the payload
carries something that could name it again and does not belong to a rotating
scheme. Applied to that hotel night, 389 "devices" became 6 — a fitness band, a
Tile, and four public-address devices — with the other 383 counted as rotating,
which is what they were. Applied to an airport concourse the same rules keep
473 of 1200, because a concourse really is full of public-address hardware.

### A label has to discriminate

Naming each rotating scheme is a treadmill, and the Swift Pair miss is what it
costs. So there is a second rule underneath, which needs to know nothing about
any vendor: **count how many random addresses have worn each label, and stop
believing a label once too many have.**

`has_identity()` only ever asked whether a payload carries a name, never
whether that name tells this device apart from the next one. Sixty-six
addresses all calling themselves "T-Dongle Biscuit" are one ESP32 dongle, and
the name is the least informative thing about it. Past `LABEL_SHARE_MAX` shared
labels the badge stops counting new wearers as devices.

Public addresses are exempt and never counted toward a label's share. They are
burned into the hardware, so 271 machines all labelled "Matsushita Electronic"
really are 271 machines. Demoting on a shared label alone would have thrown
away an entire casino floor — the same capture where the rule was needed.

Together the two rules take that BSides day from 1,045 logged addresses to 810,
drop 167 to the scheme table and 68 to the share rule, and leave all 672 public
addresses untouched.

## Advertisement floods

People spam Continuity advertisements at conferences to pop pairing dialogs on
nearby phones: hundreds a second, each from a fresh random address. Every one
looks to this app like a brand-new device, and it locked the display up.

The mechanism was not the volume itself but where it landed. `_admit_new()`
drained its whole queue every frame, so a flood meant up to a thousand dict
insertions plus payload copies inside a single frame.

Two defences, because either alone leaves a hole:

- **Admission is capped per frame**, flood or not. An unbounded drain is a
  latent stall no matter what fills the queue.
- **The interrupt stops queueing once a flood is detected**, so it does not
  even copy the payloads.

### What counts as "new" is the whole problem

The first version of this measured novelty against the live set: any address
not currently tracked counted toward the rate, every time it advertised. That
is advertisement volume, not address novelty, and a real device advertises
several times a second forever. Fifty honest devices read as 507/second.

It was also a one-way door. Under flood the interrupt admits nothing, so the
live set goes stale and empties, and against an empty set *every* device in
the room looks new on *every* advertisement. The measured rate climbed as the
room drained. Entry was self-fulfilling and exit was unreachable, so collection
stayed dead until the app was restarted by hand. That happened four times in
one day at BSides before the battery log gave it away: capture and the
two-minute battery heartbeat went silent in the same windows, with USB
connected and the voltage flat, which ruled out everything else.

So novelty now lives in its own set of recently-heard addresses, deliberately
independent of the live set, in two generations rotated at `SEEN_CAP` to bound
memory. A real device enters it once and is familiar from then on however often
it speaks and whether or not it is still tracked; a spammer's addresses are new
every time by construction. Draining the live set can no longer corrupt the
measurement, which is what makes exit reachable at all.

Entry also has to persist for `FLOOD_CONFIRM` consecutive windows, because
walking into a full hall genuinely does present hundreds of unfamiliar
addresses in one second.

The thresholds are measured rather than chosen. **41 unfamiliar addresses a
second already drags a frame out to 1.47 seconds**, so the old entry point of
60 sat above the point where the badge was already unusable and was only ever
reached by the inflated counter. Entry is 15/second and exit 5; for scale, the
busiest honest hour ever captured on this badge, an airport concourse, averaged
0.17 unfamiliar addresses a second.

Being flooded is reported rather than merely survived: the billboard shows
`BLE FLOOD n/sec` and the flags page counts what was dropped. At a conference
that is a finding worth seeing.

**This protects the display, not the log**, and an earlier version of this file
claimed otherwise: that flood addresses were Continuity beacons the rotation
rules already rejected, so the log needed no defence of its own. A BSides
capture disproved it. That spammer used *Microsoft* Swift Pair, which sailed
straight through, and 235 phantom records reached the disk.

The rate is why these are two separate problems. That burst ran at roughly 0.25
new addresses a second, nowhere near the 60/second that latches flood mode,
because it did not need to be fast to work: stalling a display takes volume,
but polluting a log only takes patience. Anything under the threshold reaches
the identification path untouched, so **the log's defence has to be
identity-based rather than rate-based**, which is what the two rules in the
previous section are for.

`tools/flood_recon.py` drives the app's own interrupt at a chosen rate so the
defence can be measured rather than assumed.

## Rogue access points

WEP has been broken since 2001 and ships on nothing current, so a WEP beacon is
already odd. A WEP beacon from a *locally administered* MAC is not old hardware
at all: the address was invented in software, and something is pretending to be
an access point.

The BSides floor had 77 of them, 76 locally administered, spread across all 13
channels, advertising 40 different SSIDs. The names are the giveaway — the
stock target list of a karma rig, mixing ISP defaults from six countries
(`KPN Fon`, `SKY`, `Virgin Media`, `Telekom_FON`, `Ziggo`, `SFR WiFi Mobile`)
with the free-WiFi names phones join without prompting (`Google Starbucks`,
`McDonald's Free WiFi`, `Airport Free WiFi`, `eduroam`).

`WEP AND locally-administered` is the whole heuristic, and past `KARMA_MIN` the
flags page names it. This was the most interesting thing the badge saw all day
and it was invisible until the log came off as CSV, which is the argument for
putting it on screen.

## Battery

Measured, not estimated. Left running recon on a full charge with both radios
up and the screen lit, sampling its own voltage every two minutes:

| | |
| --- | --- |
| Runtime | **9h 46m**, 17:57 to 03:43 |
| Discharge | 139 mV/hour, 4163 mV down to 2793 mV |
| Cut-out | 2793 mV |

That is the heaviest configuration this thing runs, and it is not a conference
day. Plan on a power bank.

Two things that only showed up over a full night:

- **The cut-out is 2793 mV, not the textbook 3300.** `battery_report.py`
  originally assumed 3300 and reported a 9h 46m runtime as 6h 27m.
- **The clock does not survive the battery dying.** The RTC comes back at
  2021-01-01, so log timestamps after an unattended death are meaningless.
  `_now()` returns 0 rather than a wrong date, so records say "unknown"
  instead of lying, but the clock needs resetting after any full discharge.

There was also one unexplained reboot at 20:05, at 3846 mV with about 70% left,
so not a brown-out. The log de-duplicates against what is already on disk at
startup, so it cost nothing measurable, but the cause is still unknown.

## Tooling

| Tool | What it does |
| --- | --- |
| `tools/deploy.sh` | Push recon to a connected badge via mass storage. |
| `tools/run.sh` | Run an on-device script, locating the badge automatically. |
| `tools/smoke.py` | Run the app for 40 frames on-device; report failures, fps, memory. |
| `tools/stress_recon.py` | Drive it at conference scale with synthetic devices. |
| `tools/flood_recon.py` | Simulate a BLE advertisement flood against it. |
| `tools/busy_room.py` | Check that a crowded honest room is not read as a flood. |
| `tools/flood_recovery.py` | Check that a latched flood clears once the spammer leaves. |
| `tools/verify_rotation.py` | Check the rotation and rogue-AP rules on-device. |
| `tools/merge_week.py` | Stitch nightly exports into one week, with a publishable summary. |
| `tools/screenshot.py` | Capture what it draws, straight off the framebuffer. |
| `tools/build_id_db.py` | Compile the IEEE and Bluetooth SIG vendor databases. |
| `tools/export_log.py` | Pull the log off the badge as CSV. |
| `tools/make_release.py` | Build the drag-and-drop install zip. |
| `tools/battery_report.py` | Turn recon's voltage log into a runtime figure. |

The SHARE view carries a QR to the repository and the URL written out, for
answering "what is that?" without handing over the badge.

The screenshot tool is worth knowing about: the framebuffer is plain RGBA8888
at 160x120 and `screen.raw` exposes it, so every image in this repo came off
the hardware. `--setup` runs Python after import with the app module bound to
`m`, which is how you reach a view that would otherwise need a button press.

`badge.update()` flips buffers, so a capture taken after it reads the buffer
you did *not* just draw into. The tool draws one final frame without updating.
