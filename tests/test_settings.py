import struct
import tempfile
import unittest
from pathlib import Path

from openloggernet import settings
from openloggernet.settings import Component, SettingDef

STATION = SettingDef(5, "Station Name", [Component("string", "Station Name")])
ROUTES = SettingDef(
    12,
    "Routes",
    [Component("int4", "Port"), Component("uint2", "Address"), Component("uint2", "Via"), Component("uint2", "Time")],
    repeat=4093,
)
RS232 = SettingDef(135, "RS232 power", [Component("enum", "RS232 Always On", {0: "No", 1: "Yes"})])


class SettingValueTests(unittest.TestCase):
    """Values as the CR300 (serial 33590) reported them to DevConfig on 2026-11-02."""

    def test_string(self) -> None:
        self.assertEqual(settings.decode(STATION, b"BenchCR300\0"), "BenchCR300")
        self.assertEqual(settings.encode(STATION, "BenchCR300"), b"BenchCR300\0")

    def test_repeated_components(self) -> None:
        raw = bytes.fromhex("00 00 00 01 0f fe 0f fe 13 88 00 00 00 01 0f f9 0f f9 13 88")
        self.assertEqual(settings.decode(ROUTES, raw), [[1, 4094, 4094, 5000], [1, 4089, 4089, 5000]])

    def test_enum_by_name(self) -> None:
        self.assertEqual(settings.decode(RS232, b"\0"), "No")
        self.assertEqual(settings.encode(RS232, "yes"), b"\1")

    def test_unknown_or_mismatched_is_hex(self) -> None:
        self.assertEqual(settings.decode(None, b"\1\2"), "01 02")
        self.assertEqual(settings.decode(RS232, b"\0\0"), "00 00")
        self.assertEqual(settings.encode(None, "hex:0a0b"), b"\n\x0b")

    def test_find_by_name_or_number(self) -> None:
        defs = {5: STATION, 12: ROUTES}
        self.assertEqual(settings.find(defs, "station name"), 5)
        self.assertEqual(settings.find(defs, "12"), 12)
        with self.assertRaises(ValueError):
            settings.find(defs, "nope")


class DescriptionLibraryTests(unittest.TestCase):
    def test_device_type_and_includes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lib = Path(directory)
            (lib / "x.dd").write_text(
                '<?xml version="1.0"?><device-description device-type="69" model-no="X">'
                '<setting-catalog major-version="1"><include href="common.ddi"/>'
                '<setting id="120" name="UTC Offset"><int4 name="UTC Offset"/></setting>'
                "</setting-catalog></device-description>"
            )
            (lib / "common.ddi").write_text(
                '<include-contents><setting id="5" name="Station Name"><string name="Station Name" length="63"/>'
                "</setting></include-contents>"
            )
            defs = settings.load_definitions(69, 1, lib)
            self.assertEqual(sorted(defs), [5, 120])
            self.assertEqual(settings.decode(defs[120], struct.pack(">l", -1)), -1)
            self.assertEqual(settings.load_definitions(55, 1, lib), {})


class NamedSettingTests(unittest.TestCase):
    def test_terminal_list_splits_on_the_type_word(self) -> None:
        # Lines as the CR6 (OS 14.4.0) printed them for terminal F, S on 2026-11-19.
        text = (
            "Name                      Type       Range\r\n"
            " StationName               String     \r\n"
            " CPUDriveFree              Long       ReadOnly\r\n"
            " DeleteCardFilesOnMismatch(1)Boolean    \r\n"
            " Beacon(19) (internet)     Long       \r\n"
            " KeepAliveMin              Long       0,5,10,15\r\n"
            "CR6>"
        )
        rows = settings.parse_setting_list(text)
        self.assertEqual(
            [r["name"] for r in rows],
            ["StationName", "CPUDriveFree", "DeleteCardFilesOnMismatch(1)", "Beacon(19) (internet)", "KeepAliveMin"],
        )
        self.assertEqual([r["read_only"] for r in rows], [False, True, False, False, False])
        self.assertEqual(rows[2]["type"], "Boolean")
        self.assertEqual(rows[4]["range"], "0,5,10,15")

    def test_values_are_written_in_the_setting_type(self) -> None:
        self.assertEqual(settings.named_value("String", "Bench"), ("Bench", "ascii"))
        self.assertEqual(settings.named_value("Boolean", "true"), (-1, "long"))
        self.assertEqual(settings.named_value("Uint1", "3"), (3, "long"))
        self.assertEqual(settings.named_value("Float", "1.5"), (1.5, "ieee4"))


if __name__ == "__main__":
    unittest.main()
