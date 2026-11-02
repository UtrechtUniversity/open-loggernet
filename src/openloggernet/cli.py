"""`oln`: one command line for a Campbell logger on a USB port.

    oln ports                                  # attached loggers
    oln --model CR300 info                     # OS, serial, program, compile result
    oln --port COM5 clock --set-utc
    oln tables
    oln collect Table1 --all --format toa5 -o Table1.dat
    oln get Public.PTemp Status.Battery
    oln set Public.DoRead=true

A logger is chosen with `--port` or `--model`; with neither, the only attached logger is used.
`--json` prints one JSON document per command (JSON Lines for streams), which is what the VS Code
extension reads; everything else is meant for people.
"""

import argparse
import datetime
import io
import json
import sys
import time
from contextlib import contextmanager

from . import pakbus, records
from .pakbus import VALUE_TYPES, PakBusError, PakBusLink
from .read_values import split_spec
from .set_value import parse_assignment


class CliError(Exception):
    """A failure to report as one line and exit status 1."""


def emit(args, data, text: str | None = None) -> None:
    """Print `data` as JSON under `--json`, otherwise `text` (or `data` itself)."""
    if args.json:
        print(json.dumps(data, default=str))
    else:
        print(text if text is not None else data)


def choose_port(args) -> str:
    if args.port:
        return args.port
    loggers = pakbus.list_loggers()
    if args.model:
        port = pakbus.find_port(args.model)
        if port is None:
            found = ", ".join(f"{p.model} on {p.device}" for p in loggers) or "none"
            raise CliError(f"no {args.model.upper()} attached (found: {found})")
        return port
    if len(loggers) > 1:
        found = ", ".join(f"{p.model} on {p.device}" for p in loggers)
        raise CliError(f"{len(loggers)} loggers attached ({found}); choose one with --port or --model")
    port = pakbus.find_port()
    if port is None:
        raise CliError("no Campbell logger serial port found")
    return port


@contextmanager
def connect(args):
    port = choose_port(args)
    with PakBusLink(port, node=args.address, my_node=args.my_address, debug=args.debug) as link:
        if not link.hello():
            raise CliError(f"the logger on {port} did not answer a hello; is LoggerNet connected to it?")
        yield link


# --- commands --------------------------------------------------------------


def cmd_ports(args) -> int:
    loggers = pakbus.list_loggers()
    emit(
        args,
        [{"port": p.device, "model": p.model, "description": p.description} for p in loggers],
        "\n".join(f"{p.device:8s} {p.model or '?'}" for p in loggers) or "no loggers attached",
    )
    return 0


def program_info(link: PakBusLink) -> dict:
    stats = link.get_program_stats()
    if stats.get("resp_code"):
        raise CliError(f"get programming statistics failed, response code {stats['resp_code']}")
    seconds, nanos = stats.pop("compile_time")
    stats["compile_time"] = pakbus.EPOCH + datetime.timedelta(seconds=seconds, microseconds=nanos / 1000)
    stats["model"] = link.model
    stats["port"] = link._port_name
    return stats


def cmd_info(args) -> int:
    with connect(args) as link:
        info = program_info(link)
    lines = [
        f"model           {info['model'] or '?'} on {info['port']}",
        f"serial number   {info['serial_number']}",
        f"OS version      {info['os_version']} (signature {info['os_signature']})",
        f"program         {info['program_name'] or '<none>'} (signature {info['program_signature']})",
        f"power-up        {info['power_up_program'] or '<none>'}",
        f"compiled        {info['compile_time']:%Y-%m-%d %H:%M:%S}",
        f"compile result  {info['compile_result'].strip()}",
    ]
    emit(args, info, "\n".join(lines))
    return 0


def measure_clock(link: PakBusLink) -> tuple[datetime.datetime, datetime.datetime, float]:
    """(logger time, host UTC at the midpoint of the round trip, round trip seconds)."""
    before = time.time()
    logger = link.clock()
    after = time.time()
    return logger, datetime.datetime.fromtimestamp((before + after) / 2, datetime.UTC), after - before


def cmd_clock(args) -> int:
    with connect(args) as link:
        logger, host, trip = measure_clock(link)
        offset = (logger - host).total_seconds()
        result: dict = {"logger": logger, "host": host, "offset": offset, "round_trip": trip}
        if args.set_utc or args.set:
            target = datetime.datetime.fromisoformat(args.set) if args.set else host
            if target.tzinfo is None:
                target = target.replace(tzinfo=datetime.UTC)
            # The logger applies the adjustment to its own clock, so the transport delay is what
            # limits the result; it is measured again rather than assumed.
            adjust = (target - logger).total_seconds() if args.set else -offset
            whole = int(adjust)
            link.clock(whole, int(round((adjust - whole) * 1e9)))
            logger, host, trip = measure_clock(link)
            result.update(adjusted=adjust, logger_after=logger, offset_after=(logger - host).total_seconds())
    text = f"logger    {result['logger']:%Y-%m-%d %H:%M:%S.%f}\nhost UTC  {result['host']:%Y-%m-%d %H:%M:%S.%f}\n"
    text += f"offset    {result['offset']:+.3f} s (round trip {result['round_trip'] * 1000:.0f} ms)"
    if "adjusted" in result:
        text += f"\nadjusted  {result['adjusted']:+.3f} s\noffset now {result['offset_after']:+.3f} s"
    emit(args, result, text)
    if args.set_utc and abs(result["offset_after"]) > args.max_drift:
        raise CliError(f"offset still above {args.max_drift} s after the set")
    return 0


def table_summary(table: pakbus.TableDef) -> dict:
    return {
        "number": table.number,
        "name": table.name,
        "size": table.size,
        "record_size": table.record_size,
        "interval": table.interval_sec or None,
        "fields": [
            {"name": f.name, "type": f.type_code, "count": f.count, "units": f.units, "processing": f.processing}
            for f in table.fields
        ],
    }


def cmd_tables(args) -> int:
    with connect(args) as link:
        tables = link.table_definitions()
    lines = [f"{'#':>3s}  {'table':22s} {'record':>7s} {'interval':>9s} {'size':>9s}  fields"]
    for t in tables:
        interval = f"{t.interval_sec}s" if t.interval_sec else "event"
        lines.append(f"{t.number:3d}  {t.name:22s} {t.record_size:6d}B {interval:>9s} {t.size:9d}  {len(t.fields)}")
    emit(args, [table_summary(t) for t in tables], "\n".join(lines))
    return 0


def find_table(tables: list, name: str) -> pakbus.TableDef:
    table = next((t for t in tables if t.name.lower() == name.lower()), None)
    if table is None:
        raise CliError(f"no table named {name!r}; the logger has {', '.join(t.name for t in tables)}")
    return table


def cmd_collect(args) -> int:
    with connect(args) as link:
        tables = link.table_definitions()
        table = find_table(tables, args.table)
        info = program_info(link)
        station = link.get_values("Status", "StationName")[0] if args.format == "toa5" else ""
        started = time.monotonic()
        count = table.size if args.all else args.count
        collected = link.collect_recent(table, count)
        elapsed = time.monotonic() - started
    environment = {
        "station": station,
        "model": info["model"] or "",
        "serial": info["serial_number"],
        "os": info["os_version"],
        "program": info["program_name"],
        "program_signature": info["program_signature"],
    }
    rate = len(collected) / elapsed if elapsed else 0.0
    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as out:
            records.write_records(out, table, collected, args.format, environment)
        summary = {"table": table.name, "records": len(collected), "seconds": elapsed, "file": args.output}
        emit(args, summary, f"{len(collected)} record(s) from {table.name} -> {args.output} ({rate:.0f} records/s)")
    else:
        records.write_records(sys.stdout, table, collected, "jsonl" if args.json else args.format, environment)
        print(f"{len(collected)} record(s) in {elapsed:.2f} s ({rate:.0f} records/s)", file=sys.stderr)
    return 0


def cmd_get(args) -> int:
    results = []
    with connect(args) as link:
        for spec in args.specs:
            table, field = split_spec(spec)
            try:
                values = link.get_values(table, field, value_type=args.type, swath=args.swath)
                results.append({"name": spec, "values": values})
            except PakBusError as exc:
                results.append({"name": spec, "error": str(exc)})
    width = max(len(s) for s in args.specs)
    lines = [
        f"{r['name']:<{width}}  " + (", ".join(map(str, r["values"])) if "values" in r else f"!! {r['error']}")
        for r in results
    ]
    emit(args, results, "\n".join(lines))
    return 1 if any("error" in r for r in results) else 0


def cmd_set(args) -> int:
    writes = [parse_assignment(text) for text in args.assignments]
    with connect(args) as link:
        for table, field, value, value_type in writes:
            link.set_values(table, field, value, value_type=value_type)
    emit(
        args,
        [{"name": f"{t}.{f}", "value": v, "type": vt} for t, f, v, vt in writes],
        "\n".join(f"{t}.{f} = {v}" for t, f, v, _ in writes),
    )
    return 0


# --- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oln", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyACM0")
    parser.add_argument("--model", help="pick the attached logger of this model, e.g. CR300")
    parser.add_argument("--address", type=int, default=1, help="logger PakBus address (default: 1)")
    parser.add_argument("--my-address", type=int, default=4094, help="this host's PakBus address (default: 4094)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--debug", action="store_true", help="dump every PakBus packet")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("ports", help="list attached loggers").set_defaults(func=cmd_ports)
    sub.add_parser("info", help="OS version, serial number, program and compile result").set_defaults(func=cmd_info)

    p = sub.add_parser("clock", help="read the logger clock, or set it")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--set-utc", action="store_true", help="set the logger to the host's UTC time")
    group.add_argument("--set", metavar="ISO_TIME", help="set the logger to this time (UTC unless it has a zone)")
    p.add_argument("--max-drift", type=float, default=0.5, help="seconds of offset to accept after --set-utc")
    p.set_defaults(func=cmd_clock)

    sub.add_parser("tables", help="the logger's data tables").set_defaults(func=cmd_tables)

    p = sub.add_parser("collect", help="read stored records from a table")
    p.add_argument("table")
    amount = p.add_mutually_exclusive_group()
    amount.add_argument("--count", type=int, default=10, help="most recent N records (default 10)")
    amount.add_argument("--all", action="store_true", help="every record the table holds")
    p.add_argument("--format", choices=records.FORMATS, default="csv")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("get", help="read values from Public, Status or a data table")
    p.add_argument("specs", nargs="+", metavar="TABLE.FIELD")
    p.add_argument("--type", default="asciiz", choices=sorted(VALUE_TYPES))
    p.add_argument("--swath", type=int, default=1, help="consecutive array elements (numeric types only)")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="write values into the running program")
    p.add_argument("assignments", nargs="+", metavar="TABLE.FIELD=VALUE")
    p.set_defaults(func=cmd_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (CliError, PakBusError, ValueError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        print(f"oln: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
