"""
On-device smoke test. Runs on the badge, not the host.

Imports the app and drives its update() for a fixed number of frames, so a
typo or a bad API call shows up as a traceback here instead of as a crash
screen at a conference.

Apps end with `run(update)`, which normally never returns. `run` is a global the
firmware injects at launch, so this stubs it with a bounded loop before the
import happens.

    .venv/bin/mpremote connect $PORT run tools/smoke.py
"""

import badgeware  # noqa: F401  (installs screen/badge/color/... as globals)

import builtins
import gc
import os
import sys
import time

FRAMES = 40

APPS = ("recon",)


class _Result:
    result = None


# Filled in by _bounded_run so import cost is reported separately from the
# frame loop. badge.update() blocks on vsync at ~60fps, so anything at 59 is
# waiting on the panel rather than on drawing.
frame_ms = 0


def _bounded_run(update):
    global frame_ms
    badge.poll()
    start = time.ticks_ms()
    for _ in range(FRAMES):
        badge.poll()
        update()
        badge.update()
    frame_ms = time.ticks_diff(time.ticks_ms(), start)
    return _Result()


def check(name):
    path = "/system/apps/" + name
    gc.collect()
    before = gc.mem_free()
    start = time.ticks_ms()

    builtins.run = _bounded_run
    sys.path.insert(0, path)
    cwd = os.getcwd()
    os.chdir(path)
    try:
        mod = __import__(path)
        getattr(mod, "on_exit", lambda: None)()
        total = time.ticks_diff(time.ticks_ms(), start)
        gc.collect()
        print("  ok    %-14s %2d fps   %4d ms import   %d bytes" % (
            name, FRAMES * 1000 // max(1, frame_ms), total - frame_ms,
            before - gc.mem_free()))
        return True
    except Exception as e:
        print("  FAIL  %s" % name)
        sys.print_exception(e)
        return False
    finally:
        os.chdir(cwd)
        if path in sys.path:
            sys.path.remove(path)
        # Drop the app's modules so the next import is a fresh one.
        for key in list(sys.modules):
            if key.startswith("/system/apps/") or key in ("conbadge",):
                del sys.modules[key]
        gc.collect()


print("smoke test, %d frames per app" % FRAMES)
failed = [name for name in APPS if not check(name)]
print()
print("FAILED: " + ", ".join(failed) if failed else "all apps ok")
