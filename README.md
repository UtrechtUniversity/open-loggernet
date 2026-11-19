# openloggernet

An open replacement for parts of the Campbell Scientific LoggerNet toolchain: a PakBus/BMP5
client, a runner for Campbell's CRBasic compilers, and the `oln` command line, which flashes
programs to a logger and reads, sets and collects its values. It is not affiliated with Campbell
Scientific.

```bash
uv tool install openloggernet        # or: pip install openloggernet
```

Everything is a subcommand of `oln`. Python 3.14 or newer is required. The optional GUI needs the
`gui` extra, see below.

## Supported loggers and limits

`oln` speaks PakBus/BMP5 over a **USB serial port** only. It was written against the CR6 and has
been used with the CR1000X and CR300; a CR350 is recognised (reply-size limit) but has not been
tried. Other Campbell loggers, and everything the wire format is not documented for, are untested.
What `oln` is not:

- It is not LoggerNet. There is no server, scheduled collection, TCP/IP, RF, modem or multi-hop
  PakBus routing, and no other protocols such as Modbus or DNP3.
- It works only with an **unsecured** logger: the PakBus security code is fixed at 0 on purpose.
- It does not include Campbell's compilers, operating systems or Device Configuration Utility
  descriptions; see [Compiling without Windows](#compiling-without-windows).
- Behaviour was confirmed on a few bench loggers and OS versions ([docs/pakbus.md](docs/pakbus.md)
  records what, and what is not confirmed). Other models and OS versions may differ.

**Be careful with a real logger.** `flash` and `program run` delete the data tables of the program
they replace, `program stop --delete-data` deletes data, `settings set` saves and resets the logger,
and `os` replaces its operating system. Collect what you need first and try changes on a bench
logger before a field one. The software is provided as is, without warranty (see [License](#license)).

## `oln`: the logger from the command line

`oln` covers what LoggerNet's Connect screen and Device Configuration Utility are used for on a
logger attached by USB. Choose the logger with `--model` or `--port` when more than one is
attached; `--json` gives machine-readable output (JSON Lines for `watch`), which is what the VS
Code extension uses. `oln --version` prints the installed version.

```bash
oln ports                                   # attached loggers and their models
oln --model CR300 info                      # OS, serial, program, compile result
oln --model CR300 status                    # the Status table, as Station Status shows it
oln --model CR6 clock --set-utc
oln --model CR300 tables
oln --model CR300 collect Table1 --all --format toa5 -o Table1.dat
oln --model CR6 watch Public PTemp          # live values, like the Table Monitor
oln --model CR6 get Public.PTemp            # read / write single values
oln --model CR6 set Public.DoRead=true
oln --model CR6 files ls                    # drives, files, run flags
oln --model CR6 files put Main.CR6          # files get / put / rm
oln --model CR6 program run Main.CR6        # program status / run / stop / resume / power-up
oln --model CR6 flash Main.CR6 --monitor 60 # compile locally, send with its libraries, run
oln --model CR300 settings "station name"   # DevConfig settings; settings set NAME=VALUE
oln --model CR6 terminal                    # the logger's terminal
oln --model CR300 os CR300.11.03.obj        # send a new operating system
oln capture --log run.log --fifo /tmp/cr6.key   # log ComUSB text, send keys (POSIX)
```

`oln gui` is the same in a window, in the manner of LoggerNet's Connect screen: station and
Status, clock, tables and collection, a live monitor with a plot, files and program control,
settings, the terminal and OS updates. It needs the `gui` extra (PySide6):

```bash
uv tool install "openloggernet[gui]"
oln gui
```

Only one program can hold a logger's port: disconnect LoggerNet (or close DevConfig) first.
Setting names come from Device Configuration Utility's device descriptions when it is installed
(`C:\Campbellsci\Lib\DevConfigLib`, or `$OLN_DEVCONFIG_LIB`); without them settings are shown by
number. How each command maps onto PakBus, and what LoggerNet does for the same action, is in
[docs/pakbus.md](docs/pakbus.md).

### Reading and writing values: `get`, `set`, `watch`

Each `get` argument is `Table.Field`. The `Public` table, the `Status` table and the program's own
data tables are all readable, and an array element can be named directly (`Public.VWC(2)`).
`watch` polls a table and prints only the values that change, which is how to observe a program
that writes nothing to `ComUSB`, such as work done in a slow sequence.

`--type` chooses the representation to ask the logger for. The default, `asciiz`, asks the logger
to format the value itself: it reads any type, renders a `Boolean` as `False` and a `NAN` as `NAN`,
and is the right choice unless you need exact binary or an array.

| `--type` | Use for | Note |
|---|---|---|
| `asciiz` | anything; the default | **ignores `--swath` and returns one value** |
| `ieee4` | `Float` / `IEEE4` | exact; honours `--swath` |
| `long` | `As Long` | exact; honours `--swath` |
| `ieee8` | extra precision | honours `--swath` |

A `Boolean` read as `long` comes back from the logger as *response code 0 with an empty body*
rather than as an error, so the library checks the returned byte count and raises instead of
silently returning nothing. Read Booleans as `asciiz`.

`set` writes `true`/`false` as CRBasic Booleans (-1/0), whole numbers as Long and anything else as
IEEE4. It changes a running program without resending it, which would delete its data tables.

### Collecting: `collect`

Table definitions come from the logger's own `.TDF`, so field names, types and array shapes are
whatever the running program actually declared. A table is collected the way LoggerNet does it,
record ranges up to a fixed end so the table can keep growing without opening a gap, with two
requests in flight: on Windows the CR300 gives about 1700 records/s and the CR1000X about
1600 records/s of 240-byte records. Records larger than one packet, such as `Status` and `Public`,
are read in fragments.

### The clock: `clock`

The logger stores no time zone. `clock --set-utc` sets it to the host's UTC time as an adjustment
the logger applies to itself, so the transport delay is what limits accuracy; the result is
measured afterwards and the command exits non-zero if more than `--max-drift` seconds remain.

### Flashing a program: `flash`

`flash` compiles a program with Campbell's compiler (see [Compiling without
Windows](#compiling-without-windows)), writes it **and the libraries it includes** to the logger's
`CPU:` drive, tells the logger to compile and run it, and reads the logger's own compile results
back. Exit status is 0 only when the logger reports a clean compile.

| Option | Purpose |
|---|---|
| `--monitor SECONDS`, `--send TEXT` | Stream `ComUSB` output afterwards; write `TEXT` first, e.g. `'\r'` for a menu-driven program. |
| `--no-run` | Write the files but leave the running program alone. |
| `--no-compile` | Send the file without compiling it locally. |
| `--no-includes` | Do not send the `IncludeSection` libraries alongside the program. |

`program run` instead compiles and runs a program that is already on the logger, without a local
compiler. Either way the file is marked **run on power up**, and the logger deletes the data tables
of the program it replaces. The logger disconnects and re-enumerates its USB device while it
compiles; `oln` waits for it and reopens the port, which may come back under a different name.
Files go in 976-byte packets on Windows and 256-byte packets elsewhere; see
[docs/lessons.md](docs/lessons.md#file-download-packet-size-depends-on-the-host). On Linux, add your
user to the `dialout` group.

`oln` sends a PakBus security code of 0 and has no option for another, so it works only with an
unsecured logger.

## Compiling without Windows

`cr6comp` runs `CR6Comp.exe` directly on Windows, through `powershell.exe` on WSL and through Wine
on macOS and Linux (on Ubuntu: `dpkg --add-architecture i386 && apt-get install wine32:i386 wine64 wine`).
The compilers are Campbell's and are not part of this package: `compiler/` is looked up in the
program's directory and then each parent, so a program in `src/` finds `../compiler/`. Copy
`CR6Comp.exe` (and `cr1000xcomp.exe` for `.CR1X`) there from a CRBasic Editor or Device
Configuration Utility install.
Only `.CR6` and `.CR1X` programs have a local compiler here; for other extensions (e.g. `.CR300`) use
`oln flash --no-compile`, or `CR6COMP=opencrbasic`.

`CR6COMP=opencrbasic` uses the native re-implementation from the
[open-crbasic](https://github.com/UtrechtUniversity/open-crbasic) project instead (`pip install
opencrbasic`); it is not the reference compiler, so keep the exe as the gate before flashing hardware.

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
about 15 s. `oln flash` then compiles exactly as on WSL.

Checked on 2026-09-23 with Wine 11.17 on an arm64 Mac: a set of CR6 entry programs compiled, and a
syntax error in a `.CR6` or in an included `.crb` gave `Compile Failed!` with its line and exit
status 1. The compiler writes `CR6cmple.xxx` next to the program.

Checked on 2026-10-20 with Ubuntu's Wine 10.0 package (`10.0~repack-12ubuntu1`) on x86-64 Ubuntu
26.04, installed with the command above: 19 entry programs compiled, a `.CR1X` included. The
compile-failure path was not exercised on this host.

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

## Related projects

- [open-crbasic](https://github.com/UtrechtUniversity/open-crbasic): open-source CRBasic
  pre-compilers and language server (`opencrbasic` on PyPI).
- [open-crbasic-vsix](https://github.com/UtrechtUniversity/open-crbasic-vsix): VS Code extension
  for CRBasic that drives loggers through `oln --json`.

## License

[MIT](LICENSE), (c) 2026 Utrecht University. Campbell Scientific, LoggerNet, CRBasic and the logger
model names are trademarks of their owners; this project is independent of them.
