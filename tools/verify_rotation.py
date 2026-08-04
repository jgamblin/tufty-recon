"""
Check the rotation and rogue-AP rules on the badge, against payloads shaped
like the ones that fooled an earlier version.

    tools/run.sh tools/verify_rotation.py
"""

import sys

PATH = "/system/apps/recon"
sys.path.insert(0, PATH)

import identify as ID  # noqa: E402

# Advertisement payloads, built the way a radio delivers them: length-prefixed
# AD structures.
SWIFT = bytes((0x06, 0xFF, 0x06, 0x00, 0x03, 0x00, 0x80))       # Microsoft CDP
FASTPAIR = bytes((0x05, 0x16, 0x2C, 0xFE, 0xAA, 0xBB))          # Google 0xFE2C
CONTINUITY = bytes((0x05, 0xFF, 0x4C, 0x00, 0x07, 0x19))        # Apple AirPods
NAMED = bytes((0x08, 0x09)) + b"Biscuit"                        # a plain name
TILE_SVC = bytes((0x03, 0x03, 0xED, 0xFE))                      # Tile service
BARE = bytes((0x02, 0x01, 0x06))                                # flags only

CASES = (
    # payload,    kind,          expect rotating?, why
    (SWIFT,       ID.STATIC,     True,  "Swift Pair rotates"),
    (FASTPAIR,    ID.STATIC,     True,  "Fast Pair rotates"),
    (CONTINUITY,  ID.STATIC,     True,  "Continuity rotates"),
    (BARE,        ID.STATIC,     True,  "no identity at all"),
    (NAMED,       ID.STATIC,     False, "a name is identity, once"),
    (TILE_SVC,    ID.STATIC,     False, "a known service is identity"),
    (SWIFT,       ID.PUBLIC,     False, "public is burned in, never rotates"),
    (CONTINUITY,  ID.PUBLIC,     False, "public is burned in, never rotates"),
    (NAMED,       ID.RESOLVABLE, True,  "resolvable always rotates"),
)

print("rotation rules")
bad = 0
for payload, kind, want, why in CASES:
    adv = ID.parse_adv(payload)
    got = ID.is_rotating(kind, adv)
    ok = got == want
    bad += not ok
    print("  %-4s kind=%d rotating=%-5s  %s" % (
        "ok" if ok else "FAIL", kind, got, why))

# The share rule, which is what catches a scheme nobody has tabulated yet.
print()
print("shared-label rule (cap %d)" % 8)
shares = {}


def discriminates(label, cap=8):
    n = shares.get(label)
    if n is None:
        shares[label] = 1
        return True
    if n > cap:
        return False
    shares[label] = n + 1
    return n + 1 <= cap


logged = sum(1 for _ in range(66) if discriminates("T-Dongle Biscuit"))
print("  %-4s 66 addresses sharing one label -> %d logged" % (
    "ok" if logged == 8 else "FAIL", logged))
bad += logged != 8

solo = sum(1 for i in range(66) if discriminates("device-%d" % i))
print("  %-4s 66 addresses with distinct labels -> %d logged" % (
    "ok" if solo == 66 else "FAIL", solo))
bad += solo != 66

# The karma heuristic.
print()
print("rogue access points")
KARMA = (
    (b"\x5e\x94\xf2\x5b\xa1\x78", True,  "spoofed MAC, WEP"),
    (b"\x00\x23\x5e\x94\x61\x40", False, "real OUI, just old"),
)
for mac, want, why in KARMA:
    got = ID.is_locally_administered(mac)
    ok = got == want
    bad += not ok
    print("  %-4s locally administered=%-5s  %s" % (
        "ok" if ok else "FAIL", got, why))

print()
print("FAILURES: %d" % bad if bad else "all rules behave as intended")
