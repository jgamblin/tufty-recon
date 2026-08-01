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
devices. Every view now holds under 10ms a frame up to 1800 devices, against a
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

Fixed-width binary records on the LittleFS root: 48 bytes per access point, 40
bytes per device.

**It cannot fill the disk.** The root is 1024KB and shared with every other
app's state, so recon takes a 560KB budget and stops there, never letting free
space fall below a 64KB reserve. Record caps derive from that budget rather
than being guessed: 4247 access points and 9557 devices, worst case 559KB.

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

The exception is what real data turned up. Apple's Find My beacons derive their
address from a rotating key but advertise it as *static random*, so by the bits
they look permanent. One two-hour outing logged 356 distinct Find My addresses
in 96 minutes, 116 inside a single 10-minute window: roughly 55 real devices.
Rotation is therefore decided from the advertisement payload as well as the
address bits.

## Tooling

| Tool | What it does |
| --- | --- |
| `tools/deploy.sh` | Push recon to a connected badge via mass storage. |
| `tools/smoke.py` | Run the app for 40 frames on-device; report failures, fps, memory. |
| `tools/stress_recon.py` | Drive it at conference scale with synthetic devices. |
| `tools/screenshot.py` | Capture what it draws, straight off the framebuffer. |
| `tools/build_id_db.py` | Compile the IEEE and Bluetooth SIG vendor databases. |
| `tools/export_log.py` | Pull the log off the badge as CSV. |
| `tools/make_release.py` | Build the drag-and-drop install zip. |

The screenshot tool is worth knowing about: the framebuffer is plain RGBA8888
at 160x120 and `screen.raw` exposes it, so every image in this repo came off
the hardware. `--setup` runs Python after import with the app module bound to
`m`, which is how you reach a view that would otherwise need a button press.

`badge.update()` flips buffers, so a capture taken after it reads the buffer
you did *not* just draw into. The tool draws one final frame without updating.
