import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openloggernet import cr6comp


class Cr6CompTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("compiler/CR6Comp.exe", "compiler/cr1000xcomp.exe", "opencrbasic/src/opencrbasic/__init__.py"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).touch()
        (self.root / "crbasic").mkdir()
        self.main = self.root / "crbasic" / "Main.CR6"

    def test_compiled_requires_success_line_and_no_failure(self) -> None:
        self.assertTrue(cr6comp.compiled("Main.CR6 -- Compiled in SequentialMode."))
        self.assertTrue(cr6comp.compiled(
            "Main.CR6 -- Compiled in SequentialMode.\nWarning: Table T, is declared but never called."
        ))
        self.assertFalse(cr6comp.compiled(
            "Broken.CR6 -- Compile Failed!\n\nline 222: Invalid, or out of place expression"
        ))
        self.assertFalse(cr6comp.compiled(""))

    def test_darwin_command_uses_wine_override_and_default_prefix(self) -> None:
        with tempfile.NamedTemporaryFile() as wine:
            environment = {key: value for key, value in os.environ.items()
                           if key not in ("WINEPREFIX", "WINEDEBUG", "CR6COMP")}
            environment["CR6_WINE"] = wine.name
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertTrue(cr6comp.available("darwin"))
                args, env = cr6comp.command(self.main, "darwin")
        self.assertEqual(args, [wine.name, str(self.root.resolve() / "compiler" / "CR6Comp.exe"), "Main.CR6"])
        self.assertEqual(env["WINEPREFIX"], str(cr6comp.DEFAULT_WINEPREFIX))
        self.assertEqual(env["WINEDEBUG"], "-all")

    def test_extension_selects_logger_compiler(self) -> None:
        self.assertEqual(cr6comp.compiler_for(self.main).name, "CR6Comp.exe")
        self.assertEqual(cr6comp.compiler_for(self.main.with_name("FinAppAcceptance.CR1X")).name,
                         "cr1000xcomp.exe")
        with self.assertRaises(ValueError):
            cr6comp.compiler_for(self.main.with_name("Lib.crb"))

    def test_program_outside_a_compiler_tree_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            program = Path(elsewhere) / "Main.CR6"
            with self.assertRaises(FileNotFoundError):
                cr6comp.compiler_for(program)
            with mock.patch.dict(os.environ, {"CR6COMP": "opencrbasic"}):
                ok, output = cr6comp.compile_program(program)
        self.assertFalse(ok)
        self.assertIn("no opencrbasic/src", output)

    def test_missing_wine_override_is_unavailable(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "CR6COMP"}
        environment["CR6_WINE"] = "/nonexistent/wine"
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertFalse(cr6comp.available("darwin"))
            with self.assertRaises(FileNotFoundError):
                cr6comp.command(self.main, "darwin")

    def test_linux_without_powershell_uses_wine(self) -> None:
        with tempfile.NamedTemporaryFile() as wine:
            environment = {key: value for key, value in os.environ.items() if key != "CR6COMP"}
            environment["CR6_WINE"] = wine.name
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch("shutil.which", return_value=None):
                self.assertTrue(cr6comp.available("linux"))
                args, _ = cr6comp.command(self.main, "linux")
        self.assertEqual(args, [wine.name, str(cr6comp.compiler_for(self.main)), "Main.CR6"])

    def test_opencrbasic_runs_native_module(self) -> None:
        with mock.patch.dict(os.environ, {"CR6COMP": "opencrbasic"}):
            self.assertTrue(cr6comp.available("linux"))
            args, env = cr6comp.command(self.main, "linux")
        self.assertEqual(args[1:], ["-m", "opencrbasic", "--no-message-file", "Main.CR6"])
        self.assertIn(str(self.root.resolve() / "opencrbasic" / "src"), env["PYTHONPATH"])


if __name__ == "__main__":
    unittest.main()
