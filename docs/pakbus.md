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

Codes 1, 4, 11, 12 and 13 are what LoggerNet 4.10's File Control sent for the actions named,
captured against the CR6 (serial 27946, OS 6.14.4.0) on 2026-11-02. Codes 2, 6, 7 and 10 were then
sent to the same CR6 by `oln`, and their effect read back from Get Prog Stats and `.DIR2`. The
CRBasic help's descriptions are wrong for 7 and 10.

| Code | File name | LoggerNet action | Observed effect |
|---|---|---|---|
| 1 | program | Run Options: Run Now + Run On Power-up | compiles and runs it, marks it run-on-power-up; 6 s hold-off, USB re-enumerates |
| 2 | program | -- | marks it run-on-power-up, nothing else; no hold-off |
| 4 | file | Delete | deletes the file |
| 6 | program | -- | compiles and runs it; the power-up program is left as it was |
| 7 | program | -- | stops and **unloads** the program: no program, no power-up program |
| 10 | program | -- | compiles and runs it, and **clears** the power-up program |
| 11 | *empty* | Stop Program, Retain Data | pauses: compile state 3, attribute 5; no hold-off, no re-enumeration |
| 12 | program | Run Options: Restart Program, Do not erase data files | resumes a paused program; 6 s hold-off |
| 13 | *empty* | Stop Program, Delete Data, Clear Run On Power-up | stops it, deletes its data, clears the power-up program |

Every code with a hold-off made the CR6 re-enumerate its USB device, which on Windows kept its COM
port name but took a new USB address.

### Get Prog Stats compile state

1 while running, 3 after code 11 paused the program, 0 with no program (CR6, 2026-11-02). The CR6,
CR1000X and CR300 also append the **model** and the **station name** (both asciiz) after the
compile result: `CR6`/`27946`, `CR1000X`/`59624`, `CR300`/`1`.

### Directory: `.DIR` and `.DIR2`

Pseudo-files read with File Upload; LoggerNet's File Control reads `.DIR2`. Worked out on all three
bench loggers on 2026-11-02 against the File Control window:

```
version (byte: 1 for .DIR, 2 for .DIR2), then per entry:
    name (asciiz), size (uint4), modified (asciiz, "YYYY-MM-DD hh:mm:ss"),
    attributes (bytes, terminated by 0)
```

A drive entry has a name ending in `:` and an empty date, and its size field is the drive's free
space: a uint4 in `.DIR`, an **IEEE4 float** in `.DIR2`. CR6: `.DIR2` `4b ef a0 00` = 31 408 128,
which `.DIR` gave as an integer and LoggerNet showed as 31.41 MB; CR300: 83 791 872, equal to
`Status.CPUDriveFree`. Attributes seen: 1 = the loaded program, 2 = run on power-up, 5 = paused.
LoggerNet showed `[1, 2]` as "running, power up" and `[1, 5, 2]` as "stopped, power up".

### Summary: `.TDFS`

The first thing LoggerNet reads on connecting. It is the Get Prog Stats fields (from OS version to
compile result) after a version byte (0x11), then the model (asciiz), then for each table its name
(asciiz), size in records (uint4) and table signature (uint2), the same signature Collect Data
uses. LoggerNet then reads one table's definition with File Upload of `<table>.TDF`, e.g.
`Public.TDF`, rather than the whole `.TDF`.

## Settings: the DevConfig protocol

Device Configuration Utility 2.34 reads and writes settings with PakCtrl messages, not BMP5.
Captured against the CR300 (serial 33590, OS 11.02) on 2026-11-02 and then used from `oln`
against all three bench loggers. DevConfig used PakBus address 4089 for itself.

| Message | Command | Response | Command body after message type and transaction |
|---|---|---|---|
| Get String Settings | `0x07` | `0x87` | names wanted (asciiz); empty = all |
| Get Settings | `0x0F` | `0x8F` | security (uint2), then optionally `0x8000 \| last id` to continue |
| Set Settings | `0x10` | `0x90` | security (uint2), per setting: id (uint2), length (uint2), value |
| Control | `0x13` | `0x93` | security (uint2), action (byte); 1 = commit |

* **Get String Settings** answers `Name=value;` text: `Company`, `Model`, `Password`, and the
  `PakCtrlCodes` and `BMP5Codes` the logger supports. The CR300 leaves out the `;` after an empty
  `Password=`.
* **Get Settings** answers outcome (byte, 1 = ok), device type (uint2: CR300 69, CR6 55,
  CR1000X 78), catalogue major and minor version (bytes), a more-settings flag (byte), then per
  setting: id (uint2), a word whose bit 14 is read-only and whose low 14 bits are the value length,
  and the value. With the flag set, the next request carries `0x8000 | the last id received`; the
  CR6 gave 115 settings in the first reply and 145 in all.
* **Set Settings** answers outcome (byte), then id (uint2) and outcome (byte) per setting; 1 is
  accepted. **Control action 1** saves the settings and resets the logger, which DevConfig's
  Apply does after a set. Station Name on the CR300 was changed this way by DevConfig, read back by
  `oln`, and set back by `oln`.

What a setting number means and how its value is laid out is not on the wire. DevConfig takes it
from Campbell's device descriptions (`.dd`, `.ddi`) under `C:\Campbellsci\Lib\DevConfigLib\en2`,
chosen by device type and catalogue major version. `openloggernet.settings` reads them from there
when installed; they are not part of this repository. Strings are NUL-terminated; enums are one
byte; `serial-no` is a uint4. The CR300's GOES settings use per-component offsets and are left as
hex.

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

Modes in use, as LoggerNet 4.10 uses them (USB captures of LoggerNet talking to the bench CR6,
CR1000X and CR300, 2026-11-02):

| Mode | Parameters | Use |
|---|---|---|
| `0x05` | P1 = N (uint4) | the most recent N records; LoggerNet asks for N = 1 to find the newest record, and for the Table Monitor |
| `0x06` | P1 = first record, P2 = end record (uint4 each) | records P1 up to but not including P2 |
| `0x08` | P1 = record (uint4), P2 = byte offset (uint4) | part of one record, from the byte offset |

Modes `0x03` and `0x04` were both answered with **response code 7** by the CR6 on 2026-09-05.

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

### Collecting a table: what LoggerNet does

An "All the Data" Custom Collection of the CR300's `Table1` (128 958 records of 6 bytes) was:

1. mode `0x05`, N = 1, for the newest record number;
2. mode `0x06` over `[newest + 1 - table size, newest + 1)`, the start wrapped below zero as a
   uint4; the logger answered from its oldest record, record 0;
3. mode `0x06` again from the record after the last one received, **with the same end**, until the
   end is reached. Each reply held as many records as fit, 816 bytes here, about 70 ms apart.

That took 76 s, 1697 records/s. `PakBusLink.collect_range` does the same, so a long collection
cannot open a gap as the table grows: the end is fixed, and every request starts after the last
record received. It got 1722 records/s from the same table. It then asks for the rest in windows
of one reply's worth, with two requests in flight. The CR1000X answers them back to back:
`FinApp_Poll` (240-byte records, three per reply) went from 1026 to 1600 records/s. The CR300
spends its time per request inside the logger, and gains nothing.

The previous method, paging "most recent N" windows and finding the largest answerable N by
binary search, managed 356 records/s on `FinApp_Poll` and stopped with "no progress" after
record 104 of the CR300's `Table1`. It is gone.

A range that starts below the oldest record held has only been tried while record 0 was still
held; on a table whose ring has wrapped, that behaviour is untested.

### Partial records

A record too large for one reply, such as `Status` or `Public`, comes back in fragments. LoggerNet
reads them when it connects and for Station Status, so the layout below is what it received from
the CR6's 1520-byte `Status`:

```
response code (byte), table number (uint2), record number (uint4),
0x80000000 | byte offset (uint4)     -- where a whole-record block has its record count
fragment
more-records flag (byte)
```

The offset counts into the record *image*: the 8-byte timestamp, then the fields. The first
fragment came from mode `0x05`, N = 1, at offset 0 with 958 field bytes after the timestamp.
LoggerNet then asked mode `0x08` for offset 966, and got the last 562 bytes: 966 + 562 = 8 + 1520.
`PakBusLink.read_record` does this. Read back on 2026-11-02, `Status` from the CR6 (1520 B),
CR1000X (1572 B) and CR300 (956 B) decoded field for field. The CR6's `WatchdogErrors` = 2 and
`LithiumBattery` = 3.67 matched LoggerNet's Station Status, and so did `StartTime`.

### Stored-record field types

Sizes and decoders are in `RECORD_TYPES` in `src/openloggernet/pakbus.py`.

| Code | Type | How it was established |
|---|---|---|
| 2 | UInt2 | `TString_Data.TString_ErrorCount`, decoded and matched to the trace |
| 6 | Int4 | `DailyPower.Profile`, read back as 2 = Dire while the trace said Dire |
| 7 | FP2 | `DailyPower.BattMinQuiet` decoded to 12.57 against a trace of `prevDayV=12.570` |
| 9 | IEEE4 | `Shunt_Data.CurrentMean` decoded to 0.196 A against heartbeats of 0.195 A |
| 11 | ASCII | `Status.OSVersion`, length from the field's piece count |
| 14 | timestamp, 8 bytes: seconds and nanoseconds since 1990 | CR6 `Status.StartTime` read as 2026-02-27 13:08:28.516, which LoggerNet's Station Status showed as 13:08:28, 2026-11-02 |
| 28 | Boolean, 4 bytes | `Freezer_tester.CR6` `FinApp_Starts` and `Battery_Data`, 2026-09-22: widths 1 and 2 broke the record framing, 4 decoded `True` beside the matching `Battery()` and `PanelTemp` values |

**A field stores `piece` values, not the product of its `dimensions`.** The dimensions describe the
source array; a field that samples one element of it keeps the array's dimensions with `piece = 1`.
`SbdFinAppDaily.FinApp_Neutrons_Avg` and `FinApp_Muons_Avg` are `dimensions = [2]`, `piece = 1`, and
the table's records are 32 bytes, confirmed against raw replies on 2026-09-17.

Campbell FP2 is a sign bit, a 2-bit negative decimal exponent and a 13-bit mantissa, with `0x9FFE`
for NaN and `0x1FFF`/`0x9FFF` for the infinities.
