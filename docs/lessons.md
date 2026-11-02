# Logger communication lessons

Behaviour of Campbell loggers observed while building this package. Each entry states the rule,
its evidence, and what the code does about it. Evidence paths under `knowledge/test_results/` are raw
bench captures in the [firnmelt-station](https://github.com/UtrechtUniversity/firnmelt-station)
repository, where these lessons were first recorded.

## PakBus interleaves with `ComUSB`

The CR6 continues sending `0xBD`-framed PakBus traffic while a program writes text to `ComUSB`.
Verified in `test_result_FinAppLib_25_08_2026_15_09.md`. Capture tools must validate and remove
frames with a bounded filter; use `src/openloggernet/pakbus.py:TextFilter`.

## USB re-enumeration during compile

The CR6 drops and recreates its USB device while compiling a downloaded program, possibly under a
different `/dev/ttyACM*` name. Observed on CR6 OS 6.14.5.1 on 2026-08-26. Resolve through
`/dev/serial/by-id/`, wait, and reopen as `src/openloggernet/flash.py` does.

## First PakBus transaction

The first packet after opening USB was lost once while the next packet succeeded. Observed once on
2026-08-26 and not reproduced. Retry the opening hello; do not treat one miss as proof of link
failure.

## File download packet size depends on the host

The bench CR1000X (serial 59624, OS `CR1000X.8.5.0`) answered PakBus hello and programming
statistics from a Mac but never answered a 512-byte file-download packet, twice; 256-byte packets
transferred every file. Observed on 2026-09-24 in
`knowledge/test_results/test_result_FinAppLib_24_09_2026_13_34.md`.

On Windows 11 the same CR1000X, the CR6 (serial 27946) and the CR300 (serial 33590) took 100 KB of
random bytes in 256-, 512- and 976-byte packets and read it back identical (2026-11-02), so the
Mac failure belongs to that host, not the CR1000X. 976 is what Device Configuration Utility uses;
it was 2 % (CR6), 9 % (CR1000X) and 29 % (CR300) faster than 256, the logger's writing being the
limit. 1500-byte packets timed out on the CR300, whose `MaxPktSize` setting is 1000.
`src/openloggernet/pakbus.py` sends 976 bytes per packet on Windows and 256 elsewhere.

## A resent identical program keeps its tables

File control code 1, which `flash` sends, erased `FinApp_Poll` on the bench CR1000X when the
program had changed, although only a `Public` variable was added and the table was untouched
(2026-09-24), but kept all 3212 records when the byte-identical program was sent again
(2026-09-28). Observed in `knowledge/test_results/test_result_FinAppLib_24_09_2026_13_34.md` and
`knowledge/test_results/test_result_FinAppLib_28_09_2026_08_34.md`. The same on the CR6 and
CR300 on 2026-11-02: sending the running `oln_test` program again under another name kept the
`Ring` table's records and numbering while the program's own counter started again from 1. A flash
cannot be relied on to clear a table. Code 13 with an empty file name, which LoggerNet sends for
"Stop Program, Delete Data", stops the program and deletes its data (CR6, 2026-11-02; see
docs/pakbus.md, File control command codes); `oln program stop --delete-data` sends it. Otherwise
tell the runs apart by time.
