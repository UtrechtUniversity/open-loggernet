#!/usr/bin/env python3
"""Read stored data table records off a running logger (CR6, CR1000X, CR300 or CR350).

The counterpart to `read_values.py`, which sees only the newest value of a field. This reads the
tables themselves, which is what makes a run's full-resolution data survive: the serial trace
carries only what a program chose to print, and a reflash deletes the tables.

    uv run collect_data --list
    uv run collect_data DailyPower
    uv run collect_data Shunt_Data --count 60 --csv shunt.csv
    uv run collect_data TString_Data --count 5
"""

import argparse
import csv
import itertools
import sys

from .pakbus import PakBusLink, PakBusError, find_port


def flatten(name: str, value) -> list[tuple[str, object]]:
    if isinstance(value, list):
        return [(f"{name}({i})", v) for i, v in enumerate(value, 1)]
    return [(name, value)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("table", nargs="?", help="table name, e.g. DailyPower")
    parser.add_argument("--list", action="store_true", help="list the logger's tables and exit")
    parser.add_argument("--count", type=int, default=10, help="most recent N records (default 10)")
    parser.add_argument("--csv", help="write the records to this CSV file instead of printing")
    parser.add_argument("--port", help="serial port (default: autodetected logger)")
    args = parser.parse_args()

    port = args.port or find_port()
    if port is None:
        print("no Campbell logger serial port found; is it attached to this host or WSL?",
              file=sys.stderr)
        return 1

    with PakBusLink(port) as link:
        if not link.hello():
            print("the logger did not answer a hello", file=sys.stderr)
            return 1
        tables = link.table_definitions()

        if args.list or not args.table:
            limit = f", replies limited to {link.max_reply_frame} B" if link.max_reply_frame else ""
            print(f"{link.model or 'unknown model'} on {port}{limit}")
            print(f"{'#':>3s}  {'table':22s} {'rec':>5s} {'interval':>9s} {'size':>8s}  fields")
            for t in tables:
                interval = f"{t.interval_sec}s" if t.interval_sec else "event"
                print(f"{t.number:3d}  {t.name:22s} {t.record_size:4d}B {interval:>9s} "
                      f"{t.size:8d}  {len(t.fields)}")
            return 0

        table = next((t for t in tables if t.name.lower() == args.table.lower()), None)
        if table is None:
            print(f"no table named {args.table!r}; try --list", file=sys.stderr)
            return 1

        try:
            records = link.collect_recent(table, args.count)
        except PakBusError as exc:
            print(f"collect failed: {exc}", file=sys.stderr)
            return 1

    if not records:
        print(f"{table.name}: no records stored")
        return 0

    columns = ["record", "time"]
    for field in table.fields:
        columns.extend(n for n, _ in flatten(field.name, records[0][2][field.name]))

    rows = []
    for number, when, values in records:
        row = {"record": number, "time": when.isoformat(sep=" ", timespec="seconds")}
        for field in table.fields:
            row.update(flatten(field.name, values[field.name]))
        rows.append(row)

    if args.csv:
        with open(args.csv, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        print(f"{len(rows)} record(s) from {table.name} -> {args.csv}")
        return 0

    print(f"{table.name}: {len(rows)} record(s), "
          f"{'interval ' + str(table.interval_sec) + 's' if table.is_interval else 'event driven'}")
    wide = len(columns) > 12
    for row in rows:
        head = f"  #{row['record']} {row['time']}"
        if wide:
            print(head)
            body = [f"{c}={row[c]}" for c in columns[2:]]
            for line in itertools.batched(body, 6):
                print("      " + "  ".join(line))
        else:
            print(head + "  " + "  ".join(
                f"{c}={row[c]:.4g}" if isinstance(row[c], float) else f"{c}={row[c]}"
                for c in columns[2:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
