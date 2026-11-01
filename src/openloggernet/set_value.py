#!/usr/bin/env python3
"""Set Public variables on a running CR6 over PakBus; the counterpart to read_values.py.

    uv run set_value Public.DoRead=true
    uv run set_value Public.RailOn=true Public.RepeatRead=false
    uv run set_value Public.ProfileOverride=2 Public.ForcedBattV=11.8

`true`/`false` are written as CRBasic Booleans (-1/0), whole numbers as Long, anything else as
IEEE4. This is how a tester is driven: it acts on its `Do*` flags and clears them itself.
"""

import argparse
import sys

from .pakbus import PakBusError, PakBusLink, find_port
from .read_values import split_spec


def parse_assignment(text: str) -> tuple[str, str, float | int, str]:
    """ "Public.DoRead=true" -> ("Public", "DoRead", -1, "long")."""
    if "=" not in text:
        raise ValueError(f"{text!r} is not Table.Field=value")
    spec, raw = text.split("=", 1)
    table, field = split_spec(spec)
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return table, field, -1 if lowered == "true" else 0, "long"
    try:
        return table, field, int(raw), "long"
    except ValueError:
        return table, field, float(raw), "ieee4"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("assignments", nargs="+", metavar="TABLE.FIELD=VALUE")
    parser.add_argument("--port", help="serial port (default: autodetected CR6)")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--address", type=int, default=1, help="logger PakBus address (default: 1)")
    parser.add_argument("--my-address", type=int, default=4094, help="this host's PakBus address")
    args = parser.parse_args()

    try:
        writes = [parse_assignment(text) for text in args.assignments]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    port = args.port or find_port()
    if port is None:
        print("no CR6 serial port found", file=sys.stderr)
        return 1
    try:
        with PakBusLink(port, args.baudrate, node=args.address, my_node=args.my_address) as link:
            if not link.hello():
                print(f"the logger on {port} did not answer", file=sys.stderr)
                return 1
            for table, field, value, value_type in writes:
                link.set_values(table, field, value, value_type=value_type)
                print(f"{table}.{field} = {value}")
    except PakBusError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
