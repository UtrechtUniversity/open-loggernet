#!/usr/bin/env python3
"""Compile a CRBasic program and send it to a CR6 over the USB (PakBus) port.

    uv run flash ../crbasic/PowerProfile_tester.CR6
    uv run flash ../crbasic/Main.CR6 --monitor 60

Replaces the manual "flash it with Device Configuration Utility / LoggerNet"
step: it compiles with the validated CR6Comp.exe, writes the file to CPU:,
tells the logger to compile and run it, and reads the compile results back
out of the logger.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

import serial

from . import cr6comp
from .pakbus import FILECTL_COMPILE_RUN_POWERUP, PakBusError, PakBusLink, TextFilter, find_port


def compile_program(program: Path) -> bool:
    """Compile with CR6Comp.exe. Returns True if the compiler reported success."""
    ok, output = cr6comp.compile_program(program)
    if output:
        print(output)
    return ok


INCLUDE_RE = re.compile(r"""["'](CPU:|USR:|CRD:|USB:)([^"']+)["']""")


def included_files(program: Path) -> list[str]:
    """Logger paths of the library files the program includes, in source order.

    Programs reference their libraries as IncludeSection("Functions",
    "CPU:SomeLib.crb"); those files have to be on the logger before it can
    compile the program.
    """
    found: list[str] = []
    for line in program.read_text(errors="replace").splitlines():
        if "include" not in line.lower():
            continue
        for drive, name in INCLUDE_RE.findall(line):
            target = f"{drive}{name}"
            if target not in found:
                found.append(target)
    return found


def reconnect(link: PakBusLink, port_override: str | None, timeout: float = 90) -> str | None:
    """Wait for the logger's USB port to come back and reopen it.

    The CR6 disconnects and re-enumerates its USB device while it compiles a
    newly sent program, and the device node it comes back on can differ from
    the one it left (ttyACM1 -> ttyACM0), so the port is looked up again.
    """
    link.close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = port_override or find_port()
        if path and os.path.exists(path):
            try:
                link.open(path)
            except serial.SerialException:
                time.sleep(0.5)
                continue
            if link.hello():
                return path
            link.close()
        time.sleep(0.5)
    return None


def send_file(link: PakBusLink, source: Path, target: str) -> bool:
    data = source.read_bytes()

    def progress(sent: int, total: int) -> None:
        print(f"\r  {target}: {sent}/{total} bytes", end="", flush=True)

    code = link.file_download(target, data, progress=progress)
    print()
    if code != 0:
        print(f"  {target}: download failed, response code {code}", file=sys.stderr)
        return False
    return True


def describe(stats: dict) -> str:
    if stats.get("resp_code"):
        return f"(get programming statistics failed, response code {stats['resp_code']})"
    return f"OS {stats['os_version']}, serial {stats['serial_number']}, running {stats['program_name'] or '<none>'}"


def monitor(link: PakBusLink, seconds: float, send: bytes = b"") -> None:
    """Print whatever the running program writes to ComUSB.

    The tester programs in crbasic/ are menu driven and stay silent until they
    receive a keystroke, so `send` is written first when given.
    """
    if send:
        link.port.write(send)
        link.port.flush()
    print(f"--- ComUSB output for {seconds:g} s ---")
    deadline = time.monotonic() + seconds
    pending = bytearray()
    text = TextFilter()
    while time.monotonic() < deadline:
        chunk = link.port.read(256)
        if not chunk:
            continue
        pending.extend(text.feed(chunk))
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            print(line.decode("ascii", "replace").rstrip("\r"))
    if pending:
        print(pending.decode("ascii", "replace").rstrip())
    print("--- end of ComUSB output ---")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("program", type=Path, help="CRBasic program to send (.CR6)")
    parser.add_argument("--port", help="serial port (default: autodetected CR6)")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--address", type=int, default=1, help="PakBus address of the logger (default: 1)")
    parser.add_argument(
        "--my-address", type=int, default=4094, help="PakBus address to use for this host (default: 4094)"
    )
    parser.add_argument("--drive", default="CPU:", help="target drive (default: CPU:)")
    parser.add_argument("--no-compile", action="store_true", help="skip the CR6Comp.exe check and send the file as-is")
    parser.add_argument("--no-includes", action="store_true", help="do not send the library files the program includes")
    parser.add_argument(
        "--no-run", action="store_true", help="write the file to the logger but do not compile and run it"
    )
    parser.add_argument(
        "--monitor", type=float, metavar="SECONDS", default=0, help="after flashing, print ComUSB output for this long"
    )
    parser.add_argument(
        "--send",
        metavar="TEXT",
        default="",
        help=r"text to write to ComUSB before monitoring, e.g. '\r' "
        "to make a menu-driven tester show its menu",
    )
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for compile results (default: 60)")
    parser.add_argument("--debug", action="store_true", help="dump PakBus packets")
    args = parser.parse_args()

    program = args.program.resolve()
    if not program.is_file():
        print(f"no such program: {program}", file=sys.stderr)
        return 1

    if not args.no_compile:
        if not compile_program(program):
            print("compilation failed - nothing was sent to the logger", file=sys.stderr)
            return 1

    port = args.port or find_port()
    if not port:
        print("no CR6 serial port found (is the logger attached to WSL?)", file=sys.stderr)
        return 1

    target = f"{args.drive}{program.name}"
    includes = [] if args.no_includes else included_files(program)
    print(f"{program.name} -> {target} via {port}")

    try:
        with PakBusLink(port, args.baudrate, node=args.address, my_node=args.my_address, debug=args.debug) as link:
            if not link.hello():
                print("logger did not answer the hello; trying the transfer anyway")
            print(describe(link.get_program_stats()))

            for include in includes:
                source = program.parent / Path(include.split(":", 1)[1]).name
                if not source.is_file():
                    print(f"  {include}: no local copy next to the program, not sent")
                    continue
                if not send_file(link, source, include):
                    return 1
            if not send_file(link, program, target):
                return 1

            if args.no_run:
                return 0

            code, hold_off = link.file_control(target, FILECTL_COMPILE_RUN_POWERUP)
            if code != 0:
                print(f"file control failed, response code {code}", file=sys.stderr)
                return 1
            print(f"compiling and running {target}" + (f" (logger asked for {hold_off} s)" if hold_off else ""))
            time.sleep(max(hold_off, 2))

            path = reconnect(link, args.port)
            if path is None:
                print("the logger's USB port did not come back after the compile", file=sys.stderr)
                return 1
            if path != port:
                print(f"logger came back on {path}")

            deadline = time.monotonic() + args.timeout
            stats: dict = {}
            while time.monotonic() < deadline:
                try:
                    stats = link.get_program_stats()
                except PakBusError:
                    time.sleep(1)
                    continue
                if stats.get("resp_code") == 0 and stats.get("program_name", "").endswith(program.name):
                    break
                time.sleep(1)

            if not stats or stats.get("resp_code"):
                print("could not read compile results back from the logger", file=sys.stderr)
                return 1

            print(f"running program: {stats['program_name']}")
            print(f"compile results: {stats['compile_result'].strip() or '<empty>'}")
            failed = "error" in stats["compile_result"].lower()

            if args.monitor:
                monitor(link, args.monitor, args.send.encode().decode("unicode_escape").encode("ascii"))

            return 1 if failed else 0
    except PakBusError as exc:
        print(f"PakBus error: {exc}", file=sys.stderr)
        return 1
    except serial.SerialException as exc:
        print(f"serial error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
