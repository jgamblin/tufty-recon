# Boot straight into recon.
#
# On a bag for a week, every power loss otherwise leaves the badge sitting at a
# launcher until someone notices. This starts scanning unattended instead.
#
# HOME exits an app by resetting the board, so without an escape hatch that
# would land right back in recon and the launcher would be unreachable, taking
# Mass Storage with it. Holding C during the countdown below opens the
# launcher instead.
#
# If this file is ever broken, double-tapping RESET still exposes the badge as
# a USB drive: that is firmware, not this script, so recovery never depends on
# anything here.

import time

LAUNCHER = "/system/apps/menu"
DEFAULT_APP = "/system/apps/recon"
HOLD_MS = 1800


def _countdown():
    """Returns True if the launcher was asked for."""
    screen.pen = color.rgb(10, 14, 22)
    screen.clear()
    start = time.ticks_ms()
    while True:
        badge.poll()
        if badge.held(BUTTON_C) or badge.pressed(BUTTON_C):
            return True
        left = HOLD_MS - time.ticks_diff(time.ticks_ms(), start)
        if left <= 0:
            return False

        screen.pen = color.rgb(10, 14, 22)
        screen.clear()
        screen.font = rom_font.nope
        screen.pen = color.rgb(46, 200, 224)
        s = "RECON"
        screen.text(s, (160 - screen.measure_text(s)[0]) / 2, 40)
        screen.font = rom_font.winds
        screen.pen = color.rgb(226, 238, 248, 130)
        s = "hold C for launcher"
        screen.text(s, (160 - screen.measure_text(s)[0]) / 2, 62)
        screen.pen = color.rgb(46, 200, 224, 90)
        screen.rectangle(8, 78, int(144 * left / HOLD_MS), 4)
        badge.update()


badge.poll()

if _countdown():
    app_to_launch = launch(LAUNCHER)
    if app_to_launch is not None:
        while badge.pressed() or badge.held() or badge.released():
            badge.poll()
        launch(app_to_launch)
else:
    launch(DEFAULT_APP)

reset()
