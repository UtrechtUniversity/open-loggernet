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
from pathlib import Path

import serial

from . import flash, os_update, pakbus, records
from . import settings as settings_lib
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
    try:
        link = PakBusLink(port, node=args.address, my_node=args.my_address, debug=args.debug)
    except serial.SerialException as exc:
        # Only one program can hold a COM port; LoggerNet holds it while a station is connected.
        raise CliError(f"cannot open {port} ({exc}); disconnect LoggerNet or any other program using it") from exc
    with link:
        if not link.hello():
            raise CliError(f"the logger on {port} did not answer a hello")
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
    stats["model"] = stats.get("model") or link.model  # the logger says, on newer OSes
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
        "interval": table.interval.total_seconds() if table.is_interval else None,
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
        interval = f"{t.interval.total_seconds():g}s" if t.is_interval else "event"
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


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return str(size)


def cmd_files_ls(args) -> int:
    with connect(args) as link:
        entries = link.directory()
    rows: list[dict] = []
    for e in entries:
        flags = [pakbus.FILE_ATTRIBUTES.get(a, f"attribute {a}") for a in e.attributes]
        if 5 in e.attributes:
            flags = [f for f in flags if f != "running"]
        rows.append({"name": e.name, "size": e.size, "modified": e.modified, "drive": e.is_drive, "flags": flags})
    lines = []
    for r in rows:
        if r["drive"]:
            lines.append(f"{r['name']:40s} {human_size(r['size']):>10s} free")
        else:
            lines.append(f"  {r['name']:38s} {human_size(r['size']):>10s}  {r['modified']}  {', '.join(r['flags'])}")
    emit(args, rows, "\n".join(lines))
    return 0


def logger_path(name: str) -> str:
    """`Main.CR6` -> `CPU:Main.CR6`; a name with a drive is kept."""
    return name if ":" in name else f"CPU:{name}"


def cmd_files_get(args) -> int:
    remote = logger_path(args.name)
    target = args.output or remote.split(":", 1)[1]
    with connect(args) as link:
        data = link.file_upload(remote, swath=983)
    with open(target, "wb") as out:
        out.write(data)
    emit(args, {"name": remote, "bytes": len(data), "file": target}, f"{remote} -> {target} ({len(data)} bytes)")
    return 0


def cmd_files_put(args) -> int:
    with open(args.file, "rb") as source:
        data = source.read()
    remote = logger_path(args.name or args.file.replace("\\", "/").rsplit("/", 1)[-1])
    with connect(args) as link:
        code = link.file_download(remote, data)
    if code != 0:
        raise CliError(f"{remote}: the logger refused the file, response code {code}")
    emit(args, {"name": remote, "bytes": len(data)}, f"{args.file} -> {remote} ({len(data)} bytes)")
    return 0


def cmd_files_rm(args) -> int:
    with connect(args) as link:
        for name in args.names:
            code, _ = link.file_control(logger_path(name), pakbus.FILECTL_DELETE)
            if code != 0:
                raise CliError(f"{logger_path(name)}: delete refused, response code {code}")
    emit(args, [logger_path(n) for n in args.names], "\n".join(f"deleted {logger_path(n)}" for n in args.names))
    return 0


def program_status(link: PakBusLink) -> dict:
    info = program_info(link)
    info["state"] = pakbus.COMPILE_STATES.get(info["compile_state"], f"state {info['compile_state']}")
    return info


def describe_program(info: dict) -> str:
    return (
        f"program   {info['program_name'] or '<none>'} ({info['state']})\n"
        f"power-up  {info['power_up_program'] or '<none>'}\n"
        f"result    {info['compile_result'].strip()}"
    )


def file_control(args, link: PakBusLink, name: str, code: int) -> dict:
    """Send a File Control command, wait out the logger's hold-off, reconnect if it
    re-enumerated, and return the program status afterwards."""
    resp, hold_off = link.file_control(name, code)
    if resp != 0:
        raise CliError(f"file control {code} on {name or 'the running program'}: response code {resp}")
    if hold_off:
        # Compiling makes the logger drop and recreate its USB device (docs/pakbus.md).
        time.sleep(hold_off)
        if flash.reconnect(link, args.port, model=link.model) is None:
            raise CliError("the logger's USB port did not come back after the compile")
    return program_status(link)


def cmd_program(args) -> int:
    with connect(args) as link:
        if args.action == "status":
            info = program_status(link)
        elif args.action == "run":
            name = logger_path(args.name)
            code = pakbus.FILECTL_COMPILE_RUN_KEEP_DATA if args.keep_data else pakbus.FILECTL_COMPILE_RUN_POWERUP
            if args.no_power_up:
                code = pakbus.FILECTL_COMPILE_RUN_KEEP_POWERUP
            info = file_control(args, link, name, code)
        elif args.action == "stop":
            code = pakbus.FILECTL_STOP_DELETE_DATA_CLEAR_POWERUP if args.delete_data else pakbus.FILECTL_PAUSE
            info = file_control(args, link, "", code)
        elif args.action == "resume":
            current = link.get_program_stats().get("program_name")
            if not current:
                raise CliError("no program is loaded; use `oln program run NAME`")
            info = file_control(args, link, current, pakbus.FILECTL_RESUME)
        else:  # power-up
            info = file_control(args, link, logger_path(args.name), pakbus.FILECTL_MARK_POWERUP)
    emit(args, info, describe_program(info))
    if args.action == "run" and "error" in info["compile_result"].lower():
        return 1
    return 0


def read_settings(link: PakBusLink) -> tuple[dict, dict]:
    got = link.get_settings()
    return got, settings_lib.load_definitions(got["device_type"], got["major"])


def cmd_settings(args) -> int:
    with connect(args) as link:
        got, defs = read_settings(link)
    wanted = [w.lower() for w in args.names]
    rows: list[dict] = []
    for sid, (read_only, raw) in sorted(got["settings"].items()):
        definition = defs.get(sid)
        name = definition.name if definition else ""
        if wanted and not any(w == str(sid) or w in name.lower() for w in wanted):
            continue
        value = settings_lib.decode(definition, raw)
        rows.append(
            {
                "id": sid,
                "name": name,
                "read_only": read_only,
                "value": value,
                "tab": definition.tab if definition else "",
            }
        )
    if not defs and not args.json:
        print("(no DevConfig description library found; settings are shown by number)", file=sys.stderr)
    lines = [f"{r['id']:4d}  {r['name'][:40]:40s} {'ro ' if r['read_only'] else '   '}{r['value']}" for r in rows]
    emit(args, {"device_type": got["device_type"], "settings": rows}, "\n".join(lines))
    return 0


def cmd_settings_set(args) -> int:
    with connect(args) as link:
        got, defs = read_settings(link)
        values = {}
        for assignment in args.assignments:
            key, sep, text = assignment.partition("=")
            if not sep:
                raise CliError(f"{assignment!r} is not NAME=VALUE")
            sid = settings_lib.find(defs, key.strip())
            if sid not in got["settings"]:
                raise CliError(f"the logger has no setting {sid}")
            if got["settings"][sid][0]:
                raise CliError(f"setting {sid} ({defs[sid].name if sid in defs else '?'}) is read-only")
            values[sid] = settings_lib.encode(defs.get(sid), text)
        outcomes = link.set_settings(values, commit=not args.no_commit)
    refused = {sid: code for sid, code in outcomes.items() if code != 1}
    if refused:
        raise CliError(f"the logger refused setting(s) {refused}; nothing was committed")
    names = {sid: defs[sid].name if sid in defs else str(sid) for sid in values}
    note = "" if args.no_commit else "; the logger saved them and is resetting"
    emit(
        args,
        {"set": [names[s] for s in values], "committed": not args.no_commit},
        f"set {', '.join(names.values())}{note}",
    )
    return 0


def cmd_watch(args) -> int:
    """LoggerNet's Table Monitor: the newest record of one table, read again every interval."""
    with connect(args) as link:
        table = find_table(link.table_definitions(), args.table)
        wanted = {f.lower() for f in args.fields}
        previous: dict = {}
        print_header = not args.json
        while True:
            started = time.monotonic()
            try:
                record = link.read_record(table)
            except PakBusError as exc:
                # A missed reply is reported and the watch goes on, as read_values --watch does.
                print(f"oln: {exc}", file=sys.stderr)
                record = None
            if record is not None:
                number, when, values = record
                names = records.column_names(table)
                flat = dict(zip(names, records.flat_values(table, values), strict=True))
                if wanted:
                    flat = {k: v for k, v in flat.items() if k.lower() in wanted or k.split("(")[0].lower() in wanted}
                if args.json:
                    row = {"record": number, "time": when.isoformat()}
                    row.update({k: records._json_safe(v) for k, v in flat.items()})
                    print(json.dumps(row), flush=True)
                else:
                    changed = {k: v for k, v in flat.items() if previous.get(k) != v}
                    if print_header:
                        print(f"{table.name}: {len(flat)} field(s), every {args.interval:g} s; Ctrl-C to stop")
                        print_header = False
                    stamp = records.timestamp(when)
                    for k, v in (flat if args.all_values else changed).items():
                        shown = f"{v:.6g}" if isinstance(v, float) else v
                        print(f"[{stamp}] {k:30s} {shown}", flush=True)
                previous = flat
            if args.count and args.count == 1:
                return 0
            if args.count:
                args.count -= 1
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


def cmd_terminal(args) -> int:
    """The logger's own terminal, as LoggerNet's Terminal Emulator opens it."""
    import threading

    with connect(args) as link:
        tran, prompt = link.terminal_open()
        if args.send is not None:
            # One-shot: send the text and print what comes back, for scripts and the extension.
            link.terminal_write(tran, args.send.encode().decode("unicode_escape").encode("latin-1"))
            deadline = time.monotonic() + args.wait
            out = bytearray()
            while time.monotonic() < deadline:
                out.extend(link.terminal_read(tran, 0.2))
            text = out.decode("latin-1")
            emit(args, {"output": text}, text)
            return 0
        lines: list[str] = []

        def read_keyboard() -> None:
            for line in sys.stdin:
                lines.append(line)
            lines.append("\x04")

        threading.Thread(target=read_keyboard, daemon=True).start()
        print("terminal open; type a line and Enter to send it, Ctrl-Z/Ctrl-D then Enter to leave", file=sys.stderr)
        sys.stdout.write(prompt.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n"))
        sys.stdout.flush()
        while True:
            text = link.terminal_read(tran, 0.1)
            if text:
                sys.stdout.write(text.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n"))
                sys.stdout.flush()
            while lines:
                line = lines.pop(0)
                if line == "\x04":
                    return 0
                link.terminal_write(tran, line.rstrip("\r\n").encode("latin-1") + b"\r")


def cmd_os(args) -> int:
    image = Path(args.image)
    if not image.is_file():
        raise CliError(f"no such file: {image}")

    def say(text: str) -> None:
        if not args.json:
            print(text, flush=True)

    def progress(sent: int, total: int) -> None:
        if not args.json and sys.stdout.isatty():
            print(f"\r  {sent}/{total} bytes ({100 * sent // total}%)", end="", flush=True)

    with connect(args) as link:
        result = os_update.send_os(link, image, port=args.port, restore=not args.no_restore, say=say, progress=progress)
    if args.json:
        emit(args, result)
    return 0 if result["after"] and result["after"] != result["before"] or args.same_ok else 1


STATUS_SUMMARY = [
    (
        "Datalogger",
        [
            "StationName",
            "OSVersion",
            "OSDate",
            "OSSignature",
            "SerialNumber",
            "PanelTemp",
            "MemorySize",
            "WatchdogErrors",
            "CPUDriveFree",
            "CardStatus",
        ],
    ),
    (
        "Program",
        [
            "ProgName",
            "StartTime",
            "RunSignature",
            "ProgSignature",
            "CompileResults",
            "MemoryFree",
            "ProgErrors",
            "SkippedScan",
            "SkippedSystemScan",
            "VarOutOfBound",
        ],
    ),
    ("Power", ["Battery", "PowerSource", "LithiumBattery", "Low12VCount", "Low5VCount"]),
]


def cmd_status(args) -> int:
    with connect(args) as link:
        table = find_table(link.table_definitions(), "Status")
        record = link.read_record(table)
    if record is None:
        raise CliError("the logger returned no Status record")
    _, when, values = record
    data = {k: (v.isoformat() if isinstance(v, datetime.datetime) else v) for k, v in values.items()}
    if args.all or args.json:
        emit(args, data, "\n".join(f"{k:28s} {v}" for k, v in data.items()))
        return 0
    lines = []
    for heading, keys in STATUS_SUMMARY:
        present = [k for k in keys if k in data]
        if present:
            lines.append(heading)
            lines.extend(f"  {k:24s} {str(data[k]).strip()}" for k in present)
    print("\n".join(lines))
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

    files = sub.add_parser("files", help="list, read, write and delete files on the logger")
    fsub = files.add_subparsers(dest="files_command", required=True, metavar="ACTION")
    fsub.add_parser("ls", help="drives, files, sizes and run flags").set_defaults(func=cmd_files_ls)
    p = fsub.add_parser("get", help="copy a file off the logger")
    p.add_argument("name", help="e.g. CPU:Main.CR6 (CPU: is assumed without a drive)")
    p.add_argument("-o", "--output", help="local file (default: the logger file's name)")
    p.set_defaults(func=cmd_files_get)
    p = fsub.add_parser("put", help="copy a file onto the logger; it is not run")
    p.add_argument("file")
    p.add_argument("name", nargs="?", help="name on the logger (default: CPU: + the file's name)")
    p.set_defaults(func=cmd_files_put)
    p = fsub.add_parser("rm", help="delete files on the logger")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_files_rm)

    prog = sub.add_parser("program", help="run, stop, resume or inspect the logger's program")
    psub = prog.add_subparsers(dest="action", required=True, metavar="ACTION")
    psub.add_parser("status", help="running program, state, power-up program, compile result")
    p = psub.add_parser("run", help="compile and run a program already on the logger")
    p.add_argument("name")
    p.add_argument("--keep-data", action="store_true", help="keep the data tables if they did not change")
    p.add_argument("--no-power-up", action="store_true", help="do not run it at power-up")
    p = psub.add_parser("stop", help="stop the running program; data is kept unless --delete-data")
    p.add_argument("--delete-data", action="store_true", help="also delete its data and clear run-at-power-up")
    psub.add_parser("resume", help="restart a stopped program, keeping its data")
    p = psub.add_parser("power-up", help="make a program the one that runs at power-up, without running it")
    p.add_argument("name")
    prog.set_defaults(func=cmd_program)

    p = sub.add_parser("status", help="the Status table, summarised as LoggerNet's Station Status")
    p.add_argument("--all", action="store_true", help="every Status field")
    p.set_defaults(func=cmd_status)

    sett = sub.add_parser("settings", help="device settings, as Device Configuration Utility shows them")
    ssub = sett.add_subparsers(dest="settings_command", metavar="ACTION")
    p = ssub.add_parser("get", help="list settings (the default)")
    p.add_argument("names", nargs="*", help="only settings whose number or name contains one of these")
    p.set_defaults(func=cmd_settings)
    p = ssub.add_parser("set", help="change settings; the logger saves them and resets")
    p.add_argument("assignments", nargs="+", metavar="NAME=VALUE", help='e.g. "Station Name=Bench" or 5=Bench')
    p.add_argument("--no-commit", action="store_true", help="send the values without saving or resetting")
    p.set_defaults(func=cmd_settings_set)
    sett.set_defaults(func=cmd_settings, names=[])

    p = sub.add_parser("watch", help="watch a table's newest record live, like LoggerNet's Table Monitor")
    p.add_argument("table", nargs="?", default="Public")
    p.add_argument("fields", nargs="*", help="only these fields (an array by its name, or one element)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between reads (default 1)")
    p.add_argument("--count", type=int, default=0, help="stop after this many reads")
    p.add_argument("--all-values", action="store_true", help="print every value each time, not only changes")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("terminal", help="the logger's terminal, like LoggerNet's Terminal Emulator")
    p.add_argument("--send", metavar="TEXT", help=r"send TEXT (escapes allowed, e.g. '\r') and print the reply")
    p.add_argument("--wait", type=float, default=1.5, help="seconds to collect the reply to --send (default 1.5)")
    p.set_defaults(func=cmd_terminal)

    p = sub.add_parser("os", help="send a new operating system (.obj) the way DevConfig's Manage OS does")
    p.add_argument("image", help="operating system image, e.g. CR300.11.03.obj")
    p.add_argument("--no-restore", action="store_true", help="leave the programs stopped afterwards")
    p.add_argument("--same-ok", action="store_true", help="exit 0 even when the OS version is unchanged")
    p.set_defaults(func=cmd_os)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `oln settings station` means `oln settings get station`.
    if "settings" in argv:
        at = argv.index("settings") + 1
        if at < len(argv) and argv[at] not in ("get", "set", "-h", "--help"):
            argv.insert(at, "get")
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
