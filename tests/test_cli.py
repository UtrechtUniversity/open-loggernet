import datetime
import io
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from openloggernet import cli, pakbus, records


def table() -> pakbus.TableDef:
    temp = pakbus.FieldDef("Temp", 9, True, "Avg", "Deg C", "", 1, 2, [2])
    flag = pakbus.FieldDef("Ok", 28, True, "Smp", "", "", 1, 1, [])
    return pakbus.TableDef(2, "Table1", 100, 14, 60, [temp, flag], 0)


RECORDS = [(7, datetime.datetime(2026, 11, 2, 17, 20, tzinfo=datetime.UTC), {"Temp": [19.5, float("nan")], "Ok": True})]


class RecordFormatTests(unittest.TestCase):
    def test_toa5_matches_loggernet_layout(self) -> None:
        out = io.StringIO()
        env = {"station": "S", "model": "CR300", "serial": "1", "os": "OS", "program": "CPU:P", "program_signature": 9}
        records.write_records(out, table(), RECORDS, "toa5", env)
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                '"TOA5","S","CR300","1","OS","CPU:P","9","Table1"',
                '"TIMESTAMP","RECORD","Temp(1)","Temp(2)","Ok"',
                '"TS","RN","Deg C","Deg C",""',
                '"","","Avg","Avg","Smp"',
                '"2026-11-02 17:20:00",7,19.5,"NAN",-1',
            ],
        )

    def test_jsonl_spells_out_array_elements(self) -> None:
        out = io.StringIO()
        records.write_records(out, table(), RECORDS, "jsonl")
        self.assertIn('"Temp(1)": 19.5, "Temp(2)": "NAN", "Ok": true', out.getvalue())


class PortChoiceTests(unittest.TestCase):
    loggers = [pakbus.LoggerPort("COM4", "CR300", "CR300 (COM4)"), pakbus.LoggerPort("COM5", "CR6", "CR6 (COM5)")]

    def args(self, **kw) -> SimpleNamespace:
        return SimpleNamespace(**({"port": None, "model": None} | kw))

    def test_several_loggers_need_a_choice(self) -> None:
        with mock.patch.object(pakbus, "list_loggers", return_value=self.loggers):
            with self.assertRaisesRegex(cli.CliError, "choose one"):
                cli.choose_port(self.args())
            self.assertEqual(cli.choose_port(self.args(model="cr6")), "COM5")

    @unittest.skipUnless(sys.platform == "win32", "Windows builds the description from the product string")
    def test_windows_model_comes_from_the_description(self) -> None:
        info = SimpleNamespace(product=None, description="CR1000X (COM7)")
        self.assertEqual(pakbus._model_of(info), "CR1000X")


if __name__ == "__main__":
    unittest.main()
