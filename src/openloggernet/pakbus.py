#!/usr/bin/env python3
"""Minimal PakBus/BMP5 client for a Campbell logger over serial: send program files, read values back.

Written against the CR6 and also used with the CR1000X, CR300 and CR350. The link reads the
logger model from the USB product string and sizes its requests to that model's reply limit
(`MAX_REPLY_FRAME`).

Only the transactions this repository's bench tooling needs are implemented:

    Hello              (PakCtrl 0x09 / 0x89)  open the link
    File Download      (BMP5    0x1c / 0x9c)  write a file to the logger
    File Control       (BMP5    0x1e / 0x9e)  compile / run / stop a file
    Get Prog Stats     (BMP5    0x18 / 0x98)  read back compile results
    Get Values         (BMP5    0x1a / 0x9a)  read a variable or table field
    Set Values         (BMP5    0x1b / 0x9b)  write a variable or table field
    Clock              (BMP5    0x17 / 0x97)  read or adjust the logger clock
    File Upload        (BMP5    0x1d / 0x9d)  read a file off the logger, incl. `.TDF`
    Collect Data       (BMP5    0x09 / 0x89)  read stored data table records

Wire-format notes and sources: docs/pakbus.md
"""

import datetime
import re
import struct
import sys
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


@dataclass
class LoggerPort:
    """A serial port with a Campbell logger behind it. `model` comes from the USB descriptors."""

    device: str
    model: str | None
    description: str


def _model_of(info) -> str | None:
    """The model a port reports: its USB product string on Linux and macOS, or, on Windows, where
    the product string is empty, the `CR300 (COM4)` description built from it."""
    product = (info.product or "").strip()
    if not product and sys.platform == "win32":
        product = re.sub(r"\s*\(COM\d+\)$", "", info.description or "").strip()
    return product.upper() or None


def list_loggers() -> list[LoggerPort]:
    """Every attached Campbell logger port, by USB manufacturer string, sorted by device name.

    Looked up afresh on every call: the logger re-enumerates when it compiles a newly sent
    program and can come back on a different node (docs/pakbus.md).
    """
    return [
        LoggerPort(info.device, _model_of(info), info.description or "")
        for info in sorted(list_ports.comports(), key=lambda port: port.device)
        if "campbell" in (info.manufacturer or "").lower()
    ]


def find_port(model: str | None = None) -> str | None:
    """Locate a Campbell logger's USB serial port, optionally of one model such as "CR300".

    Without a model the first logger is taken, and failing that a port that only looks like a
    CDC-ACM modem.
    """
    loggers = list_loggers()
    if model:
        wanted = model.strip().upper()
        return next((p.device for p in loggers if p.model == wanted), None)
    if loggers:
        return loggers[0].device
    for port in sorted(list_ports.comports(), key=lambda port: port.device):
        if "ttyACM" in port.device or "usbmodem" in port.device:
            return port.device
    return None


def require_port(model: str | None = None) -> str:
    """`find_port`, raising when no logger is attached."""
    port = find_port(model)
    if port is None:
        raise PakBusError(f"no {model or 'Campbell logger'} serial port found")
    return port


# --- framing ---------------------------------------------------------------

SYNC = 0xBD  # frame delimiter
QUOTE = 0xBC  # escape byte; 0xBC -> BC DC, 0xBD -> BC DD

# higher-level protocol codes carried in the PakBus header
PROTO_PAKCTRL = 0x0
PROTO_BMP5 = 0x1

# link states
LINK_RING = 0x9
LINK_READY = 0xA

# message types
MSG_HELLO = 0x09
MSG_HELLO_RESP = 0x89
MSG_PLEASE_WAIT = 0xA1
MSG_GETPROGSTAT = 0x18
MSG_GETPROGSTAT_RESP = 0x98
MSG_FILEDOWNLOAD = 0x1C
MSG_FILEDOWNLOAD_RESP = 0x9C
MSG_FILECONTROL = 0x1E
MSG_FILECONTROL_RESP = 0x9E
MSG_GETVALUES = 0x1A
MSG_GETVALUES_RESP = 0x9A
MSG_SETVALUES = 0x1B
MSG_SETVALUES_RESP = 0x9B
MSG_CLOCK = 0x17
MSG_CLOCK_RESP = 0x97
MSG_FILEUPLOAD = 0x1D
MSG_FILEUPLOAD_RESP = 0x9D
MSG_COLLECTDATA = 0x09
MSG_COLLECTDATA_RESP = 0x89
MSG_TERMINAL = 0x0B
MSG_TERMINAL_RESP = 0x8B
# PakCtrl messages Device Configuration Utility uses (USB capture against the CR300, 2026-11-02).
MSG_GET_STRING_SETTINGS = 0x07  # -> 0x87 "Name=value;Name=value;"
MSG_GET_STRING_SETTINGS_RESP = 0x87
MSG_DEVCONFIG_GET = 0x0F  # -> 0x8F typed settings, by numeric id
MSG_DEVCONFIG_GET_RESP = 0x8F
MSG_DEVCONFIG_SET = 0x10
MSG_DEVCONFIG_SET_RESP = 0x90
MSG_DEVCONFIG_CONTROL = 0x13  # action 1 = commit the settings set and reset the logger
MSG_DEVCONFIG_CONTROL_RESP = 0x93
DEVCONFIG_COMMIT = 1

# Collect Data modes. 'Most recent' serves loggers without a reply limit; modes 3 and 4 were
# both answered with response code 7 by the CR6 on 2026-09-05. A record-number range serves
# loggers with a reply limit, because 'most recent' fills a reply to the logger's own size.
COLLECT_MOST_RECENT = 0x05
COLLECT_RANGE = 0x06
# Part of one record from a byte offset: P1 record number (uint4), P2 byte offset (uint4).
# How LoggerNet reads Status and Public, whose records exceed a packet (capture, 2026-11-02).
COLLECT_PARTIAL = 0x08

# File Control command codes. Only the codes this tool uses are named; the full
# set is listed in the CR6 help under Web Server/API Commands -> FileControl,
# whose action codes are the same set.
# Codes 1, 4, 11, 12 and 13 are what LoggerNet's File Control sent for the actions named
# (USB capture against the CR6, 2026-11-02); 11 and 13 go with an empty file name. The others
# are from the CRBasic help and are marked where not seen.
FILECTL_COMPILE_RUN_POWERUP = 1  # "Run Now" + "Run On Power-up"; also flash's code
FILECTL_MARK_POWERUP = 2  # help only
FILECTL_DELETE = 4  # "Delete"
FILECTL_COMPILE_RUN_KEEP_DATA = 6  # help only
FILECTL_STOP = 7  # help only; LoggerNet stops with 11 or 13
FILECTL_STOP_DELETE_DATA = 8  # help only
FILECTL_COMPILE_RUN_KEEP_POWERUP = 10  # help only
FILECTL_PAUSE = 11  # "Stop Program", "Retain Data"; empty file name
FILECTL_RESUME = 12  # "Restart Program", "Do not erase data files"; with the program's name
FILECTL_STOP_DELETE_DATA_CLEAR_POWERUP = 13  # "Stop Program", "Delete Data"; empty file name

# Get Prog Stats compile state: 1 while LoggerNet showed the program running, 3 after it stopped
# it with code 11, 0 with no program (CR6, 2026-11-02).
COMPILE_STATES = {0: "no program", 1: "running", 3: "stopped"}

# File bytes per download packet. 976 is what DevConfig uses; on Windows the CR6, CR1000X and CR300
# all took it (2026-11-02). A Mac lost 512-byte packets to the CR1000X, so elsewhere it stays 256;
# docs/lessons.md. More than the logger's MaxPktSize (1000) was lost on the CR300.
DEFAULT_SWATH = 976 if sys.platform == "win32" else 256

# The logger reports time as signed seconds and nanoseconds since this epoch.
EPOCH = datetime.datetime(1990, 1, 1, tzinfo=datetime.UTC)

# --- Get Values data types -------------------------------------------------
# The logger converts a field to whatever type is asked for, where it can, so
# the type code chooses the representation rather than describing the field.
# These four were established on the bench (2026-08-31) by reading Public
# variables of known type and value; docs/pakbus.md
# records how. Everything below is what this repository has confirmed, not the
# full BMP5 type table.
TYPE_INT4 = 6  # 4-byte signed, big-endian. Exact for `As Long`.
TYPE_IEEE4B = 9  # 4-byte IEEE, big-endian. Exact for Float/IEEE4.
TYPE_ASCIIZ = 16  # the logger's own formatting, NUL-terminated.
TYPE_IEEE8B = 18  # 8-byte IEEE, big-endian.

# name -> (type code, struct format, bytes per value)
VALUE_TYPES = {
    "asciiz": (TYPE_ASCIIZ, None, None),
    "long": (TYPE_INT4, ">i", 4),
    "ieee4": (TYPE_IEEE4B, ">f", 4),
    "ieee8": (TYPE_IEEE8B, ">d", 8),
}

# --- stored-record field types ---------------------------------------------
# Table definitions and Get Values share one BMP5 type space, but a stored record
# is packed, so each type also needs a size. Only the types this program's data
# tables actually use are decoded; anything else raises rather than guessing.
# Confirmed on CR6 serial 18624 on 2026-09-05 by decoding known values back out
# of DailyPower, Shunt_Data and PT1000_Data.
RECORD_TYPES = {
    1: ("uint1", 1),
    2: ("uint2", 2),
    3: ("uint4", 4),
    4: ("int1", 1),
    5: ("int2", 2),
    6: ("int4", 4),
    7: ("fp2", 2),
    9: ("ieee4", 4),
    11: ("ascii", 1),  # length comes from the field's piece count
    14: ("nsec", 8),  # timestamp: seconds and nanoseconds since 1990
    17: ("bool", 1),
    18: ("ieee8", 8),
    28: ("bool", 4),  # CRBasic Boolean
}

# struct format per numeric record type; bool, nsec and ascii are decoded by hand
RECORD_FORMATS = {
    "uint1": "B",
    "uint2": "H",
    "uint4": "L",
    "int1": "b",
    "int2": "h",
    "int4": "l",
    "fp2": "H",
    "ieee4": "f",
    "ieee8": "d",
}

# Types 2, 6, 7, 9 and 11 were confirmed by decoding known values back out of DailyPower,
# Shunt_Data, PT1000_Data and TString_Data and matching them against the serial trace.
# Type 28 is a CRBasic `Boolean` stored with `Sample(...,Boolean)`, 4 bytes wide: decoded out of
# Freezer_tester.CR6's Battery_Data and FinApp_Starts on 2026-09-22, where widths 1 and 2 broke
# the record framing and 4 gave the Battery() and PanelTemp values beside it.
# Type 14 is a timestamp, 8 bytes: the CR6's Status.StartTime, read whole with read_record on
# 2026-11-02, matched the start time LoggerNet's Station Status showed.

FP2_NAN = 0x9FFE
FP2_POS_INF = 0x1FFF
FP2_NEG_INF = 0x9FFF


def decode_fp2(word: int) -> float:
    """Campbell two-byte float: sign, 2-bit negative decimal exponent, 13-bit mantissa."""
    if word == FP2_NAN:
        return float("nan")
    if word == FP2_POS_INF:
        return float("inf")
    if word == FP2_NEG_INF:
        return float("-inf")
    sign = -1.0 if word & 0x8000 else 1.0
    exponent = (word >> 13) & 0x03
    mantissa = word & 0x1FFF
    return sign * mantissa / (10**exponent)


# Response codes seen on the wire from this logger are 0, 16 and 17; the other
# two are from the open-source implementations cited in the protocol notes and
# have not been provoked here.
GETVALUES_RESP = {
    0: "ok",
    1: "permission denied",
    16: "invalid table or field name",
    17: "data type conversion not supported",
    20: "memory bounds violation",
}

# Set Values shares the Get Values response-code space. Only 0 and 16 have been
# seen from this logger; the rest are carried over from the Get Values table and
# have not been provoked here.
SETVALUES_RESP = dict(GETVALUES_RESP)

# Clock response codes. 0 was observed for a zero adjustment on 2026-09-03; the
# meaning of any other code is not established here, so an unknown code is
# reported rather than interpreted.
CLOCK_RESP = {
    0: "ok",
    1: "permission denied",
}

# Every BMP5 command carries a security code. This is a bench tool for
# validation work on an unsecured logger, so it is fixed at 0 and deliberately
# not configurable - nothing here should be able to reach a secured station.
SECURITY_CODE = 0


def signature(data: bytes, seed: int = 0xAAAA) -> int:
    """Campbell 16-bit packet signature."""
    sig = seed
    for byte in data:
        prev = sig
        sig = (sig << 1) & 0x1FF
        if sig >= 0x100:
            sig += 1
        sig = ((sig + (prev >> 8) + byte) & 0xFF) | ((prev << 8) & 0xFF00)
    return sig


def signature_nullifier(sig: int) -> bytes:
    """Two bytes that drive the running signature of a packet to zero."""
    out = bytearray()
    for _ in range(2):
        sig = signature(bytes(out[-1:]), sig) if out else sig
        doubled = (sig << 1) & 0x1FF
        if doubled >= 0x100:
            doubled += 1
        out.append((0x100 - (doubled + (sig >> 8))) & 0xFF)
    return bytes(out)


def quote(data: bytes) -> bytes:
    return data.replace(b"\xbc", b"\xbc\xdc").replace(b"\xbd", b"\xbc\xdd")


def unquote(data: bytes) -> bytes:
    return data.replace(b"\xbc\xdd", b"\xbd").replace(b"\xbc\xdc", b"\xbc")


def frame(packet: bytes) -> bytes:
    """Append the signature nullifier, quote, and delimit a raw packet."""
    body = packet + signature_nullifier(signature(packet))
    return bytes([SYNC]) + quote(body) + bytes([SYNC])


def header(
    dst_node: int,
    src_node: int,
    proto: int,
    *,
    link_state: int = LINK_READY,
    expect_more: int = 0x2,
    priority: int = 0x1,
    hop_count: int = 0x0,
) -> bytes:
    return struct.pack(
        ">4H",
        (link_state & 0xF) << 12 | (dst_node & 0xFFF),
        (expect_more & 0x3) << 14 | (priority & 0x3) << 12 | (src_node & 0xFFF),
        (proto & 0xF) << 12 | (dst_node & 0xFFF),
        (hop_count & 0xF) << 12 | (src_node & 0xFFF),
    )


def asciiz(text: str) -> bytes:
    return text.encode("ascii") + b"\0"


def read_asciiz(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\0", offset)
    return data[offset:end].decode("ascii", "replace"), end + 1


def parse_table_definitions(data: bytes) -> list:
    """Parse a `.TDF` into TableDef objects.

    Layout, established byte by byte against this logger on 2026-09-05 by aligning the file
    against table and field names already known from the CRBasic source:

        version (byte), then per table:
            name (asciiz), size (uint4), time type (byte),
            time-into (2 x uint4), interval (2 x uint4),
            then per field until a 0 type byte:
                type (byte, 0x80 = read only), name, alias, processing, units,
                description (all asciiz), begin index (uint4), piece (uint4),
                dimensions (uint4 list terminated by a 0)
    """
    if not data:
        raise PakBusError("empty table definition file")
    offset = 1  # file version byte
    tables = []
    number = 0
    while offset < len(data):
        start = offset
        number += 1
        name, offset = read_asciiz(data, offset)
        size = struct.unpack_from(">L", data, offset)[0]
        offset += 4
        time_type = data[offset]
        offset += 1
        offset += 8  # time into the interval, unused here
        interval_sec, interval_nsec = struct.unpack_from(">LL", data, offset)
        offset += 8
        fields = []
        while data[offset] != 0:
            raw_type = data[offset]
            offset += 1
            field_name, offset = read_asciiz(data, offset)
            _alias, offset = read_asciiz(data, offset)
            processing, offset = read_asciiz(data, offset)
            units, offset = read_asciiz(data, offset)
            description, offset = read_asciiz(data, offset)
            begin_index = struct.unpack_from(">L", data, offset)[0]
            offset += 4
            piece = struct.unpack_from(">L", data, offset)[0]
            offset += 4
            dimensions = []
            while True:
                value = struct.unpack_from(">L", data, offset)[0]
                offset += 4
                if value == 0:
                    break
                dimensions.append(value)
            fields.append(
                FieldDef(
                    field_name,
                    raw_type & 0x7F,
                    bool(raw_type & 0x80),
                    processing,
                    units,
                    description,
                    begin_index,
                    piece,
                    dimensions,
                )
            )
        offset += 1  # field list terminator
        tables.append(
            TableDef(number, name, size, time_type, interval_sec, fields, signature(data[start:offset]), interval_nsec)
        )
    return tables


def decode_record(table: TableDef, buf: bytes) -> dict:
    """Unpack one packed record into {field name: value or list of values}."""
    out: dict[str, object] = {}
    at = 0
    for field in table.fields:
        if field.type_code not in RECORD_TYPES:
            raise PakBusError(
                f"{table.name}.{field.name}: field type {field.type_code} is not decoded; "
                f"add it to RECORD_TYPES once its layout is confirmed on hardware"
            )
        kind, width = RECORD_TYPES[field.type_code]
        if kind == "ascii":
            raw = buf[at : at + field.piece]
            out[field.name] = raw.split(b"\0", 1)[0].decode("ascii", "replace")
            at += field.piece
            continue
        raw = buf[at : at + width * field.count]
        at += width * field.count
        if kind == "bool":
            values = [byte != 0 for byte in raw[::width]]
        elif kind == "nsec":
            values = [
                EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
                for secs, nanos in struct.iter_unpack(">LL", raw)
            ]
        else:
            values = [value for (value,) in struct.iter_unpack(">" + RECORD_FORMATS[kind], raw)]
            if kind == "fp2":
                values = [decode_fp2(word) for word in values]
        out[field.name] = values[0] if len(values) == 1 else values
    return out


def parse_collect_response(table: TableDef, body: bytes) -> tuple[int, list, bool]:
    """Decode a Collect Data response body into (first record number, records, more flag).

    One response can carry **several blocks**, each with its own table number, first record
    number, record count and -- for interval tables -- its own timestamp. The logger starts a
    new block where an interval table's record times are not contiguous. Only the final byte of
    the body is the more-records flag. Reading just the first block and treating the next byte
    as that flag silently stopped paging part way through a table; observed on 2026-09-14 when a
    ten-day `Shunt_Data` collection returned only its first 255 records.
    """
    if not body:
        raise PakBusError(f"{table.name}: empty collect data response")
    resp_code = body[0]
    if resp_code != 0:
        raise PakBusError(f"{table.name}: collect data response code {resp_code}")
    if len(body) <= 1:
        return 0, [], False

    end = len(body) - 1  # the last byte is the more-records flag
    at = 1
    first_begin = None
    records = []
    while at < end:
        if at + 8 > end:
            raise PakBusError(f"{table.name}: truncated block header at byte {at}")
        _number, begin, nbr = struct.unpack_from(">HLH", body, at)
        at += 8
        if nbr & 0x8000:
            raise PakBusError(f"{table.name}: part of a record; read it with PakBusLink.read_record")
        if first_begin is None:
            first_begin = begin
        stamp = EPOCH
        for index in range(nbr):
            if table.is_interval:
                if index == 0:
                    secs, nanos = struct.unpack_from(">LL", body, at)
                    at += 8
                    stamp = EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
                when = stamp + table.interval * index
            else:
                secs, nanos = struct.unpack_from(">LL", body, at)
                at += 8
                when = EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
            chunk = body[at : at + table.record_size]
            at += table.record_size
            if len(chunk) < table.record_size or at > end:
                raise PakBusError(
                    f"{table.name}: record {begin + index} is {table.record_size} bytes and did "
                    f"not fit the response; read records larger than a packet with "
                    f"PakBusLink.read_record."
                )
            records.append((begin + index, when, decode_record(table, chunk)))
    return (first_begin or 0), records, bool(body[end])


def is_partial_response(body: bytes) -> bool:
    """A Collect Data reply that carries part of one record: the top bit of the word where a
    whole-record block has its record count. Seen from LoggerNet's capture, 2026-11-02."""
    return len(body) >= 11 and body[0] == 0 and bool(body[7] & 0x80)


def parse_partial_response(body: bytes) -> tuple[int, int, bytes]:
    """Decode a partial-record reply into (record number, byte offset, fragment).

    Layout, from LoggerNet reading the CR6 `Status` table (1520-byte records) on 2026-11-02:

        response code (byte), table number (uint2), record number (uint4),
        0x80000000 | byte offset into the record image (uint4), fragment, more flag (byte)

    The record image is the record's 8-byte timestamp followed by its fields, so the first
    fragment starts with the timestamp and the offsets count it: LoggerNet asked for offset 966
    after a 966-byte first fragment, and 966 + 562 = 8 + 1520.
    """
    if body[0] != 0:
        raise PakBusError(f"collect data response code {body[0]}")
    _number, record, word = struct.unpack_from(">HLL", body, 1)
    return record, word & 0x7FFFFFFF, body[11:-1]


# --- pseudo-files ------------------------------------------------------------

# File attribute codes in `.DIR`/`.DIR2`. The running program carried 1 and 2 while LoggerNet's
# File Control showed it as "running, power up" (CR6, CR1000X, CR300, 2026-11-02).
FILE_ATTRIBUTES = {1: "running", 2: "run on power up"}


@dataclass
class DirEntry:
    """A file, or a drive (`size` is then its free bytes and `modified` is empty)."""

    name: str
    size: int
    modified: str
    attributes: list

    @property
    def is_drive(self) -> bool:
        return self.name.endswith(":")


def parse_directory(data: bytes) -> list[DirEntry]:
    """Parse `.DIR` or `.DIR2` into drives and files, in the logger's order.

    Layout, from the CR6, CR1000X and CR300 on 2026-11-02 against LoggerNet's File Control:

        version (byte; 1 for .DIR, 2 for .DIR2), then per entry:
            name (asciiz), size (uint4), modified (asciiz), attributes (bytes, 0-terminated)

    A drive entry has a name ending in ':' and an empty date, and its size field is the drive's
    free space: a uint4 in `.DIR`, an IEEE4 float in `.DIR2`. The CR6's `.DIR2` said 0x4BEFA000,
    31 408 128 as a float, where LoggerNet showed 31.41 MB free and `.DIR` said 31 408 128.
    """
    if not data:
        raise PakBusError("empty directory")
    version, at, out = data[0], 1, []
    while at < len(data):
        name, at = read_asciiz(data, at)
        raw = data[at : at + 4]
        at += 4
        modified, at = read_asciiz(data, at)
        end = data.index(b"\0", at)
        attributes = list(data[at:end])
        at = end + 1
        if name.endswith(":") and version >= 2:
            size = int(struct.unpack(">f", raw)[0])
        else:
            size = struct.unpack(">L", raw)[0]
        out.append(DirEntry(name, size, modified, attributes))
    return out


def parse_tdfs(data: bytes) -> dict:
    """Parse `.TDFS`, the summary LoggerNet reads on connecting.

    Layout, from all three bench loggers on 2026-11-02: version (byte, 0x11), then the Get Prog
    Stats fields (OS version, OS signature, serial number, power-up program, compile state,
    program name, program signature, compile time, compile result), the **model** (asciiz), and
    per table its name (asciiz), size (uint4) and signature (uint2).
    """
    out: dict = {"version": data[0]}
    at = 1
    out["os_version"], at = read_asciiz(data, at)
    out["os_signature"] = struct.unpack_from(">H", data, at)[0]
    out["serial_number"], at = read_asciiz(data, at + 2)
    out["power_up_program"], at = read_asciiz(data, at)
    out["compile_state"] = data[at]
    out["program_name"], at = read_asciiz(data, at + 1)
    out["program_signature"] = struct.unpack_from(">H", data, at)[0]
    seconds, nanos = struct.unpack_from(">lL", data, at + 2)
    out["compile_time"] = EPOCH + datetime.timedelta(seconds=seconds, microseconds=nanos / 1000)
    out["compile_result"], at = read_asciiz(data, at + 10)
    out["model"], at = read_asciiz(data, at)
    tables = []
    while at < len(data):
        name, at = read_asciiz(data, at)
        size, sig = struct.unpack_from(">LH", data, at)
        at += 6
        tables.append({"name": name, "size": size, "signature": sig})
    out["tables"] = tables
    return out


# --- decoded packets -------------------------------------------------------


@dataclass
class FieldDef:
    """One field of a data table, as described by the logger's own `.TDF`."""

    name: str
    type_code: int
    read_only: bool
    processing: str
    units: str
    description: str
    begin_index: int
    piece: int
    dimensions: list

    @property
    def count(self) -> int:
        """Number of values this field contributes to a record.

        That is `piece`, not the product of `dimensions`. The dimensions describe the source
        array, and a field can store part of it: `SbdFinAppDaily.FinApp_Neutrons_Avg` is one
        element of a two-element array, `dimensions = [2]` with `piece = 1`, and its records are
        32 bytes, not the 40 that multiplying out the dimensions gives. Confirmed on 2026-09-17.
        """
        if self.type_code == 11:
            return 1
        return self.piece

    @property
    def size(self) -> int:
        kind, width = RECORD_TYPES[self.type_code]
        if kind == "ascii":
            return self.piece
        return width * self.count


@dataclass
class TableDef:
    """A data table. `number` is its 1-based position in the `.TDF`, which is what
    Collect Data addresses, and `signature` is over that table's own definition bytes."""

    number: int
    name: str
    size: int
    time_type: int
    interval_sec: int
    fields: list
    signature: int
    interval_nsec: int = 0  # the sub-second part, for tables faster than 1 s

    @property
    def record_size(self) -> int:
        return sum(f.size for f in self.fields)

    @property
    def interval(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.interval_sec, microseconds=self.interval_nsec / 1000)

    @property
    def is_interval(self) -> bool:
        """Interval tables carry one timestamp per fragment; event tables one per record."""
        return self.interval_sec > 0 or self.interval_nsec > 0


@dataclass
class Packet:
    dst_node: int
    src_node: int
    proto: int
    msg_type: int
    tran_nbr: int
    body: bytes  # message payload after msg_type and tran_nbr
    raw: bytes


def decode(packet: bytes) -> Packet | None:
    if len(packet) < 10:
        return None
    words = struct.unpack(">4H", packet[:8])
    return Packet(
        dst_node=words[2] & 0xFFF,
        src_node=words[3] & 0xFFF,
        proto=(words[2] >> 12) & 0xF,
        msg_type=packet[8],
        tran_nbr=packet[9],
        body=packet[10:],
        raw=packet,
    )


class TextFilter:
    """Pull a program's ComUSB text out of a stream that also carries PakBus.

    The logger beacons PakBus frames on ComUSB even while a running program
    owns the port, and they interleave mid-line with the program's own output
    (docs/lessons.md). Frames are dropped; anything that fails to
    check out as a frame is passed through as text, so a lost delimiter costs
    at most one garbled line instead of swallowing the rest of the session.
    """

    MAX_FRAME = 2048  # bail-out bound: beyond this it was never a frame

    def __init__(self) -> None:
        self._in_frame = False
        self._held = bytearray()

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            if not self._in_frame:
                if byte == SYNC:
                    self._in_frame = True
                    self._held.clear()
                else:
                    out.append(byte)
                continue
            if byte == SYNC:
                if not self._held:
                    continue  # runs of delimiters are just framing
                if signature(unquote(bytes(self._held))) != 0:
                    out.extend(self._held)  # not a frame after all
                self._held.clear()
                self._in_frame = False
                continue
            self._held.append(byte)
            if len(self._held) > self.MAX_FRAME:
                out.extend(self._held)
                self._held.clear()
                self._in_frame = False
        return bytes(out)


# Largest reply frame, SYNC bytes included, that reaches the host intact, by USB product string.
# Only the first 64-byte USB packet of a CR300 reply reached this client on Linux; the CR350
# entry is assumed. On Windows the same CR300 sends whole replies, so the cut belongs to the
# Linux host path and no limit is applied there: docs/pakbus.md, "CR300 replies cut at 64 bytes".
# Models not listed (CR6, CR1000X) need none; the CR6 answers with replies of about 1 KB.
MAX_REPLY_FRAME = {"CR300": 64, "CR350": 64} if sys.platform != "win32" else {}

# Frame bytes outside a reply's message body: two SYNCs, the header, the message type and
# transaction number, and the signature nullifier.
FRAME_OVERHEAD = 2 + 8 + 2 + 2


class PakBusError(Exception):
    pass


class PakBusTimeout(PakBusError):
    """No reply arrived; under a reply limit, usually a reply that outgrew it."""


def port_model(port: str) -> str | None:
    """The logger model a USB serial port reports, e.g. "CR300"."""
    for info in list_ports.comports():
        if info.device == port:
            return _model_of(info)
    return None


def reply_budget(max_frame: int) -> int:
    """Message body bytes a reply may carry and still fit `max_frame`.

    An eighth is held back for quoting, where each 0xBC or 0xBD goes out as two bytes. A reply
    that needs more than that is lost, and the callers split and retry it.
    """
    return (max_frame - FRAME_OVERHEAD) * 7 // 8


class PakBusLink:
    """PakBus over a serial port (the logger USB port speaks PakBus directly)."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        node: int = 1,
        my_node: int = 4094,
        max_reply_frame: int | None = None,
        debug: bool = False,
    ):
        """`max_reply_frame` overrides the reply limit looked up from the logger model."""
        self.node = node
        self.my_node = my_node
        self.debug = debug
        self.baudrate = baudrate
        self._tran = 0
        self._buf = bytearray()
        self._port_name = port
        self.model: str | None = None
        self.max_reply_frame: int | None = max_reply_frame
        self._limit_override = max_reply_frame is not None
        self.open()

    def open(self, port: str | None = None) -> None:
        """(Re)open the serial port, e.g. after the logger re-enumerates."""
        self.close()
        self._port_name = port or self._port_name
        self._buf.clear()
        self.model = port_model(self._port_name)
        if not self._limit_override:
            self.max_reply_frame = MAX_REPLY_FRAME.get(self.model or "")
        self.port: serial.Serial = serial.Serial(self._port_name, self.baudrate, timeout=0.1)

    def close(self) -> None:
        if not hasattr(self, "port"):
            return
        try:
            self.port.close()
        except OSError, serial.SerialException:
            pass

    def __enter__(self) -> PakBusLink:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport --

    def next_tran(self) -> int:
        self._tran = (self._tran + 1) & 0xFF or 1
        return self._tran

    def send(self, packet: bytes) -> None:
        if self.debug:
            print(f"  TX {packet.hex(' ')}")
        self.port.write(frame(packet))
        self.port.flush()

    def receive(self, timeout: float = 5.0) -> Packet | None:
        """Return the next well-formed packet, or None on timeout.

        Bytes outside 0xBD-delimited frames are discarded, so debug output a
        program writes to the same USB port does not break packet decoding.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Ask only for what is already buffered, or one byte. `read(512)` blocked until 512
            # bytes or the port timeout, which put 100 ms on every short reply (docs/pakbus.md).
            chunk = self.port.read(max(1, self.port.in_waiting))
            if chunk:
                self._buf.extend(chunk)
            while SYNC in self._buf:
                start = self._buf.index(SYNC)
                end = self._buf.find(SYNC, start + 1)
                if end < 0:
                    del self._buf[:start]
                    break
                candidate = bytes(self._buf[start + 1 : end])
                del self._buf[:end]  # leave the trailing SYNC as the next start
                if not candidate:
                    continue
                body = unquote(candidate)
                if signature(body) != 0:
                    if self.debug:
                        print(f"  ?? bad signature: {body.hex(' ')}")
                    continue
                packet = decode(body[:-2])
                if packet is None:
                    continue
                if self.debug:
                    print(f"  RX {body[:-2].hex(' ')}")
                return packet
        return None

    # -- transactions --

    def transact(self, packet: bytes, tran_nbr: int, timeout: float = 10.0) -> Packet:
        """Send a packet and wait for the response with the same transaction number."""
        self.send(packet)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                hint = ""
                if self.max_reply_frame:
                    hint = (
                        f"; a {self.model or 'logger'} reply longer than "
                        f"{self.max_reply_frame} bytes is lost, see MAX_REPLY_FRAME"
                    )
                raise PakBusTimeout(f"timed out waiting for a response from the logger{hint}")
            reply = self.receive(remaining)
            if reply is None:
                continue
            if reply.proto == PROTO_PAKCTRL and reply.msg_type == MSG_HELLO:
                self._answer_hello(reply)
                continue
            if reply.tran_nbr != tran_nbr:
                continue
            if reply.msg_type == MSG_PLEASE_WAIT:
                wait = struct.unpack_from(">H", reply.body, 1)[0]
                if self.debug:
                    print(f"  .. logger asked to wait {wait} s")
                deadline += wait
                continue
            return reply

    def _answer_hello(self, request: Packet) -> None:
        packet = header(request.src_node, self.my_node, PROTO_PAKCTRL, link_state=LINK_READY, expect_more=0x1)
        packet += struct.pack(">BBBBH", MSG_HELLO_RESP, request.tran_nbr, 0x00, 0x02, 1800)
        self.send(packet)

    def hello(self, timeout: float = 3.0, attempts: int = 3) -> bool:
        """Open the link. Returns True if the logger answered.

        Retried because the first packet written after the USB port is opened
        is regularly lost, presumably while the CDC-ACM link settles.
        """
        for _ in range(attempts):
            tran = self.next_tran()
            packet = header(self.node, self.my_node, PROTO_PAKCTRL, link_state=LINK_RING, expect_more=0x1)
            packet += struct.pack(">BBBBH", MSG_HELLO, tran, 0x00, 0x02, 1800)
            try:
                self.transact(packet, tran, timeout)
            except PakBusError:
                continue
            return True
        return False

    def get_program_stats(self, timeout: float = 10.0) -> dict:
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_GETPROGSTAT, tran, SECURITY_CODE)
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_GETPROGSTAT_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        resp_code = reply.body[0]
        stats: dict = {"resp_code": resp_code}
        if resp_code != 0:
            return stats
        data, offset = reply.body, 1
        stats["os_version"], offset = read_asciiz(data, offset)
        stats["os_signature"] = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        stats["serial_number"], offset = read_asciiz(data, offset)
        stats["power_up_program"], offset = read_asciiz(data, offset)
        stats["compile_state"] = data[offset]
        offset += 1
        stats["program_name"], offset = read_asciiz(data, offset)
        stats["program_signature"] = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        seconds, nanos = struct.unpack_from(">ll", data, offset)
        stats["compile_time"] = (seconds, nanos)
        offset += 8
        stats["compile_result"], offset = read_asciiz(data, offset)
        # The CR6 appends the model and the station name, which LoggerNet's capture showed.
        if offset < len(data):
            stats["model"], offset = read_asciiz(data, offset)
        if offset < len(data):
            stats["station_name"], offset = read_asciiz(data, offset)
        return stats

    def _pakctrl(self, msg_type: int, body: bytes, expect: int, timeout: float) -> bytes:
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_PAKCTRL) + struct.pack(">BB", msg_type, tran) + body
        reply = self.transact(packet, tran, timeout)
        if reply.proto != PROTO_PAKCTRL or reply.msg_type != expect:
            raise PakBusError(f"unexpected reply 0x{reply.msg_type:02x} to PakCtrl 0x{msg_type:02x}")
        return reply.body

    def string_settings(self, timeout: float = 10.0) -> dict:
        """PakCtrl Get String Settings: the logger's self-description, e.g. Model and the PakCtrl and
        BMP5 message codes it supports. DevConfig sends an empty name list ("all")."""
        text = self._pakctrl(MSG_GET_STRING_SETTINGS, b"\0", MSG_GET_STRING_SETTINGS_RESP, timeout)
        # The CR300 (OS 11.02) leaves out the ';' after an empty "Password=", so a name is found
        # by the next "Name=", not only by ';'.
        pairs = re.findall(r"([A-Za-z][A-Za-z0-9]*)=([^;=]*?)\s*(?=;|[A-Za-z][A-Za-z0-9]*=|$)", text.decode("latin-1"))
        return {name: value.strip("\0 ") for name, value in pairs}

    def get_settings(self, timeout: float = 20.0) -> dict:
        """DevConfig Get Settings: every setting as {id: (read_only, raw value)}, plus the header.

        Reply layout, from DevConfig reading the CR300 on 2026-11-02 (docs/pakbus.md, Settings):
        outcome (byte, 1 = ok), device type (uint2), catalogue major version (byte), minor version
        (byte), more-settings flag (byte), then per setting: id (uint2), flags and length (uint2:
        bit 14 read-only, low 14 bits the value length), value. With the more-settings flag set,
        DevConfig asked again with `0x8000 | last id received` after the security code, and the
        CR300 answered with the settings after that id: none, with the flag cleared.
        """
        settings: dict = {}
        request = struct.pack(">H", SECURITY_CODE)
        while True:
            body = self._pakctrl(MSG_DEVCONFIG_GET, request, MSG_DEVCONFIG_GET_RESP, timeout)
            if not body or body[0] != 1:
                raise PakBusError(f"get settings: outcome {body[:1].hex() or 'none'}")
            device_type, major, minor, more = struct.unpack_from(">HBBB", body, 1)
            at, last = 6, None
            while at + 4 <= len(body):
                sid, word = struct.unpack_from(">HH", body, at)
                length = word & 0x3FFF
                settings[sid] = (bool(word & 0x4000), body[at + 4 : at + 4 + length])
                at += 4 + length
                last = sid
            if not more or last is None:
                break
            request = struct.pack(">HH", SECURITY_CODE, 0x8000 | last)
        return {"device_type": device_type, "major": major, "minor": minor, "settings": settings}

    def set_settings(self, values: dict, commit: bool = True, timeout: float = 20.0) -> dict:
        """DevConfig Set Settings for {id: raw value}; returns {id: outcome} (1 = accepted).

        With `commit`, the Control "commit" action follows, as DevConfig's Apply does: the logger
        then saves the settings and **resets**, so its program restarts.
        """
        body = struct.pack(">H", SECURITY_CODE)
        for sid, raw in values.items():
            body += struct.pack(">HH", sid, len(raw)) + raw
        reply = self._pakctrl(MSG_DEVCONFIG_SET, body, MSG_DEVCONFIG_SET_RESP, timeout)
        if not reply or reply[0] != 1:
            raise PakBusError(f"set settings: outcome {reply[:1].hex() or 'none'}")
        outcomes = {
            sid: code for sid, code in (struct.unpack_from(">HB", reply, at) for at in range(1, len(reply) - 2, 3))
        }
        if commit and all(code == 1 for code in outcomes.values()):
            done = self._pakctrl(
                MSG_DEVCONFIG_CONTROL,
                struct.pack(">HB", SECURITY_CODE, DEVCONFIG_COMMIT),
                MSG_DEVCONFIG_CONTROL_RESP,
                timeout,
            )
            if not done or done[0] != 1:
                raise PakBusError(f"settings commit: outcome {done[:1].hex() or 'none'}")
        return outcomes

    # -- terminal --
    # BMP5 0x0B carries keystrokes to the logger's terminal and 0x8B its text back: security code,
    # then the bytes typed; the reply is a response byte, then text. One transaction number serves
    # the whole session, opened by a message with no bytes. From LoggerNet's Terminal Emulator on
    # the CR6, 2026-11-02 (docs/pakbus.md, Terminal).

    def terminal_open(self, attempts: int = 4) -> tuple[int, bytes]:
        """Open a terminal session. Returns (transaction number, the logger's prompt).

        The CR6 running a 10 Hz program never answered the first keystroke after the opening
        message, whether it came 0.5 s or 4 s later, and answered every one after it
        (2026-11-02). So carriage returns are sent until the logger answers.
        """
        tran = self.next_tran()
        self.terminal_write(tran, b"")
        for _ in range(attempts):
            self.terminal_write(tran, b"\r")
            prompt = self.terminal_read(tran, 1.0)
            if prompt:
                return tran, prompt
        raise PakBusError("the logger's terminal did not answer")

    def terminal_write(self, tran: int, data: bytes) -> None:
        # Marked "expect more", as LoggerNet marks them: with the default the CR6 never answered.
        packet = header(self.node, self.my_node, PROTO_BMP5, expect_more=0x1, priority=0x2)
        self.send(packet + struct.pack(">BBH", MSG_TERMINAL, tran, SECURITY_CODE) + data)

    def terminal_read(self, tran: int, timeout: float = 0.2) -> bytes:
        """Terminal text that arrives within `timeout`, b"" when none."""
        out = bytearray()
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            reply = self.receive(remaining)
            if reply is None:
                break
            if reply.proto == PROTO_PAKCTRL and reply.msg_type == MSG_HELLO:
                self._answer_hello(reply)
            elif reply.msg_type == MSG_TERMINAL_RESP and reply.tran_nbr == tran:
                out.extend(reply.body[1:])
                deadline = time.monotonic() + min(timeout, 0.05)  # gather what follows closely
        return bytes(out)

    def directory(self, timeout: float = 20.0) -> list:
        """The logger's drives and files, from `.DIR2` as LoggerNet's File Control reads it."""
        return parse_directory(self.file_upload(".DIR2", swath=983, timeout=timeout))

    def file_download(
        self, name: str, data: bytes, *, swath: int = DEFAULT_SWATH, progress=None, timeout: float = 20.0
    ) -> int:
        """Write a file to the logger. Returns the final response code (0 = ok)."""
        tran = self.next_tran()
        offset = 0
        while True:
            fragment = data[offset : offset + swath]
            close_flag = 1 if offset + len(fragment) >= len(data) else 0
            packet = header(self.node, self.my_node, PROTO_BMP5)
            packet += struct.pack(">BBH", MSG_FILEDOWNLOAD, tran, SECURITY_CODE)
            packet += asciiz(name)
            packet += struct.pack(">BBL", 0x00, close_flag, offset)
            packet += fragment
            reply = self.transact(packet, tran, timeout)
            if reply.msg_type != MSG_FILEDOWNLOAD_RESP:
                raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
            resp_code = reply.body[0]
            if resp_code != 0:
                return resp_code
            offset += len(fragment)
            if progress:
                progress(offset, len(data))
            if close_flag:
                return 0

    def get_values_raw(
        self, table: str, field: str, type_code: int, swath: int = 1, timeout: float = 10.0
    ) -> tuple[int, bytes]:
        """Read `swath` consecutive values as `type_code`. Returns (response code, raw bytes).

        The logger converts to the requested type where it can, so more than one type code will
        answer for a given field; the caller is responsible for asking for one that round-trips.
        Raw bytes are returned undecoded so that an unrecognised type code is still readable.
        """
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_GETVALUES, tran, SECURITY_CODE)
        packet += asciiz(table)
        packet += struct.pack(">B", type_code)
        packet += asciiz(field)
        packet += struct.pack(">H", swath)
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_GETVALUES_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        return reply.body[0], reply.body[1:]

    def get_values(
        self, table: str, field: str, *, value_type: str = "asciiz", swath: int = 1, timeout: float = 10.0
    ) -> list:
        """Read `swath` consecutive values from a running program. Raises on any failure.

        `value_type` is a key of VALUE_TYPES. "asciiz" is the safe default -- the logger
        formats the value itself, so it reads any type, renders a Boolean as "False" and a
        NAN as "NAN" -- but **it returns exactly one value and ignores `swath`**, so an
        array needs one of the numeric types. Asking for a type the field cannot convert to
        is not always an error: a Boolean requested as "long" comes back as response code 0
        with an empty body, so the returned length is checked rather than trusted.
        """
        if value_type not in VALUE_TYPES:
            raise ValueError(f"unknown value type {value_type!r}; expected one of {', '.join(sorted(VALUE_TYPES))}")
        if swath < 1:
            raise ValueError("swath must be at least 1")
        type_code, fmt, size = VALUE_TYPES[value_type]
        if value_type == "asciiz" and swath != 1:
            raise ValueError(
                "the asciiz type ignores swath and returns one value; use long, ieee4 or ieee8 to read an array"
            )

        resp_code, data = self.get_values_raw(table, field, type_code, swath, timeout)
        if resp_code != 0:
            meaning = GETVALUES_RESP.get(resp_code, "unrecognised response code")
            raise PakBusError(f"{table}.{field}: {meaning} (response code {resp_code})")

        if value_type == "asciiz":
            return [data.split(b"\0", 1)[0].decode("ascii", "replace")]
        assert fmt is not None and size is not None
        if len(data) != size * swath:
            raise PakBusError(
                f"{table}.{field}: asked for {swath} value(s) as {value_type} and the logger "
                f"returned {len(data)} bytes, not {size * swath} -- the field is probably not "
                f"convertible to that type, or the swath runs past the end of the array"
            )
        return [value for (value,) in struct.iter_unpack(fmt, data)]

    def set_values(self, table: str, field: str, values, *, value_type: str = "long", timeout: float = 10.0) -> None:
        """Write one or more consecutive values into a running program. Raises on any failure.

        The counterpart to `get_values`. `value_type` names the representation the values are
        sent in and must be a numeric key of VALUE_TYPES: unlike a read, a write cannot ask the
        logger to parse its own formatting, so "asciiz" is rejected. Writing more than one value
        walks consecutive array elements from `field`.
        """
        if value_type not in VALUE_TYPES:
            raise ValueError(f"unknown value type {value_type!r}; expected one of {', '.join(sorted(VALUE_TYPES))}")
        if value_type == "asciiz":
            raise ValueError(
                "set_values needs an explicit numeric type; the logger is not asked to parse its own formatting"
            )
        if isinstance(values, (int, float)):
            values = [values]
        if not values:
            raise ValueError("nothing to write")

        type_code, fmt, size = VALUE_TYPES[value_type]
        payload = struct.pack(f">{len(values)}{fmt[1:]}", *values)

        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_SETVALUES, tran, SECURITY_CODE)
        packet += asciiz(table)
        packet += struct.pack(">B", type_code)
        packet += asciiz(field)
        packet += struct.pack(">H", len(values))
        packet += payload
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_SETVALUES_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        resp_code = reply.body[0]
        if resp_code != 0:
            meaning = SETVALUES_RESP.get(resp_code, "unrecognised response code")
            raise PakBusError(f"{table}.{field}: {meaning} (response code {resp_code})")

    def clock(self, adjust_sec: int = 0, adjust_nsec: int = 0, timeout: float = 10.0) -> datetime.datetime:
        """Read the logger clock, optionally adjusting it first.

        A zero adjustment is a pure read. Returns the logger time **as it was before** any
        adjustment, which is what the logger sends back, so a set reports the clock it replaced.
        The logger has no time zone: whatever is written here is what every `RealTime` index in
        the program means, and `architecture.md` requires that to be UTC.
        """
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_CLOCK, tran, SECURITY_CODE)
        packet += struct.pack(">ll", adjust_sec, adjust_nsec)
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_CLOCK_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        resp_code = reply.body[0]
        if resp_code != 0:
            meaning = CLOCK_RESP.get(resp_code, "unrecognised response code")
            raise PakBusError(f"clock: {meaning} (response code {resp_code})")
        seconds, nanos = struct.unpack_from(">lL", reply.body, 1)
        return EPOCH + datetime.timedelta(seconds=seconds, microseconds=nanos / 1000)

    def file_upload(self, name: str, *, swath: int = 400, offset: int = 0, timeout: float = 20.0) -> bytes:
        """Read a file off the logger. Returns its bytes.

        `.TDF` is the table definitions pseudo-file, which is what makes stored records
        interpretable: without it a Collect Data response is undelimited bytes.

        Under a reply limit the swath is cut to fit, and a fragment whose reply is still lost
        to quoting is asked again at half the swath.
        """
        if self.max_reply_frame:
            swath = min(swath, reply_budget(self.max_reply_frame) - 7)
            timeout = min(timeout, 5.0)
        out = bytearray()
        # One transaction number for the whole file. A fresh transaction per fragment is
        # answered with response code 9: the logger tracks an open upload per transaction.
        # Confirmed on CR6 serial 18624 on 2026-09-05.
        tran = self.next_tran()
        while True:
            packet = header(self.node, self.my_node, PROTO_BMP5)
            packet += struct.pack(">BBH", MSG_FILEUPLOAD, tran, SECURITY_CODE)
            packet += asciiz(name)
            packet += struct.pack(">BLH", 0x00, offset, swath)
            try:
                reply = self.transact(packet, tran, timeout)
            except PakBusTimeout:
                if not self.max_reply_frame or swath <= 8:
                    raise
                swath //= 2
                continue
            if reply.msg_type != MSG_FILEUPLOAD_RESP:
                raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
            resp_code = reply.body[0]
            if resp_code != 0:
                if out:
                    break
                raise PakBusError(f"file upload {name!r}: response code {resp_code}")
            chunk = reply.body[5:]
            if not chunk:
                break
            out.extend(chunk)
            offset += len(chunk)
        return bytes(out)

    def table_definitions(self, timeout: float = 30.0) -> list:
        """Fetch and parse the logger's `.TDF`. Table numbers and signatures come from here."""
        return parse_table_definitions(self.file_upload(".TDF", timeout=timeout))

    def _collect_fragment(self, table: TableDef, count: int, timeout: float = 25.0) -> tuple[int, list, bool]:
        """One Collect Data request for the most recent `count` records.

        Returns (first record number, decoded records, more-records flag). An empty list
        means the logger declined the window, which is what it does when `count` exceeds the
        records actually stored -- it returns a bare response code rather than a short answer.
        """
        return self._collect_request(table, COLLECT_MOST_RECENT, struct.pack(">L", count), timeout)

    def _collect_range(self, table: TableDef, begin: int, end: int, timeout: float = 5.0) -> list:
        """Records `begin` up to but not including `end`, as far as the logger holds them."""
        return self._collect_request(table, COLLECT_RANGE, struct.pack(">LL", begin, end), timeout)[1]

    def _collect_raw(self, table: TableDef, mode: int, params: bytes, timeout: float) -> bytes:
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_COLLECTDATA, tran, SECURITY_CODE)
        packet += struct.pack(">BHH", mode, table.number, table.signature)
        packet += params
        packet += struct.pack(">H", 0)  # empty field list = every field
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_COLLECTDATA_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        return reply.body

    def _collect_request(self, table: TableDef, mode: int, params: bytes, timeout: float) -> tuple[int, list, bool]:
        return parse_collect_response(table, self._collect_raw(table, mode, params, timeout))

    def newest_record(self, table: TableDef, timeout: float = 10.0) -> int | None:
        """The number of the newest record the table holds, or None when it holds none.

        One 'most recent 1' request, as LoggerNet starts a collection; a record larger than a
        packet answers with its first fragment, whose header carries the number all the same.
        """
        body = self._collect_raw(table, COLLECT_MOST_RECENT, struct.pack(">L", 1), timeout)
        if is_partial_response(body):
            return parse_partial_response(body)[0]
        _, records, _ = parse_collect_response(table, body)
        return records[-1][0] if records else None

    def read_record(self, table: TableDef, number: int | None = None, timeout: float = 10.0) -> tuple | None:
        """One whole record, the newest when `number` is None: (record number, time, values).

        This is how LoggerNet's Table Monitor and Station Status read `Public` and `Status`. A
        record larger than a packet comes back in fragments, the rest read with partial-record
        requests from the byte offset reached (docs/pakbus.md, "Partial records").
        """
        if number is None:
            body = self._collect_raw(table, COLLECT_MOST_RECENT, struct.pack(">L", 1), timeout)
        else:
            body = self._collect_raw(table, COLLECT_RANGE, struct.pack(">LL", number, number + 1), timeout)
        if not is_partial_response(body):
            _, records, _ = parse_collect_response(table, body)
            return records[-1] if records else None
        record, offset, image = parse_partial_response(body)
        image = bytearray(image) if offset == 0 else bytearray()
        if offset != 0:
            raise PakBusError(f"{table.name}: first fragment of record {record} starts at byte {offset}")
        size = 8 + table.record_size
        while len(image) < size:
            body = self._collect_raw(table, COLLECT_PARTIAL, struct.pack(">LL", record, len(image)), timeout)
            got, at, fragment = parse_partial_response(body)
            if got != record or at != len(image) or not fragment:
                raise PakBusError(f"{table.name}: record {record} fragment at {at} ({len(fragment)} B) did not follow")
            image.extend(fragment)
        secs, nanos = struct.unpack_from(">LL", image)
        when = EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
        return record, when, decode_record(table, bytes(image[8:size]))

    def collect_range(
        self, table: TableDef, begin: int, end: int, progress=None, timeout: float = 10.0, depth: int = 2
    ) -> list:
        """Records `begin` up to but not including `end`: [(record number, time, values)].

        LoggerNet's method (capture of an "All the Data" collection from the CR300, 2026-11-02):
        ask for the whole remaining range with mode 0x06, take the records the logger fits into
        its reply, and ask again from the record after the last one received, keeping the same
        end. The end is fixed, so table growth during a long collection cannot shift the window,
        and a `begin` below the oldest record held is answered from the oldest record.
        `progress(records so far, records asked for)` is called after every reply.

        Beyond LoggerNet: once the first reply shows how many records fit a reply, the rest is
        asked for in windows of that many, `depth` requests in flight at once. The CR1000X
        answers them back to back, which took `FinApp_Poll` from 1026 to 1600 records/s; the
        CR300 spends its ~70 ms per request inside the logger and gains nothing (2026-11-02). A
        window answered short is asked again from where its reply stopped.
        """
        body = self._collect_raw(table, COLLECT_RANGE, struct.pack(">LL", begin, end), timeout)
        if is_partial_response(body):
            # Records larger than a reply: read them one at a time, starting from the record the
            # logger answered with, which is the oldest it holds at or after `begin`.
            first = parse_partial_response(body)[0]
            whole = []
            for number in range(first, end):
                record = self.read_record(table, number, timeout)
                if record is not None:
                    whole.append(record)
                if progress:
                    progress(len(whole), end - begin)
            return whole
        _, page, _ = parse_collect_response(table, body)
        page = [record for record in page if begin <= record[0] < end]
        if not page:
            return []
        got = {record[0]: record for record in page}
        begin = page[0][0]  # the oldest record held, when the range reached below it
        per_reply = len(page)
        windows = [(s, min(s + per_reply, end)) for s in range(page[-1][0] + 1, end, per_reply)]
        if progress:
            progress(len(got), end - begin)
        inflight: dict[int, tuple[int, int]] = {}
        deadline = time.monotonic() + timeout
        while windows or inflight:
            while windows and len(inflight) < max(1, depth):
                lo, hi = windows.pop(0)
                tran = self.next_tran()
                packet = header(self.node, self.my_node, PROTO_BMP5)
                packet += struct.pack(">BBH", MSG_COLLECTDATA, tran, SECURITY_CODE)
                packet += struct.pack(">BHH", COLLECT_RANGE, table.number, table.signature)
                packet += struct.pack(">LLH", lo, hi, 0)
                self.send(packet)
                inflight[tran] = (lo, hi)
            reply = self.receive(max(0.0, deadline - time.monotonic()))
            if reply is None:
                raise PakBusTimeout(f"{table.name}: no reply to {len(inflight)} collect request(s) in flight")
            if reply.tran_nbr not in inflight or reply.msg_type != MSG_COLLECTDATA_RESP:
                continue
            deadline = time.monotonic() + timeout
            lo, hi = inflight.pop(reply.tran_nbr)
            _, page, _ = parse_collect_response(table, reply.body)
            page = [record for record in page if lo <= record[0] < hi]
            got.update((record[0], record) for record in page)
            # An empty reply means the logger no longer holds the window, overwritten while the
            # collection ran; there is nothing to ask for again.
            if page and page[-1][0] + 1 < hi:
                windows.insert(0, (page[-1][0] + 1, hi))
            if progress:
                progress(len(got), end - begin)
        return [got[number] for number in sorted(got)]

    def collect_recent(self, table: TableDef, count: int, timeout: float = 25.0, progress=None) -> list:
        """Read up to the most recent `count` records. Returns [(record number, time, values)].

        The newest record number is read first and the range up to it collected with
        `collect_range`, as LoggerNet does. A logger with a reply limit is read by
        `_collect_recent_limited` instead.
        """
        if self.max_reply_frame:
            return self._collect_recent_limited(table, count, self.max_reply_frame)
        if table.size <= 1:
            # Status, Public and DataTableInfo hold one record, made when it is asked for, and
            # answer a range request with nothing at all (CR6, 2026-11-02); LoggerNet reads them
            # with 'most recent 1' only.
            record = self.read_record(table, timeout=timeout)
            return [record] if record else []
        newest = self.newest_record(table)
        if newest is None:
            return []
        # Not capped at the declared table size: the CR300 (OS 11.03) held about 30 800 records of a
        # table its .TDF and DataTableInfo both declared as 50 (2026-11-02). A range reaching
        # below the oldest record held is answered from the oldest one.
        want = max(1, count)
        return self.collect_range(table, max(0, newest + 1 - want), newest + 1, progress)

    def _collect_recent_limited(self, table: TableDef, count: int, max_frame: int) -> list:
        """`collect_recent` for a logger whose replies must fit `max_reply_frame`.

        The newest record is found with a one-record 'most recent' request, then fixed
        record-number ranges sized to the reply limit are read backwards from it. Fixed ranges
        cannot shift while the table grows. A range whose reply is lost anyway is split, and a
        range the logger declines is walked one record at a time, since it reaches past the
        oldest record held.
        """
        per_record = table.record_size + (0 if table.is_interval else 8)
        fixed = 1 + 8 + (8 if table.is_interval else 0) + 1
        span = (reply_budget(max_frame) - fixed) // per_record
        if span < 1:
            raise PakBusError(
                f"{table.name}: one {table.record_size}-byte record does not fit this "
                f"{self.model or 'logger'}'s {self.max_reply_frame}-byte reply; use Get Values"
            )

        _, newest, _ = self._collect_fragment(table, 1, timeout=5.0)
        if not newest:
            return []
        end = newest[-1][0] + 1
        want = max(1, min(count, table.size))
        start = max(0, end - want)

        def read(begin: int, stop: int) -> tuple[list, bool]:
            """Records in [begin, stop) and whether the oldest held record was reached."""
            try:
                records = self._collect_range(table, begin, stop)
            except PakBusTimeout:
                if stop - begin == 1:
                    raise
                mid = (begin + stop) // 2
                upper, exhausted = read(mid, stop)
                if exhausted:
                    return upper, True
                lower, exhausted = read(begin, mid)
                return lower + upper, exhausted
            if records or stop - begin == 1:
                return records, not records
            held = []
            for number in range(stop - 1, begin - 1, -1):
                single = self._collect_range(table, number, number + 1)
                if not single:
                    return held, True
                held = single + held
            return held, False

        pages: list = []
        while end > start:
            begin = max(start, end - span)
            records, exhausted = read(begin, end)
            pages.append([r for r in records if r[0] < end])
            if exhausted:
                break
            end = begin
        return [record for page in reversed(pages) for record in page]

    def file_control(self, name: str, command: int, timeout: float = 20.0) -> tuple[int, int]:
        """Run a File Control command. Returns (response code, hold-off seconds)."""
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_FILECONTROL, tran, SECURITY_CODE)
        packet += asciiz(name)
        packet += struct.pack(">B", command)
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_FILECONTROL_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        resp_code = reply.body[0]
        hold_off = struct.unpack_from(">H", reply.body, 1)[0] if len(reply.body) >= 3 else 0
        return resp_code, hold_off
