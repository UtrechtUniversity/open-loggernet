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

    def test_string_array_gets_a_column_per_string(self) -> None:
        # The CR6's Status.PortConfig: piece 48, dimensions [4, 12], four 12-byte strings.
        field = pakbus.FieldDef("PortConfig", 11, True, "Smp", "", "", 1, 48, [4, 12])
        table = pakbus.TableDef(1, "Status", 1, 14, 0, [field], 0)
        raw = b"".join(s.ljust(12, b"\0") for s in (b"Input", b"Output", b"SDI12", b""))
        values = pakbus.decode_record(table, raw)
        self.assertEqual(values["PortConfig"], ["Input", "Output", "SDI12", ""])
        out = io.StringIO()
        records.write_records(out, table, [(1, RECORDS[0][1], values)], "csv")
        self.assertEqual(
            out.getvalue().splitlines()[0], "TIMESTAMP,RECORD,PortConfig(1),PortConfig(2),PortConfig(3),PortConfig(4)"
        )

    def test_jsonl_spells_out_array_elements(self) -> None:
        out = io.StringIO()
        records.write_records(out, table(), RECORDS, "jsonl")
        self.assertIn('"Temp(1)": 19.5, "Temp(2)": "NAN", "Ok": true', out.getvalue())


class Tob1Tests(unittest.TestCase):
    def test_tob1_layout_matches_a_loggernet_file(self) -> None:
        # csdf's TOB1_Station_Daily.dat (LoggerNet): CRLF header lines, then little-endian ULONG
        # seconds since 1990, nanoseconds and record, and IEEE4 values; 2014-04-11 is 0x2da89300.
        out = io.BytesIO()
        when = datetime.datetime(2014, 4, 11, tzinfo=datetime.UTC)
        records.write_tob1(out, table(), [(7, when, {"Temp": [13.04, 1.5], "Ok": True})], {"station": "S"})
        head, _, body = out.getvalue().rpartition(b"\r\n")
        self.assertEqual(
            head.split(b"\r\n"),
            [
                b'"TOB1","S","","","","","","Table1"',
                b'"SECONDS","NANOSECONDS","RECORD","Temp(1)","Temp(2)","Ok"',
                b'"SECONDS","NANOSECONDS","RN","Deg C","Deg C",""',
                b'"","","","Avg","Avg","Smp"',
                b'"ULONG","ULONG","ULONG","IEEE4","IEEE4","LONG"',
            ],
        )
        self.assertEqual(body[:16].hex(), "0093a82d0000000007000000d7a35041")
        self.assertEqual(body[20:], b"\xff\xff\xff\xff")


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
