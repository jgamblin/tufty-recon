#!/usr/bin/env python3
"""
Capture what an installed app actually draws, as a PNG on the host.

The badge's framebuffer is plain RGBA8888 at 160x120 and `screen.raw` exposes
it, so this runs the app for a few frames on-device, dumps the buffer to the
badge's little internal filesystem, pulls it over, and saves an image. Much
faster than squinting at the panel.

    python3 tools/screenshot.py defcon34
    python3 tools/screenshot.py defcon34 --frames 90 --setup 'm.card.page = 1'
    python3 tools/screenshot.py recon --setup 'm.view = 2' -o flags.png

--setup runs after the app is imported and before the frames, with the app
module bound to `m`. It is how you reach a page that would otherwise need a
button press.
"""

import argparse
import os
import subprocess
import sys
import tempfile

W, H = 160, 120
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
MPR = os.environ.get("MPR", os.path.join(ROOT, ".venv", "bin", "mpremote"))

# Runs on the badge. `run` is normally the firmware's infinite app loop; it is
# stubbed so importing the app hands back its update() instead of blocking.
DEVICE_SCRIPT = """
import badgeware, builtins, sys, os
_cap = []
class _R:
    result = None
builtins.run = lambda u: (_cap.append(u), _R())[1]

p = "/system/apps/{app}"
sys.path.insert(0, p)
os.chdir(p)
m = __import__(p)
u = _cap[0]
{setup}
for _ in range({frames}):
    badge.poll()
    u()
    badge.update()

# One more frame with no update() after it. update() flips buffers, so calling
# it last would leave screen.raw pointing at the buffer we did not just draw.
badge.poll()
u()

with open("/shot.raw", "wb") as f:
    f.write(screen.raw)
print("SHOT_OK")
"""


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--frames", type=int, default=30,
                    help="frames to run before capturing (default 30)")
    ap.add_argument("--setup", default="",
                    help="python run after import, app module bound to `m`")
    ap.add_argument("-o", "--out", help="output PNG (default shots/<app>.png)")
    args = ap.parse_args()

    out = args.out or os.path.join(ROOT, "shots", args.app + ".png")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    script = DEVICE_SCRIPT.format(
        app=args.app,
        frames=args.frames,
        setup=args.setup,
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "capture.py")
        with open(path, "w") as f:
            f.write(script)

        p = port()
        res = subprocess.run([MPR, "connect", p, "run", path],
                             capture_output=True, text=True)
        if "SHOT_OK" not in res.stdout:
            sys.stderr.write(res.stdout + res.stderr)
            sys.exit("capture failed")

        raw = os.path.join(tmp, "shot.raw")
        subprocess.run([MPR, "connect", p, "fs", "cp", ":/shot.raw", raw],
                       check=True, capture_output=True)
        data = open(raw, "rb").read()
        subprocess.run([MPR, "connect", p, "fs", "rm", ":/shot.raw"],
                       capture_output=True)

    expected = W * H * 4
    if len(data) != expected:
        sys.exit("got %d bytes, expected %d" % (len(data), expected))

    from PIL import Image
    img = Image.frombytes("RGBA", (W, H), data)
    # The panel is 320x240 showing a 160x120 buffer, so doubling is what the
    # eye actually sees. Nearest keeps the pixel art crisp.
    img.resize((W * 2, H * 2), Image.NEAREST).save(out)
    print("wrote %s" % os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
