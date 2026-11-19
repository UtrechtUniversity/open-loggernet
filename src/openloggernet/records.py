"""Write collected data table records as TOA5, TOB1, CSV or JSON Lines.

A record is what `PakBusLink.collect_recent` returns: (record number, timestamp, {field: value}),
where an array field's value is a list. TOA5 is the text format LoggerNet writes `.dat` files in:
four header lines (file environment, field names, units, processing) and then one quoted
timestamp, the record number and the values per line. TOB1 is LoggerNet's binary `.dat`: five
quoted header lines ending in CRLF (the TOA5 ones with SECONDS, NANOSECONDS, RECORD in front, then
the data types), and then packed little-endian records. Checked against a LoggerNet TOB1 file
(csdf's TOB1_Station_Daily.dat): ULONG and IEEE4 are little-endian, SecNano is two ULONGs.
"""

import csv
import datetime
import json
import math
import struct
from collections.abc import Iterable
from typing import BinaryIO, TextIO

from .pakbus import EPOCH, RECORD_TYPES, TableDef

FORMATS = ("toa5", "tob1", "csv", "jsonl")

# Record type -> (TOB1 type name, struct format). FP2 is written as IEEE4 and Booleans as LONG -1/0,
# as TOA5 writes them, so every column is a plain little-endian number.
TOB1_TYPES = {
    "uint1": ("ULONG", "<L"),
    "uint2": ("ULONG", "<L"),
    "uint4": ("ULONG", "<L"),
    "int1": ("LONG", "<l"),
    "int2": ("LONG", "<l"),
    "int4": ("LONG", "<l"),
    "fp2": ("IEEE4", "<f"),
    "ieee4": ("IEEE4", "<f"),
    "ieee8": ("IEEE8", "<d"),
    "bool": ("LONG", "<l"),
    "nsec": ("SecNano", "<LL"),
}


def column_names(table: TableDef) -> list[str]:
    """Field names with array elements spelled out as LoggerNet does: `Temp(1)`, `Temp(2)`, ..."""
    names = []
    for field in table.fields:
        if field.count > 1:
            names.extend(f"{field.name}({i})" for i in range(1, field.count + 1))
        else:
            names.append(field.name)
    return names


def _per_column(table: TableDef, attribute: str) -> list[str]:
    out = []
    for field in table.fields:
        width = field.count
        out.extend([getattr(field, attribute)] * width)
    return out


def flat_values(table: TableDef, values: dict) -> list:
    out = []
    for field in table.fields:
        value = values[field.name]
        out.extend(value if isinstance(value, list) else [value])
    return out


def timestamp(when: datetime.datetime) -> str:
    """LoggerNet's TOA5 timestamp: no zone, fractional seconds only when there are some."""
    text = when.strftime("%Y-%m-%d %H:%M:%S")
    if when.microsecond:
        text += f".{when.microsecond:06d}".rstrip("0")
    return text


def _toa5_value(value) -> str:
    if isinstance(value, datetime.datetime):
        return f'"{timestamp(value)}"'
    if isinstance(value, bool):
        return "-1" if value else "0"
    if isinstance(value, float):
        if math.isnan(value):
            return '"NAN"'
        if math.isinf(value):
            return '"INF"' if value > 0 else '"-INF"'
        return f"{value:.7g}"
    if isinstance(value, str):
        return '"' + value.replace('"', '""') + '"'
    return str(value)


def write_toa5_header(out: TextIO, table: TableDef, environment: dict) -> None:
    """`environment`: station, model, serial, os, program, program_signature."""
    head = [
        "TOA5",
        environment.get("station", ""),
        environment.get("model", ""),
        environment.get("serial", ""),
        environment.get("os", ""),
        environment.get("program", ""),
        str(environment.get("program_signature", "")),
        table.name,
    ]
    quoted = lambda items: ",".join(f'"{item}"' for item in items)
    out.write(quoted(head) + "\n")
    out.write(quoted(["TIMESTAMP", "RECORD", *column_names(table)]) + "\n")
    out.write(quoted(["TS", "RN", *_per_column(table, "units")]) + "\n")
    out.write(quoted(["", "", *_per_column(table, "processing")]) + "\n")


def _seconds_nanos(when: datetime.datetime) -> tuple[int, int]:
    delta = when.replace(tzinfo=None) - EPOCH.replace(tzinfo=None)
    return delta.days * 86400 + delta.seconds, delta.microseconds * 1000


def write_tob1(out: BinaryIO, table: TableDef, records: Iterable, environment: dict | None = None) -> int:
    """Write `records` as TOB1 to a binary stream. Returns the record count."""
    env = environment or {}
    columns = []  # (type name, struct format, string length) per column
    for field in table.fields:
        kind = RECORD_TYPES[field.type_code][0]
        if kind == "ascii":
            length = field.piece // field.count
            columns += [(f"ASCII({length})", None, length)] * field.count
        else:
            columns += [(*TOB1_TYPES[kind], 0)] * field.count
    quoted = lambda items: (",".join(f'"{item}"' for item in items) + "\r\n").encode("latin-1", "replace")
    keys = ("station", "model", "serial", "os", "program", "program_signature")
    out.write(quoted(["TOB1", *(env.get(k, "") for k in keys), table.name]))
    out.write(quoted(["SECONDS", "NANOSECONDS", "RECORD", *column_names(table)]))
    out.write(quoted(["SECONDS", "NANOSECONDS", "RN", *_per_column(table, "units")]))
    out.write(quoted(["", "", "", *_per_column(table, "processing")]))
    out.write(quoted(["ULONG", "ULONG", "ULONG", *(c[0] for c in columns)]))
    count = 0
    for number, when, values in records:
        out.write(struct.pack("<LLL", *_seconds_nanos(when), number))
        for (_, fmt, length), value in zip(columns, flat_values(table, values), strict=True):
            if fmt is None:
                out.write(str(value).encode("latin-1", "replace")[:length].ljust(length, b"\0"))
            elif fmt == "<LL":
                out.write(struct.pack(fmt, *_seconds_nanos(value)))
            elif isinstance(value, bool):
                out.write(struct.pack(fmt, -1 if value else 0))
            else:
                out.write(struct.pack(fmt, value))
        count += 1
    return count


def write_records(out: TextIO, table: TableDef, records: Iterable, fmt: str, environment: dict | None = None) -> int:
    """Write `records` to `out` in `fmt`, with the format's header. Returns the record count."""
    if fmt == "tob1":
        raise ValueError("TOB1 is binary; use write_tob1 with a binary stream")
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    count = 0
    names = column_names(table)
    if fmt == "toa5":
        write_toa5_header(out, table, environment or {})
        for number, when, values in records:
            row = [f'"{timestamp(when)}"', str(number), *(_toa5_value(v) for v in flat_values(table, values))]
            out.write(",".join(row) + "\n")
            count += 1
    elif fmt == "csv":
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["TIMESTAMP", "RECORD", *names])
        for number, when, values in records:
            row = [timestamp(v) if isinstance(v, datetime.datetime) else v for v in flat_values(table, values)]
            writer.writerow([timestamp(when), number, *row])
            count += 1
    else:
        for number, when, values in records:
            row = {"record": number, "time": when.isoformat()}
            row.update(zip(names, (_json_safe(v) for v in flat_values(table, values)), strict=True))
            out.write(json.dumps(row) + "\n")
            count += 1
    return count


def _json_safe(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value).upper()
    return value
