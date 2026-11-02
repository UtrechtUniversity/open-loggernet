# PakBus / BMP5 — the part of the protocol `src/openloggernet/pakbus.py` implements

Reference for the wire format used to send a program to the CR6 over its USB port and to read
values back off it. Only the five transactions the tooling needs are covered.

## Sources, and their standing

Campbell's own *BMP5 Transparent Commands* manual is the authority for this format, but **no copy
is available** — it is not in this repository, not installed with LoggerNet or Device Configuration
Utility on the workstation, and every published URL for it returned 404 on 2026-08-26. What is
recorded here therefore comes from two independent open-source implementations that cite that
manual section by section, cross-checked against each other:

* PyPak — `pakbus.py`, Dietrich Feist, Max Planck Institute for Biogeochemistry (GPL), which cites
  *BMP5 Transparent Commands, Rev. 9/08* section by section: https://github.com/kitplummer/PyPak
* PbCdlComm: https://github.com/kalkocz/PbCdlComm

and, for the File Control command codes, the CRBasic help topic `Info/webserverapicommands1.htm` (shipped with Campbell's CRBasic Editor, not redistributed here), whose
`FileControl` action codes are the same set exposed over HTTP.

`src/openloggernet/pakbus.py` was written from these notes, not copied from either implementation.

**Every field below was confirmed against the CR6 on the bench (serial 18624, OS CR6.14.5.1) on
2026-08-26**: the logger accepted the packets, and its replies verified under our own signature
implementation. Where something is *not* confirmed, it says so.

## Framing

A packet is delimited by `0xBD` bytes. Inside the frame, `0xBC` and `0xBD` are escaped as
`0xBC 0xDC` and `0xBC 0xDD`. The frame body is `header + message + 2-byte signature nullifier`.

The signature is Campbell's 16-bit rolling checksum, seeded `0xAAAA`; the nullifier is the two
bytes that drive the signature of the whole body to zero. A received packet is valid when the
signature computed over the body *including* its nullifier is 0 — which is how the implementation
is checked, and how the logger's replies confirmed it.

Bytes that fall outside a frame are ignored, so a program writing debug output to `ComUSB` does
not disturb PakBus traffic on the same USB port.

## Header — 8 bytes, four big-endian 16-bit words

| Word | Bits 15..12 | Bits 11..0 |
|---|---|---|
| 1 | link state | destination physical address |
| 2 | expect-more (2 b) + priority (2 b) | source physical address |
| 3 | higher-level protocol | destination node id |
| 4 | hop count | source node id |

Link state `0x9` = ring, `0xA` = ready. Protocol `0x0` = PakCtrl, `0x1` = BMP5. The CR6's default
PakBus address is 1; the flasher uses 4094 for itself, the address LoggerNet reserves for a client.

## Messages

Every message begins with a message type byte and a transaction number byte. A response carries
the transaction number of its command. A `0xA1` "please wait" response means the logger needs more
time and carries the number of seconds to wait — observed from this logger.

| Transaction | Command | Response | Command body after the two leading bytes |
|---|---|---|---|
| Hello | `0x09` | `0x89` | is-router (byte), hop metric (byte), verify interval (uint2) |
| Get programming statistics | `0x18` | `0x98` | security code (uint2) |
| File download | `0x1C` | `0x9C` | security (uint2), file name (asciiz), attribute (byte), close flag (byte), file offset (uint4), file data |
| File control | `0x1E` | `0x9E` | security (uint2), file name (asciiz), command (byte) |
| Get values | `0x1A` | `0x9A` | security (uint2), table name (asciiz), type code (byte), field name (asciiz), swath (uint2) |
| Set values | `0x1B` | `0x9B` | security (uint2), table name (asciiz), type code (byte), field name (asciiz), swath (uint2), values |
| Clock | `0x17` | `0x97` | security (uint2), adjustment (int4 seconds, int4 nanoseconds) |
| File upload | `0x1D` | `0x9D` | security (uint2), file name (asciiz), close flag (byte), file offset (uint4), swath (uint2) |
| Collect data | `0x09` | `0x89` | security (uint2), mode (byte), table number (uint2), table signature (uint2), mode parameters, field-number list terminated by a `0` uint2 |

Every BMP5 command carries a 16-bit security code. `src/openloggernet/pakbus.py` always sends 0 and has no
way to send anything else — see `README.md` for why.

Responses:

* File download → response code (byte), file offset (uint4). Response code 0 = accepted.
* File control → response code (byte), hold-off seconds (uint2). This logger asks for 6 s after a
  compile-and-run.
* Get programming statistics → response code (byte), then OS version (asciiz), OS signature
  (uint2), serial number (asciiz), power-up program (asciiz), compile state (byte), program name
  (asciiz), program signature (uint2), compile time (2 × int4), **compile result (asciiz)**. The
  compile result is the same text `CR6Comp.exe` prints, produced by the logger itself, so it is
  what a flashing tool should report.

* Get values → response code (byte), then the values themselves, `swath` of them back to back in
  the requested type. Response code 0 = ok, 16 = invalid table or field name, 17 = data type
  conversion not supported — all three provoked on this logger. The open-source implementations
  also list 1 = permission denied and 20 = memory bounds violation; neither has been seen here.

### Get values data types

**Established on the bench, 2026-08-31**, by reading `Public` variables of known type and value
from a running `Main.CR6` on CR6 serial 18624 — sweeping type codes 1–27 against `PTemp`
(a Float reading 20.53) and against a `Boolean`, and checking which decoded correctly:

| Code | Type | Evidence |
|---|---|---|
| 6 | Int4, signed big-endian | `PTemp` → `00 00 00 14` = 20, correctly truncated; `wdog_timer` (`As Long`) reads exactly |
| 9 | IEEE4B, 4-byte IEEE big-endian | `PTemp` → `41 a4 3e 10` = 20.530304 |
| 16 | ASCIIZ, the logger's own formatting | `PTemp` → `"20.530300\0"`; a `Boolean` → `"False\0"`; a `NAN` → `"NAN\0"` |
| 18 | IEEE8B, 8-byte IEEE big-endian | `PTemp` → 20.530304 |

Codes 3, 4, 5, 7, 8, 10, 12, 13, 15, 19, 22, 23, 26 and 27 all return response code 17 for a Float, so
they are real codes the logger knows and refuses to convert to, not gaps. Code 24 returned the
IEEE4 bytes **little-endian**; code 11 returned a single ASCII character (the first of the ASCIIZ
rendering). Neither is used.

**Two behaviours that will bite:**

* **ASCIIZ ignores `swath` and always returns exactly one value.** Asking for 9 elements of a
  `Long` array as ASCIIZ returns the first element and nothing else, with response code 0. Arrays
  need one of the numeric types, which do honour `swath`.
* **A conversion the logger cannot do is not always response code 17.** A `Boolean` requested as
  Int4 comes back as **response code 0 with an empty body**. `pakbus.get_values` therefore checks
  the returned byte count against `swath × size` rather than trusting the response code, and a
  `Boolean` is best read as ASCIIZ (`"True"`/`"False"`) or as IEEE4 (0.0 / non-zero).

The `Public` table is addressable by that name, and so are the `Status` table and any of the
program's own data tables — `Public.PTemp`, `Status.SkippedScan` and `StationStatus.PTemp` all
read correctly. An array element can be named directly, e.g. `Acclima_VWC(2)`.

A file larger than one packet is sent as a series of file-download commands that share one
transaction number, each with its byte offset, the last one with the close flag set. The flasher
sends 256 bytes per packet; a CR1000X ignored 512 (`docs/lessons.md`).

### File control command codes

Only code 1 is used by the flasher and therefore the only one confirmed here. The full set is in
the CRBasic help topic `Info/webserverapicommands1.htm` (shipped with Campbell's CRBasic Editor, not redistributed here) → FileControl.

| Code | Meaning |
|---|---|
| 1 | compile and run, and mark as run-on-power-up |
| 2 | mark as run-on-power-up |
| 4 | delete the file |
| 6 | compile and run without deleting data tables |
| 7 | stop the running program |
| 10 | compile and run, leaving the power-up program alone |

## CR6 behaviour observed while implementing this

* **The logger disconnects and re-enumerates its USB device when it compiles a newly sent
  program.** The host's serial port goes away mid-transaction (`Errno 5` on write) and comes back
  a few seconds later, possibly on a different `/dev/ttyACM*` node — observed moving from
  `ttyACM1` to `ttyACM0`. Anything that flashes over USB has to reopen the port afterwards, and
  should find it again through `/dev/serial/by-id/`, not by node name.
* **The first packet written after opening the port is sometimes lost**, so the opening hello is
  retried. Observed once, on the first connection after a long idle; not reproduced since.
* A program's `IncludeSection` libraries must already be on `CPU:` when the program is compiled,
  so they are sent first.
* PakBus and program debug output share the USB port without interfering.

## CR300 replies cut at 64 bytes

**Observed on a CR300 (serial 33590, OS CR300.Std.11.02) over USB on Linux `cdc_acm`, on
2026-10-20. Needs further research before it is treated as a property of the logger.**

* A reply frame longer than 64 bytes never arrives whole. The host receives exactly the first 64
  bytes, one full-speed USB packet, and nothing more; the frame fails its signature and the request
  times out. Seen on Get Program Stats and on File Upload with 400- and 128-byte swaths. Every
  reply of 64 bytes or less arrived intact.
* Asserting or clearing RTS, and writing further bytes to the logger after the first packet, did
  not release the rest. With DTR cleared the logger sent nothing at all.
* Collect Data mode `0x06`, "records P1 up to but not including P2", works on this logger. A range
  reaching below the oldest record held was not tested: record 0 was still stored.
* Hello, Clock and Get Values replies are short and work unchanged.

`pakbus` works around this with `MAX_REPLY_FRAME`: it reads the model from the USB product
string and, for the CR300 and CR350, sizes File Upload swaths and Collect Data ranges to fit 64
bytes. That collects about 39 records a second: 126 042 one-minute `Table1` records took 54
minutes. **LoggerNet collects the same logger much faster** (human report, 2026-10-20), so the
limit may be an artefact of this client or of the host side -- something LoggerNet negotiates, or a
host USB-serial difference -- rather than a hard CR300 limit. That is inference, not tested. Until that is understood the 64-byte budget is a workaround, and
the CR350 entry is an assumption from the shared platform, not an observation.

**On Windows the same CR300 sends whole replies.** Observed 2026-11-02, CR300 serial 33590, OS
`CR300.Std.11.02`, Windows 11 `usbser` on COM4: Get Prog Stats came back intact, File Upload of
the 1879-byte `.TDF` worked at a 1000-byte swath, and 3000 `Table1` records were collected in 2.8 s
(1060 records/s) with no reply limit applied. So the cut is a property of the Linux host path, not
of the logger. Windows reports the model only in the port description (`CR300 (COM4)`); its USB
product string is empty, so `MAX_REPLY_FRAME` is not applied there.

## Reply latency

`PakBusLink.receive` used to call `serial.read(512)` with a 0.1 s port timeout, which returns only
when 512 bytes have arrived or the timeout expires, so every reply shorter than 512 bytes cost
100 ms. Measured on the CR1000X (serial 59624) on Windows on 2026-11-02: Hello, Clock and Collect
Data all took 0.10–0.11 s; reading what is buffered instead brought them to 1–4 ms, and collecting
3000 `FinApp_Poll` records (240 B each, three per reply) went from 321 s to 9 s.

## Reading stored records

**Established on this logger on 2026-09-05.** Three things are needed: the table definitions, a
table's number and signature, and the record framing.

### Table definitions, `.TDF`

`.TDF` is a pseudo-file read with File Upload. Its layout, worked out byte by byte by aligning the
file against table and field names already known from the CRBasic source:

```
version (byte)
per table:
    name (asciiz), size (uint4), time type (byte),
    time into interval (uint4 seconds, uint4 nanoseconds),
    interval             (uint4 seconds, uint4 nanoseconds)
    per field, until a field-type byte of 0:
        type (byte; 0x80 = read only), name, alias, processing, units, description
        (all asciiz), begin index (uint4), piece (uint4),
        dimensions (uint4 list terminated by a 0)
    0 (byte) ends the field list
```

The parse is self-checking: it must consume the file exactly. This logger's 11235-byte `.TDF`
yields 15 tables, and `TString_Data.TString_Temp_C` comes back as one field with `piece = 48` and
`dimensions = [48]`, matching `Const TStringCount = 48`.

A table's **number** is its 1-based position in the file, and its **signature** is the Campbell
signature over that table's own bytes, from its name through its field-list terminator.

### File Upload needs one transaction for the whole file

A fresh transaction number for each fragment is answered with **response code 9**; reusing the
first transaction number returns fragment after fragment. The logger tracks an open upload per
transaction.

### Collect Data

On the CR6 only mode `0x05`, "the most recent P1 records", is used. Modes `0x03` and `0x04` were
both answered with **response code 7**. Loggers with a reply limit are read with mode `0x06`
instead; see [CR300 replies cut at 64 bytes](#cr300-replies-cut-at-64-bytes).

Response framing, confirmed against records whose values were already known from the serial trace:

```
response code (byte)
one or more blocks, each:
    table number (uint2), first record number (uint4), record count (uint2)
    interval tables: one timestamp (uint4 seconds, uint4 nanoseconds) for the block,
                     records then follow at the table interval
    event tables:    a timestamp before every record
    records, packed with no padding, in table field order
more-records flag (byte) -- the last byte of the body, after the final block
```

**A reply can carry several blocks.** Confirmed 2026-09-14: the logger starts a new block, with its
own first record number and timestamp, where an interval table's record times are not contiguous,
and only the body's final byte is the more-records flag. Reading just the first block and taking
the next byte as that flag stopped a ten-day `Shunt_Data` collection after 255 records.

Behaviours `collect_recent` depends on:

* **Asking for more records than are stored returns a bare response code**, not a short answer.
  The largest answerable window is found by binary search; halving alone undershoots and silently
  returns fewer records than exist.
* **A reply holds about 1 KB of records** (245 `StationStatus` records, 61 `Shunt_Data` records),
  with the more-records flag set; the rest is walked forward page by page.
* **A "most recent N" window is measured from the logger's newest record at each request**, and the
  table keeps growing while a long collection runs. The window anchor must be re-derived from every
  page. Holding the first request's anchor fixed skipped one record at a page boundary each time
  the table grew: nine records from a ten-day `StationStatus` collection, all of which were then
  found present on the logger. Each follow-up request overlaps two records and raises if a page
  still starts past the next expected record.
* **A record larger than one packet cannot be collected.** `Status` (1582 B), `Public` (1980 B) and
  `DataTableInfo` (1320 B) all exceed it. That needs the partial-record mode, which is not
  implemented; use Get Values for those.

### Stored-record field types

Sizes and decoders are in `RECORD_TYPES` in `src/openloggernet/pakbus.py`.

| Code | Type | How it was established |
|---|---|---|
| 2 | UInt2 | `TString_Data.TString_ErrorCount`, decoded and matched to the trace |
| 6 | Int4 | `DailyPower.Profile`, read back as 2 = Dire while the trace said Dire |
| 7 | FP2 | `DailyPower.BattMinQuiet` decoded to 12.57 against a trace of `prevDayV=12.570` |
| 9 | IEEE4 | `Shunt_Data.CurrentMean` decoded to 0.196 A against heartbeats of 0.195 A |
| 11 | ASCII | `Status.OSVersion`, length from the field's piece count |
| 14 | timestamp | **Inferred.** Only on `StartTime`, `LastSystemScan`, `LastSlowScan`, `LastPublish` |
| 28 | Boolean, 4 bytes | `Freezer_tester.CR6` `FinApp_Starts` and `Battery_Data`, 2026-09-22: widths 1 and 2 broke the record framing, 4 decoded `True` beside the matching `Battery()` and `PanelTemp` values |

**A field stores `piece` values, not the product of its `dimensions`.** The dimensions describe the
source array; a field that samples one element of it keeps the array's dimensions with `piece = 1`.
`SbdFinAppDaily.FinApp_Neutrons_Avg` and `FinApp_Muons_Avg` are `dimensions = [2]`, `piece = 1`, and
the table's records are 32 bytes, confirmed against raw replies on 2026-09-17.

Type 14 appears only in tables whose records exceed one packet, so neither its width nor its
decoding has been confirmed by reading a known value back.

Campbell FP2 is a sign bit, a 2-bit negative decimal exponent and a 13-bit mantissa, with `0x9FFE`
for NaN and `0x1FFF`/`0x9FFF` for the infinities.
