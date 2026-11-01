# openloggernet

An open replacement for the parts of the Campbell Scientific LoggerNet toolchain that the
[FirnMelt stations](https://github.com/UtrechtUniversity/firnmelt-station) use: a PakBus/BMP5
client, a runner for Campbell's CRBasic compilers, and command-line tools that flash programs to a
logger and read, set and collect its values. It is not affiliated with Campbell Scientific.

```bash
pip install openloggernet        # or: uv tool install openloggernet
```

The commands are installed as console scripts (`flash`, `read_values`, ...) and as modules
(`python -m openloggernet.flash`). Python 3.15 or newer is required.

## Development

```bash
uv sync                          # creates .venv with the dev tools
uv run pre-commit install        # ruff, pyrefly and file hygiene on every commit
uv run pre-commit run --all-files
uv run pytest
```

CI (`.github/workflows/ci.yml`) runs the same checks on every push and pull request. Agent and
contributor conventions are in [AGENTS.md](AGENTS.md).

## Releasing

Every push to `dev` that passes CI is published to [TestPyPI](https://test.pypi.org/p/openloggernet)
as `<version>.dev<run number>`. A push to `main` publishes to [PyPI](https://pypi.org/p/openloggernet)
when the `version` in `pyproject.toml` has not been released yet, so bump it (`uv version --bump
patch`) in the change that should be released. Publishing uses PyPI trusted publishing from the
`pypi` and `testpypi` GitHub environments; no API token is stored in the repository.

| Module | What it does |
|---|---|
| `flash` | Compile a CRBasic program and send it to a connected CR6 over USB. |
| `read_values` | Read `Public` variables, data table fields and `Status` fields off a running CR6. |
| `set_value` | Set `Public` variables on a running CR6; how testers are driven. |
| `collect_data` | Read stored data table records off a running CR6. |
| `logger_clock` | Read the CR6 clock, or set it to the host's UTC time. |
| `cr6comp` | Runs `compiler/CR6Comp.exe` (`.CR6`) or `compiler/cr1000xcomp.exe` (`.CR1X`) through `powershell.exe` (WSL) or Wine (macOS, Linux); used by `flash`. Not a script. |
| `pakbus` | PakBus/BMP5 client library used by `flash`, `read_values` and `logger_clock`. Not a script. |
| `comusb_capture` | Captures timestamped tester output and sends keys through a FIFO. |

For a long interactive tester session:

```bash
comusb_capture --log run.log --fifo /tmp/cr6.key
printf '1' > /tmp/cr6.key
```

---

## Compiling without Windows

`cr6comp` runs `CR6Comp.exe` through `powershell.exe` on WSL and through Wine on macOS and
Linux (on Ubuntu: `dpkg --add-architecture i386 && apt-get install wine32:i386 wine64 wine`).
The compilers are Campbell's and are not part of this package: `compiler/` is looked up in the
program's directory and then each parent, so a program under `crbasic/` finds `../compiler/`. Copy
`CR6Comp.exe` (and `cr1000xcomp.exe` for `.CR1X`) there from a CRBasic Editor or Device
Configuration Utility install.
`CR6COMP=opencrbasic` uses the native re-implementation from the
[opencrbasic](https://github.com/UtrechtUniversity/open-crbasic) package instead (`pip install
opencrbasic`); it reproduces CR6Comp's output for the FirnMelt programs but is not the reference
compiler, so keep the exe as the gate before flashing hardware.

## Compiling on macOS

`CR6Comp.exe` is a 32-bit Windows console program that imports only `KERNEL32.dll`, so it runs
under Wine; on Apple Silicon, Wine's x86-64 code runs through Rosetta 2. The Homebrew wine casks
were disabled on 2026-09-01 for failing Gatekeeper, so install the same Gcenx build they used,
without admin rights:

```bash
softwareupdate --install-rosetta --agree-to-license
mkdir -p ~/.local/wine
curl -L https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.17/wine-devel-11.17-osx64.tar.xz | tar -xJ -C ~/.local/wine
```

`cr6comp` looks for Wine in `$CR6_WINE`, then at
`~/.local/wine/Wine Devel.app/Contents/Resources/wine/bin/wine`, then as `wine` on `PATH`. It
uses the prefix `~/.wine-cr6` unless `WINEPREFIX` is set; Wine creates it on first use, which takes
about 15 s. `flash` then compiles exactly as on WSL.

Checked on 2026-09-23 with Wine 11.17 on an arm64 Mac: every entry program compiled; a syntax error
in a `.CR6` or in an included `.crb` gave `Compile Failed!` with its line and exit status 1; and the
FirnMelt tree at commit `da10004` reproduced the PipelineMode results its bench captures record. The
compiler writes `CR6cmple.xxx` into `crbasic/`, which git ignores.

Checked on 2026-10-20 with Ubuntu's Wine 10.0 package (`10.0~repack-12ubuntu1`) on x86-64 Ubuntu
26.04, installed with the command under [Compiling without Windows](#compiling-without-windows):
the FirnMelt `scripts/verify.py` compiled all 19 entry programs, `FinAppAcceptance.CR1X` included, with six warnings, all
for tables declared but never called. The compile-failure path was not exercised on this host.

---

## Reading stored records: `collect_data`

`read_values` sees only the newest value of a field. This reads the tables themselves, which is
what makes a run's full-resolution data survive: the serial trace carries only what the program
chose to print, and flashing deletes the tables.

```bash
collect_data --list                         # the logger's tables, sizes and intervals
collect_data DailyPower                     # the power-profile decision history
collect_data Shunt_Data --count 60 --csv shunt.csv
collect_data TString_Data --count 5         # all 48 sensors per record
```

Table definitions come from the logger's own `.TDF`, so field names, types and array shapes are
whatever the running program actually declared. A reply holds about 1 KB of records, so a long table is walked page
by page, each page re-anchored on the logger's newest record because the table keeps growing during
collection; a page that would leave a gap raises instead. Ten days of the 60 s `StationStatus` table
takes a few minutes. Records whose size exceeds one packet -- `Status`,
`Public`, `DataTableInfo` -- cannot be collected; read those with `read_values`. Details and the
behaviours this depends on are in [docs/pakbus.md](docs/pakbus.md).

---

## Setting the clock: `logger_clock`

The FirnMelt `Main.CR6` derives every schedule index from the logger's own clock and requires
that clock to be UTC. The logger stores no time zone, so a logger left on local time
silently shifts the whole grid, the daily aggregation boundary and the SBD send unit of day, with
no symptom other than things happening at the wrong time.

```bash
logger_clock                 # report logger time and its offset from host UTC
logger_clock --set-utc       # set it, then measure the result
```

The set is reported as an adjustment the logger applies to itself, so the transport delay is what
limits accuracy; the script measures the outcome afterwards rather than assuming it, and exits
non-zero if more than `--max-drift` seconds remain. The bench logger was found 7201 s fast on
2026-09-03, which is CEST, and was corrected to within 1 ms.

---

## Writing values to a running program: `set_value`

The counterpart to `read_values`, over `pakbus`'s Set Values transaction:

```bash
set_value Public.DoRead=true              # true/false are Booleans (-1/0)
set_value Public.ProfileOverride=0        # whole numbers go as Long
set_value Public.ForcedBattV=11.8         # anything else as IEEE4
```

This is how testers are driven: `TDR_tester.CR6` has no key menu and acts on its `Do*` flags, and
the others are following. It is also how a long run's power profile is changed without
re-flashing, which would delete the run's data tables, so check the port before writing to a
logger that is not on the bench.

---

## Flashing a program: `flash`

Replaces the manual "send the program with Device Configuration Utility" step. It compiles with
the validated `compiler/CR6Comp.exe`, writes the program **and the libraries it includes** to the
logger's `CPU:` drive, tells the logger to compile and run it, and reads the logger's own compile
results back out.

```bash
flash ../crbasic/PowerProfile_tester.CR6                     # compile, send, run
flash ../crbasic/PowerProfile_tester.CR6 --monitor 60 --send '\r'
flash ../crbasic/Main.CR6 --no-run                           # write the file only
```

The `--monitor` window prints whatever the running program writes to `ComUSB`, so a flash and the
start of a bench session are one command. The tester programs are menu driven and stay silent
until they get a keystroke, hence `--send '\r'`.

Exit status is 0 only when the logger reports a clean compile; a compile error on the logger is
printed and returns 1.

A `.CR1X` program is compiled with `cr1000xcomp.exe` and sent the same way to the bench CR1000X.
Files go in 256-byte packets; see [docs/lessons.md](docs/lessons.md#cr1000x-file-downloads-need-256-byte-packets).

### Requirements

* The CR6's USB port attached to WSL (`usbipd attach`), where it appears as
  `/dev/serial/by-id/usb-Campbell_Scientific_Inc._CR6_*`. `flash` finds it there by itself;
  `--port` overrides.
* Your user in the `dialout` group.
* `powershell.exe` (WSL) or Wine (macOS/Linux, see [Compiling on macOS](#compiling-on-macos)) for
  `CR6Comp.exe`. Use `--no-compile` to skip that step.

### Useful options

| Option | Purpose |
|---|---|
| `--monitor SECONDS`, `--send TEXT` | Stream `ComUSB` output after flashing; write `TEXT` first. |
| `--no-run` | Write the files but leave the running program alone. |
| `--no-compile` | Skip `CR6Comp.exe` and send the file as-is. |
| `--no-includes` | Do not send the `IncludeSection` libraries alongside the program. |
| `--address`, `--my-address` | PakBus addresses (logger default 1, host default 4094). |
| `--debug` | Dump every PakBus packet as hex. |

---

## Reading state off a running logger: `read_values`

The counterpart to `flash`. `flash` puts a program on the logger and shows what it writes to
`ComUSB`; `read_values` shows the state of a program that writes nothing. **Most libraries here
are exercised from a slow sequence, which produces no serial output at all**, so without this their
variables are unobservable while `Main.CR6` runs.

```bash
read_values Public.PT1000_Temp_C Public.AmbientForDecision Status.SkippedScan
read_values --type long --swath 9 Public.PowerRTime     # an array
read_values --watch 30 Public.PT1000Ok                  # poll, print on change
```

Each argument is `Table.Field`. The `Public` table, the `Status` table and the program's own data
tables are all readable, and an array element can be named directly (`Public.Acclima_VWC(2)`).

`--watch SECONDS` polls until interrupted and prints **only when a value changes**, which is how to
wait out a gated slow sequence without guessing when it fired. A bad field name inside a watch is
reported and the watch continues, so one typo does not end a long observation.

### Types

`--type` chooses the representation to ask the logger for. The default, `asciiz`, asks the logger
to format the value itself: it reads any type, renders a `Boolean` as `False` and a `NAN` as `NAN`,
and is the right choice unless you need exact binary or an array.

| `--type` | Use for | Note |
|---|---|---|
| `asciiz` | anything; the default | **ignores `--swath` and returns one value** |
| `ieee4` | `Float` / `IEEE4` | exact; honours `--swath` |
| `long` | `As Long` | exact; honours `--swath` |
| `ieee8` | extra precision | honours `--swath` |

A `Boolean` read as `long` comes back from the logger as *response code 0 with an empty body* rather
than as an error, so the library checks the returned byte count and raises instead of silently
returning nothing. Read Booleans as `asciiz`. The wire format, the type codes and how each was
established on the bench are in [docs/pakbus.md](docs/pakbus.md).

### Requirements

The same as `flash`: the CR6's USB port attached to WSL, and your user in `dialout`. It does not
need `powershell.exe`, since nothing is compiled.

---

There is deliberately **no** option for a PakBus security code. This is a bench tool for
validation work on an unsecured logger; it sends a security code of 0 and has no way to send
anything else, so it cannot be pointed at a secured station.

### What it does to the logger

Sending a program is the same operation Device Configuration Utility performs: the file is marked
**run on power up**, and the logger deletes the data tables of the program it replaces. The logger
disconnects and re-enumerates its USB device while it compiles — `flash` waits for it and
reopens the port, which may be a different `/dev/ttyACM*` node than before.

Protocol details and what has been verified on hardware: [docs/pakbus.md](docs/pakbus.md).
