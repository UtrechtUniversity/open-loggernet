"""Send a new operating system to a logger over an ordinary PakBus connection.

This is what Device Configuration Utility 2.34's "Manage OS -> Update" did to the CR300 (serial
33590, OS 11.02 -> 11.03) on 2026-11-02, from a USB capture (docs/pakbus.md, "Operating system"):

1. note the running and the power-up program (Get Prog Stats);
2. File Control 8 with an empty file name: the program stops; the logger resets, hold-off 26 s;
3. File Download of the `.obj` image to `CPU:` under its own name, 976 bytes per fragment;
4. File Control 1 on the image: the logger loads it as its operating system, hold-off 40 s, and
   comes back up on the new OS with the image gone from `CPU:`;
5. File Control 2 on the old power-up program and 6 on the old running program, which put both
   back; settings and the files on `CPU:` survived.

Campbell's instructions call this "more likely (although not guaranteed)" to keep programs and
settings than the boot-code method, which resets everything and is not implemented here.
"""

import time
from collections.abc import Callable
from pathlib import Path

from . import flash
from .pakbus import (
    FILECTL_COMPILE_RUN_KEEP_DATA,
    FILECTL_COMPILE_RUN_POWERUP,
    FILECTL_MARK_POWERUP,
    FILECTL_STOP_DELETE_DATA,
    PakBusError,
    PakBusLink,
)

# The fragment size DevConfig used for the image; each took the CR300 about 93 ms to write.
OS_SWATH = 976


def _wait_and_reconnect(link: PakBusLink, hold_off: int, port: str | None, say: Callable[[str], None]) -> None:
    if hold_off:
        say(f"logger asked for {hold_off} s")
        time.sleep(hold_off)
    if flash.reconnect(link, port, timeout=180, model=link.model) is None:
        raise PakBusError("the logger did not come back")


def send_os(
    link: PakBusLink,
    image: Path,
    *,
    port: str | None = None,
    restore: bool = True,
    say: Callable[[str], None] = print,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Load `image` as the logger's operating system. Returns {"before": ..., "after": ...} OS versions.

    `port` pins the serial port to reopen after each reset; otherwise the logger is found again by
    its model.
    """
    data = image.read_bytes()
    if not image.suffix.lower() == ".obj":
        raise ValueError(f"{image.name}: an operating system image is a .obj file")
    target = f"CPU:{image.name}"

    stats = link.get_program_stats()
    before = stats.get("os_version", "")
    running = stats.get("program_name", "")
    power_up = stats.get("power_up_program", "")
    say(f"OS {before}; running {running or '<none>'}, power-up {power_up or '<none>'}")

    say("stopping the program")
    code, hold_off = link.file_control("", FILECTL_STOP_DELETE_DATA)
    if code != 0:
        raise PakBusError(f"stop: response code {code}")
    _wait_and_reconnect(link, hold_off, port, say)

    after = before
    try:
        say(f"sending {image.name} ({len(data)} bytes)")
        code = link.file_download(target, data, swath=OS_SWATH, progress=progress, timeout=30)
        if code != 0:
            raise PakBusError(f"{target}: download refused, response code {code}")

        say("starting the new operating system")
        code, hold_off = link.file_control(target, FILECTL_COMPILE_RUN_POWERUP)
        if code == 13:
            # Seen when the CR300 (OS 11.03) was sent the 11.03 image it already ran, 2026-11-02;
            # the image was then gone from CPU:. That it means "same OS" is inferred.
            raise PakBusError(
                f"{target}: the logger refused the image (response code 13), as it did the OS it already runs"
            )
        if code != 0:
            raise PakBusError(f"{target}: the logger would not load it, response code {code}")
        _wait_and_reconnect(link, hold_off, port, say)
        after = link.get_program_stats().get("os_version", "")
        say(f"OS now {after}")
    finally:
        # The programs are put back whatever happened above: step 2 left the logger without one.
        if restore:
            _restore(link, running, power_up, port, say)
    return {"before": before, "after": after, "program": link.get_program_stats().get("program_name", "")}


def _restore(link: PakBusLink, running: str, power_up: str, port: str | None, say: Callable[[str], None]) -> None:
    if power_up:
        say(f"restoring power-up program {power_up}")
        link.file_control(power_up, FILECTL_MARK_POWERUP)
    if running:
        say(f"restarting {running}")
        code, hold_off = link.file_control(running, FILECTL_COMPILE_RUN_KEEP_DATA)
        if code == 0 and hold_off:
            _wait_and_reconnect(link, hold_off, port, say)
