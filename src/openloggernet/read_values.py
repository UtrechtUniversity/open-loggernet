#!/usr/bin/env python3
"""Read Public variables, data table fields and Status fields off a running CR6.

    uv run read_values Public.PT1000_Temp_C Public.AmbientForDecision
    uv run read_values Status.SkippedScan Status.WatchdogErrors
    uv run read_values --type ieee4 --swath 9 Public.PowerRTime
    uv run read_values --watch 30 Public.PT1000_Temp_C Public.PT1000Ok

This is the counterpart to flash.py: flash.py puts a program on the logger and shows
what it writes to ComUSB, and this shows the state of a program that writes nothing.
A library exercised only from a slow sequence -- which is most of them -- produces no
serial output at all, so without this its variables are unobservable.

--watch polls until interrupted and prints a line only when a value changes, which is
how you wait out a gated slow sequence without guessing when it fired.
"""

import argparse
import io
import sys
import time

from .pakbus import VALUE_TYPES, PakBusError, PakBusLink, find_port


def split_spec(spec: str) -> tuple[str, str]:
    """ "Public.PTemp" -> ("Public", "PTemp"). Array indices keep their parentheses."""
    if "." not in spec:
        raise ValueError(f"{spec!r} is not Table.Field -- e.g. Public.PTemp, Status.SkippedScan")
    table, field = spec.split(".", 1)
    if not table or not field:
        raise ValueError(f"{spec!r} is not Table.Field -- e.g. Public.PTemp")
    return table, field


def format_values(values: list) -> str:
    return ", ".join(f"{v:.6g}" if isinstance(v, float) else str(v) for v in values)


def read_one(link: PakBusLink, spec: str, value_type: str, swath: int, timeout: float) -> str:
    """Return a printable reading, or the error text, so one bad spec cannot end a watch."""
    table, field = split_spec(spec)
    try:
        return format_values(link.get_values(table, field, value_type=value_type, swath=swath, timeout=timeout))
    except (PakBusError, ValueError) as exc:
        return f"!! {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "specs",
        nargs="+",
        metavar="TABLE.FIELD",
        help="e.g. Public.PT1000_Temp_C, Status.SkippedScan, StationStatus.PTemp",
    )
    parser.add_argument(
        "--type",
        default="asciiz",
        choices=sorted(VALUE_TYPES),
        help="representation to ask the logger for (default: asciiz, which "
        "reads any type but returns one value and ignores --swath)",
    )
    parser.add_argument("--swath", type=int, default=1, help="consecutive array elements to read (default: 1)")
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        default=0,
        help="poll every SECONDS and print only when a value changes",
    )
    parser.add_argument("--port", help="serial port (default: autodetected CR6)")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--address", type=int, default=1, help="logger PakBus address (default: 1)")
    parser.add_argument("--my-address", type=int, default=4094, help="this host's PakBus address (default: 4094)")
    parser.add_argument("--timeout", type=float, default=10, help="per-transaction timeout in seconds (default: 10)")
    parser.add_argument("--debug", action="store_true", help="dump PakBus packets")
    args = parser.parse_args()

    # A watch is meant to be redirected to a log and left running, and Python block-buffers
    # stdout when it is not a tty -- without this, a backgrounded watch shows nothing until it
    # exits, which for an unbounded watch means never.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    port = args.port or find_port()
    if port is None:
        print("no CR6 serial port found; is the USB device attached to WSL (usbipd attach)?", file=sys.stderr)
        return 1

    for spec in args.specs:
        try:
            split_spec(spec)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2

    width = max(len(s) for s in args.specs)

    try:
        with PakBusLink(port, args.baudrate, node=args.address, my_node=args.my_address, debug=args.debug) as link:
            if not link.hello():
                print(f"the logger on {port} did not answer", file=sys.stderr)
                return 1

            if not args.watch:
                for spec in args.specs:
                    print(f"{spec:<{width}}  {read_one(link, spec, args.type, args.swath, args.timeout)}")
                return 0

            print(f"watching {len(args.specs)} field(s) every {args.watch:g} s; prints on change, Ctrl-C to stop")
            previous: dict[str, str] = {}
            while True:
                for spec in args.specs:
                    reading = read_one(link, spec, args.type, args.swath, args.timeout)
                    if previous.get(spec) != reading:
                        print(f"[{time.strftime('%H:%M:%S')}] {spec:<{width}}  {reading}")
                        previous[spec] = reading
                time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except PakBusError as exc:
        print(f"PakBus error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
