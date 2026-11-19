# Logger communication lessons

Behaviour of Campbell loggers observed while building this package. Each entry states the rule,
its evidence, and what the code does about it. Evidence is a bench observation on the date given.

## PakBus interleaves with `ComUSB`

The CR6 continues sending `0xBD`-framed PakBus traffic while a program writes text to `ComUSB`.
Observed in a bench capture on 2026-08-25. Capture tools must validate and remove
frames with a bounded filter; use `src/openloggernet/pakbus.py:TextFilter`.

## USB re-enumeration during compile

The CR6 drops and recreates its USB device while compiling a downloaded program, possibly under a
different `/dev/ttyACM*` name. Observed on CR6 OS 6.14.5.1 on 2026-08-26. Resolve through
`/dev/serial/by-id/`, wait, and reopen as `src/openloggernet/flash.py:reconnect` does.

## First PakBus transaction

The first packet after opening USB was lost once while the next packet succeeded. Observed once on
2026-08-26 and not reproduced. Retry the opening hello; do not treat one miss as proof of link
failure.

## File download packet size depends on the host

The bench CR1000X (serial 59624, OS `CR1000X.8.5.0`) answered PakBus hello and programming
statistics from a Mac but never answered a 512-byte file-download packet, twice; 256-byte packets
transferred every file. Observed on 2026-09-24.

On Windows 11 the same CR1000X, the CR6 (serial 27946) and the CR300 (serial 33590) took 100 KB of
random bytes in 256-, 512- and 976-byte packets and read it back identical (2026-11-02), so the
Mac failure belongs to that host, not the CR1000X. 976 is what Device Configuration Utility uses;
it was 2 % (CR6), 9 % (CR1000X) and 29 % (CR300) faster than 256, the logger's writing being the
limit. 1500-byte packets timed out on the CR300, whose `MaxPktSize` setting is 1000.
`src/openloggernet/pakbus.py` sends 976 bytes per packet on Windows and 256 elsewhere.

## A resent identical program keeps its tables

File control code 1, which `oln flash` sends, erased `FinApp_Poll` on the bench CR1000X when the
program had changed, although only a `Public` variable was added and the table was untouched
(2026-09-24), but kept all 3212 records when the byte-identical program was sent again
(2026-09-28). The same on the CR6 and
CR300 on 2026-11-02: sending the running `oln_test` program again under another name kept the
`Ring` table's records and numbering while the program's own counter started again from 1. A flash
cannot be relied on to clear a table. Code 13 with an empty file name, which LoggerNet sends for
"Stop Program, Delete Data", stops the program and deletes its data (CR6, 2026-11-02; see
docs/pakbus.md, File control command codes); `oln program stop --delete-data` sends it. Otherwise
tell the runs apart by time.

## A clock adjustment lands after the reply

The CR6 (serial 27946, OS 14.4.0) answers a Clock command that carries an adjustment with the time
it had before, and a Clock read sent straight after still returned that old time; one sent 0.5 s
later returned the adjusted time, for adjustments of 10 s, 1 h, 30 days and 248 days
(2026-11-19). `oln clock --set-utc` therefore failed every time with "offset still above 0.5 s"
although the logger had been set. `src/openloggernet/cli.py:cmd_clock` now reads the clock again
every 0.3 s, for up to 3 s, until the offset it asked for is reached.

## Settings written by name are kept without a reset

On the same CR6, BMP5 Set Values on the `Settings` table changed Station Name, UTCOffset and
IsRouter at once: Get Values read the new values back, Station Name also in the DevConfig Get
Settings reply (setting 2) and in `Status.StationName`, and the next calls were answered at once
on the same port, with no sign of a reset; all three were set back (2026-11-19). DevConfig's own Set Settings saves with a commit that resets the
logger; this path does not. Whether a setting that needs a restart to act (a port or network
setting) takes effect before one was not tested. `oln settings set` uses this path when no DevConfig
library is installed and the logger lists its settings on its terminal (`docs/pakbus.md`,
"Settings by name").

## Not every setting the terminal lists can be read by name

Get Values on `Settings.UID`, `Settings.CampbellCloudEnable` and `Settings.MQTTState` answered
"permission denied" on the CR6 above, while the other 333 listed settings read (2026-11-19).
`src/openloggernet/settings.py:read_named` keeps such a setting with its error instead of failing,
and the VS Code extension shows it read-only.
