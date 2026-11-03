"""Run the Campbell compiler from WSL (through powershell.exe) or macOS/Linux (through Wine).

Set CR6COMP=opencrbasic to use the native re-implementation (the `opencrbasic` package, installed
separately) instead of the exe. It is not the reference compiler; see the opencrbasic README for its
verified scope.

The program's extension picks the compiler: `.CR6` uses compiler/CR6Comp.exe and `.CR1X` uses
compiler/cr1000xcomp.exe. `compiler/` is found in the program's directory or the nearest parent
that has it. Used by `oln flash`.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

COMPILER_BY_SUFFIX = {".cr6": "CR6Comp.exe", ".cr1x": "cr1000xcomp.exe"}

DEFAULT_WINE = Path.home() / ".local" / "wine" / "Wine Devel.app" / "Contents" / "Resources" / "wine" / "bin" / "wine"
DEFAULT_WINEPREFIX = Path.home() / ".wine-cr6"


def find_up(start: Path, relative: str) -> Path | None:
    """`relative` under `start` or the nearest parent that has it."""
    for directory in (start.resolve(), *start.resolve().parents):
        if (directory / relative).exists():
            return directory / relative
    return None


def find_wine() -> str | None:
    """Wine binary: $CR6_WINE, then the ~/.local/wine install, then `wine` on PATH."""
    override = os.environ.get("CR6_WINE")
    if override:
        return override if Path(override).is_file() else None
    if DEFAULT_WINE.is_file():
        return str(DEFAULT_WINE)
    return shutil.which("wine")


def native() -> bool:
    """True when CR6COMP=opencrbasic selects the native re-implementation."""
    return os.environ.get("CR6COMP", "").lower() == "opencrbasic"


def available(platform: str = sys.platform) -> bool:
    if native():
        return importlib.util.find_spec("opencrbasic") is not None
    if platform == "darwin":
        return find_wine() is not None
    return shutil.which("powershell.exe") is not None or find_wine() is not None


def compiler_for(program: Path) -> Path:
    """The compiler for `program`'s logger, chosen by its extension."""
    name = COMPILER_BY_SUFFIX.get(program.suffix.lower())
    if name is None:
        raise ValueError(f"{program.name}: no compiler for extension {program.suffix!r}")
    compiler = find_up(program.parent, f"compiler/{name}")
    if compiler is None:
        raise FileNotFoundError(f"{program.name}: no compiler/{name} in its directory or a parent")
    return compiler


def command(program: Path, platform: str = sys.platform) -> tuple[list[str], dict[str, str]]:
    """Command line and environment that compile `program` from its own directory."""
    env = dict(os.environ)
    if native():
        return [sys.executable, "-m", "opencrbasic", "--no-message-file", program.name], env
    compiler = compiler_for(program)
    if platform == "darwin" or shutil.which("powershell.exe") is None:
        wine = find_wine()
        if wine is None:
            raise FileNotFoundError("Wine not found; see the openloggernet README, 'Compiling on macOS'")
        env.setdefault("WINEPREFIX", str(DEFAULT_WINEPREFIX))
        env.setdefault("WINEDEBUG", "-all")
        return [wine, str(compiler), program.name], env
    windows_path = subprocess.run(
        ["wslpath", "-w", str(compiler)], capture_output=True, text=True, check=True
    ).stdout.strip()
    return ["powershell.exe", "-Command", f"& '{windows_path}' {program.name}"], env


def compiled(output: str) -> bool:
    return "Compiled in" in output and "Compile Failed" not in output


def compile_program(program: Path) -> tuple[bool, str]:
    """Compile `program`; returns (success, compiler output)."""
    if not available():
        if native():
            return False, "CR6COMP=opencrbasic needs the opencrbasic package: pip install opencrbasic"
        return False, "no CR6Comp.exe runner: needs powershell.exe (WSL) or Wine (macOS/Linux), or CR6COMP=opencrbasic"
    try:
        args, env = command(program)
    except FileNotFoundError as exc:
        return False, str(exc)
    result = subprocess.run(args, cwd=program.parent, env=env, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return compiled(output), output
