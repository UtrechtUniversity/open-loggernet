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
import struct
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


def find_port() -> str | None:
    """Locate a Campbell logger's USB serial port by its USB manufacturer string, on Linux or macOS.

    Looked up afresh on every call: the logger re-enumerates when it compiles a newly sent
    program and can come back on a different node (docs/pakbus.md).
    """
    ports = sorted(list_ports.comports(), key=lambda port: port.device)
    for port in ports:
        if "campbell" in (port.manufacturer or "").lower():
            return port.device
    for port in ports:
        if "ttyACM" in port.device or "usbmodem" in port.device:
            return port.device
    return None


def require_port() -> str:
    """`find_port`, raising when no logger is attached."""
    port = find_port()
    if port is None:
        raise PakBusError("no Campbell logger serial port found")
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

# Collect Data modes. 'Most recent' serves loggers without a reply limit; modes 3 and 4 were
# both answered with response code 7 by the CR6 on 2026-09-05. A record-number range serves
# loggers with a reply limit, because 'most recent' fills a reply to the logger's own size.
COLLECT_MOST_RECENT = 0x05
COLLECT_RANGE = 0x06
# Records each follow-up request re-asks for, so table growth between requests cannot
# open a gap. Kept below the smallest page (TString_Data, about four records).
COLLECT_OVERLAP = 2

# File Control command codes. Only the codes this tool uses are named; the full
# set is listed in the CR6 help under Web Server/API Commands -> FileControl,
# whose action codes are the same set.
FILECTL_COMPILE_RUN_POWERUP = 1  # compile, run, and mark as run-on-power-up
FILECTL_MARK_POWERUP = 2
FILECTL_DELETE = 4
FILECTL_COMPILE_RUN_KEEP_DATA = 6
FILECTL_STOP = 7
FILECTL_STOP_DELETE_DATA = 8
FILECTL_COMPILE_RUN_KEEP_POWERUP = 10

DEFAULT_SWATH = 256  # file bytes per download packet; docs/lessons.md

# The logger reports time as signed seconds and nanoseconds since this epoch.
EPOCH = datetime.datetime(1990, 1, 1, tzinfo=datetime.UTC)

# --- Get Values data types -------------------------------------------------
# The logger converts a field to whatever type is asked for, where it can, so
# the type code chooses the representation rather than describing the field.
# These four were established on the bench (2026-08-31) by reading Public
# variables of known type and value; docs/pakbus.md
# records how. Everything below is what this repository has confirmed, not the
# full BMP5 type table.
TYPE_INT4 = 6       # 4-byte signed, big-endian. Exact for `As Long`.
TYPE_IEEE4B = 9     # 4-byte IEEE, big-endian. Exact for Float/IEEE4.
TYPE_ASCIIZ = 16    # the logger's own formatting, NUL-terminated.
TYPE_IEEE8B = 18    # 8-byte IEEE, big-endian.

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
    11: ("ascii", 1),      # length comes from the field's piece count
    14: ("nsec", 8),       # timestamp: seconds and nanoseconds since 1990
    17: ("bool", 1),
    18: ("ieee8", 8),
    28: ("bool", 4),       # CRBasic Boolean
}

# struct format per numeric record type; bool, nsec and ascii are decoded by hand
RECORD_FORMATS = {"uint1": "B", "uint2": "H", "uint4": "L", "int1": "b", "int2": "h",
                  "int4": "l", "fp2": "H", "ieee4": "f", "ieee8": "d"}

# Types 2, 6, 7, 9 and 11 were confirmed by decoding known values back out of DailyPower,
# Shunt_Data, PT1000_Data and TString_Data and matching them against the serial trace.
# Type 28 is a CRBasic `Boolean` stored with `Sample(...,Boolean)`, 4 bytes wide: decoded out of
# Freezer_tester.CR6's Battery_Data and FinApp_Starts on 2026-09-22, where widths 1 and 2 broke
# the record framing and 4 gave the Battery() and PanelTemp values beside it.
# Type 14 is INFERRED, not decoded: it appears only in Status, Public and DataTableInfo, whose
# records are larger than one packet and so cannot be collected here. It is a timestamp because
# its instances are StartTime, LastSystemScan, LastSlowScan and LastPublish; its width is untested.

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
    return sign * mantissa / (10 ** exponent)


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
        interval_sec = struct.unpack_from(">L", data, offset)[0]
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
            fields.append(FieldDef(field_name, raw_type & 0x7F, bool(raw_type & 0x80),
                                   processing, units, description, begin_index, piece,
                                   dimensions))
        offset += 1  # field list terminator
        tables.append(TableDef(number, name, size, time_type, interval_sec, fields,
                               signature(data[start:offset])))
    return tables


def decode_record(table: TableDef, buf: bytes) -> dict:
    """Unpack one packed record into {field name: value or list of values}."""
    out: dict[str, object] = {}
    at = 0
    for field in table.fields:
        if field.type_code not in RECORD_TYPES:
            raise PakBusError(
                f"{table.name}.{field.name}: field type {field.type_code} is not decoded; "
                f"add it to RECORD_TYPES once its layout is confirmed on hardware")
        kind, width = RECORD_TYPES[field.type_code]
        if kind == "ascii":
            raw = buf[at:at + field.piece]
            out[field.name] = raw.split(b"\0", 1)[0].decode("ascii", "replace")
            at += field.piece
            continue
        raw = buf[at:at + width * field.count]
        at += width * field.count
        if kind == "bool":
            values = [byte != 0 for byte in raw[::width]]
        elif kind == "nsec":
            values = [EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
                      for secs, nanos in struct.iter_unpack(">LL", raw)]
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
            raise PakBusError(f"{table.name}: partial-record block, which is not implemented")
        if first_begin is None:
            first_begin = begin
        stamp = EPOCH
        for index in range(nbr):
            if table.is_interval:
                if index == 0:
                    secs, nanos = struct.unpack_from(">LL", body, at)
                    at += 8
                    stamp = EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
                when = stamp + datetime.timedelta(seconds=table.interval_sec * index)
            else:
                secs, nanos = struct.unpack_from(">LL", body, at)
                at += 8
                when = EPOCH + datetime.timedelta(seconds=secs, microseconds=nanos / 1000)
            chunk = body[at:at + table.record_size]
            at += table.record_size
            if len(chunk) < table.record_size or at > end:
                raise PakBusError(
                    f"{table.name}: record {begin + index} is {table.record_size} bytes and did "
                    f"not fit the response. Records larger than one packet need Collect Data's "
                    f"partial-record mode, which is not implemented; read these fields with "
                    f"read_values.py instead.")
            records.append((begin + index, when, decode_record(table, chunk)))
    return (first_begin or 0), records, bool(body[end])


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

    @property
    def record_size(self) -> int:
        return sum(f.size for f in self.fields)

    @property
    def is_interval(self) -> bool:
        """Interval tables carry one timestamp per fragment; event tables one per record."""
        return self.interval_sec > 0


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
        return out.take_bytes()


# Largest reply frame, SYNC bytes included, that reaches the host intact, by USB product string.
# Only the first 64-byte USB packet of a CR300 reply reached this client; the CR350 entry is
# assumed. LoggerNet is much faster on the same logger, so this is a workaround pending research:
# docs/pakbus.md, "CR300 replies cut at 64 bytes". Models not listed
# (CR6, CR1000X) need none; the CR6 answers with replies of about 1 KB.
MAX_REPLY_FRAME = {"CR300": 64, "CR350": 64}

# Frame bytes outside a reply's message body: two SYNCs, the header, the message type and
# transaction number, and the signature nullifier.
FRAME_OVERHEAD = 2 + 8 + 2 + 2


class PakBusError(Exception):
    pass


class PakBusTimeout(PakBusError):
    """No reply arrived; under a reply limit, usually a reply that outgrew it."""


def port_model(port: str) -> str | None:
    """The logger model a USB serial port reports as its product string, e.g. "CR300"."""
    for info in list_ports.comports():
        if info.device == port:
            return (info.product or "").strip().upper() or None
    return None


def reply_budget(max_frame: int) -> int:
    """Message body bytes a reply may carry and still fit `max_frame`.

    An eighth is held back for quoting, where each 0xBC or 0xBD goes out as two bytes. A reply
    that needs more than that is lost, and the callers split and retry it.
    """
    return (max_frame - FRAME_OVERHEAD) * 7 // 8


class PakBusLink:
    """PakBus over a serial port (the logger USB port speaks PakBus directly)."""

    def __init__(self, port: str, baudrate: int = 115200, *, node: int = 1,
                 my_node: int = 4094, max_reply_frame: int | None = None,
                 debug: bool = False):
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
        except (OSError, serial.SerialException):
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
            chunk = self.port.read(512)
            if chunk:
                self._buf.extend(chunk)
            while SYNC in self._buf:
                start = self._buf.index(SYNC)
                end = self._buf.find(SYNC, start + 1)
                if end < 0:
                    del self._buf[:start]
                    break
                candidate = bytes(self._buf[start + 1:end])
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
                    hint = (f"; a {self.model or 'logger'} reply longer than "
                            f"{self.max_reply_frame} bytes is lost, see MAX_REPLY_FRAME")
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
        packet = header(request.src_node, self.my_node, PROTO_PAKCTRL,
                        link_state=LINK_READY, expect_more=0x1)
        packet += struct.pack(">BBBBH", MSG_HELLO_RESP, request.tran_nbr, 0x00, 0x02, 1800)
        self.send(packet)

    def hello(self, timeout: float = 3.0, attempts: int = 3) -> bool:
        """Open the link. Returns True if the logger answered.

        Retried because the first packet written after the USB port is opened
        is regularly lost, presumably while the CDC-ACM link settles.
        """
        for _ in range(attempts):
            tran = self.next_tran()
            packet = header(self.node, self.my_node, PROTO_PAKCTRL,
                            link_state=LINK_RING, expect_more=0x1)
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
        return stats

    def file_download(self, name: str, data: bytes, *, swath: int = DEFAULT_SWATH,
                      progress=None, timeout: float = 20.0) -> int:
        """Write a file to the logger. Returns the final response code (0 = ok)."""
        tran = self.next_tran()
        offset = 0
        while True:
            fragment = data[offset:offset + swath]
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

    def get_values_raw(self, table: str, field: str, type_code: int, swath: int = 1,
                       timeout: float = 10.0) -> tuple[int, bytes]:
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

    def get_values(self, table: str, field: str, *, value_type: str = "asciiz",
                   swath: int = 1, timeout: float = 10.0) -> list:
        """Read `swath` consecutive values from a running program. Raises on any failure.

        `value_type` is a key of VALUE_TYPES. "asciiz" is the safe default -- the logger
        formats the value itself, so it reads any type, renders a Boolean as "False" and a
        NAN as "NAN" -- but **it returns exactly one value and ignores `swath`**, so an
        array needs one of the numeric types. Asking for a type the field cannot convert to
        is not always an error: a Boolean requested as "long" comes back as response code 0
        with an empty body, so the returned length is checked rather than trusted.
        """
        if value_type not in VALUE_TYPES:
            raise ValueError(f"unknown value type {value_type!r}; "
                             f"expected one of {', '.join(sorted(VALUE_TYPES))}")
        if swath < 1:
            raise ValueError("swath must be at least 1")
        type_code, fmt, size = VALUE_TYPES[value_type]
        if value_type == "asciiz" and swath != 1:
            raise ValueError("the asciiz type ignores swath and returns one value; "
                             "use long, ieee4 or ieee8 to read an array")

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
                f"convertible to that type, or the swath runs past the end of the array")
        return [value for (value,) in struct.iter_unpack(fmt, data)]

    def set_values(self, table: str, field: str, values, *, value_type: str = "long",
                   timeout: float = 10.0) -> None:
        """Write one or more consecutive values into a running program. Raises on any failure.

        The counterpart to `get_values`. `value_type` names the representation the values are
        sent in and must be a numeric key of VALUE_TYPES: unlike a read, a write cannot ask the
        logger to parse its own formatting, so "asciiz" is rejected. Writing more than one value
        walks consecutive array elements from `field`.
        """
        if value_type not in VALUE_TYPES:
            raise ValueError(f"unknown value type {value_type!r}; "
                             f"expected one of {', '.join(sorted(VALUE_TYPES))}")
        if value_type == "asciiz":
            raise ValueError("set_values needs an explicit numeric type; "
                             "the logger is not asked to parse its own formatting")
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

    def clock(self, adjust_sec: int = 0, adjust_nsec: int = 0,
              timeout: float = 10.0) -> datetime.datetime:
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

    def file_upload(self, name: str, *, swath: int = 400, offset: int = 0,
                    timeout: float = 20.0) -> bytes:
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

    def _collect_fragment(self, table: TableDef, count: int,
                          timeout: float = 25.0) -> tuple[int, list, bool]:
        """One Collect Data request for the most recent `count` records.

        Returns (first record number, decoded records, more-records flag). An empty list
        means the logger declined the window, which is what it does when `count` exceeds the
        records actually stored -- it returns a bare response code rather than a short answer.
        """
        return self._collect_request(table, COLLECT_MOST_RECENT, struct.pack(">L", count),
                                     timeout)

    def _collect_range(self, table: TableDef, begin: int, end: int,
                       timeout: float = 5.0) -> list:
        """Records `begin` up to but not including `end`, as far as the logger holds them."""
        return self._collect_request(table, COLLECT_RANGE, struct.pack(">LL", begin, end),
                                     timeout)[1]

    def _collect_request(self, table: TableDef, mode: int, params: bytes,
                         timeout: float) -> tuple[int, list, bool]:
        tran = self.next_tran()
        packet = header(self.node, self.my_node, PROTO_BMP5)
        packet += struct.pack(">BBH", MSG_COLLECTDATA, tran, SECURITY_CODE)
        packet += struct.pack(">BHH", mode, table.number, table.signature)
        packet += params
        packet += struct.pack(">H", 0)  # empty field list = every field
        reply = self.transact(packet, tran, timeout)
        if reply.msg_type != MSG_COLLECTDATA_RESP:
            raise PakBusError(f"unexpected message type 0x{reply.msg_type:02x}")
        return parse_collect_response(table, reply.body)

    def collect_recent(self, table: TableDef, count: int, timeout: float = 25.0) -> list:
        """Read up to the most recent `count` records. Returns [(record number, time, values)].

        A logger with a reply limit is read by `_collect_recent_limited` instead.

        Two logger behaviours shape this. Asking for more records than are stored returns
        nothing at all rather than a short answer, so the largest satisfiable window is found
        by halving. A window larger than one packet is answered in part, so the rest is walked
        forward by anchoring a smaller window on the records already seen.
        """
        if self.max_reply_frame:
            return self._collect_recent_limited(table, count, self.max_reply_frame)

        # Find the largest window the logger will answer. Halving alone undershoots -- asking
        # for 14 when 6 exist would settle on 3 and silently drop half the records -- so the
        # halving is followed by a binary search between the last refusal and the last answer.
        want = max(1, min(count, table.size))
        lo, hi = 0, want
        best: tuple | None = None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            begin, records, more = self._collect_fragment(table, mid, timeout)
            if records:
                lo, best = mid, (mid, begin, records, more)
            else:
                hi = mid - 1
        if best is None:
            return []
        want, begin, records, more = best

        # A "most recent N" window is measured from the logger's newest record at the moment
        # of each request, and the logger keeps writing while a long collection runs. Holding
        # the first request's idea of "newest" fixed therefore shifts every later page forward
        # and drops one record at a page boundary each time the table grows -- nine records
        # were lost that way from a ten-day StationStatus collection on 2026-09-14. So the
        # anchor is re-derived from every page, each request overlaps what is already held,
        # and a page that still starts past the next expected record raises rather than
        # leaving a silent gap.
        newest = begin + want - 1
        collected = list(records)
        while more:
            last = collected[-1][0]
            asked = newest - last + COLLECT_OVERLAP
            page_begin, page, more = self._collect_fragment(table, asked, timeout)
            if not page:
                break
            newest = page_begin + asked - 1
            fresh = [r for r in page if r[0] > last]
            if not fresh:
                if more:
                    raise PakBusError(f"{table.name}: collection made no progress after record {last}")
                break
            if fresh[0][0] != last + 1:
                raise PakBusError(
                    f"{table.name}: records {last + 1}..{fresh[0][0] - 1} were skipped; the table "
                    f"grew by more than {COLLECT_OVERLAP} records between two requests")
            collected.extend(fresh)
        return collected

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
                f"{self.model or 'logger'}'s {self.max_reply_frame}-byte reply; use Get Values")

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
