import struct
import tempfile
import unittest
from pathlib import Path

from openloggernet import flash, pakbus


class PakBusTests(unittest.TestCase):
    def test_signature_nullifier_drives_signature_to_zero(self) -> None:
        packet = bytes.fromhex("a001effe1001effe090100020708")
        body = packet + pakbus.signature_nullifier(pakbus.signature(packet))
        self.assertEqual(pakbus.signature(body), 0)

    def test_quote_round_trip(self) -> None:
        data = b"A\xbcB\xbdC"
        self.assertEqual(pakbus.unquote(pakbus.quote(data)), data)

    def test_text_filter_removes_valid_frame_inside_text(self) -> None:
        packet = bytes.fromhex("a001effe1001effe090100020708")
        stream = b"before " + pakbus.frame(packet) + b"after\r\n"
        self.assertEqual(pakbus.TextFilter().feed(stream), b"before after\r\n")

    def test_include_discovery_deduplicates_in_source_order(self) -> None:
        source = """
IncludeSection("Definitions", "CPU:First.crb")
IncludeSection("Functions", "CPU:Second.crb")
IncludeSection("Functions", "CPU:First.crb")
"""
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "Test.CR6"
            program.write_text(source, encoding="ascii")
            self.assertEqual(
                flash.included_files(program),
                ["CPU:First.crb", "CPU:Second.crb"],
            )


class CollectDataTests(unittest.TestCase):
    """Collect Data framing, built from replies observed on CR6 serial 18624."""

    @staticmethod
    def table(interval_sec: int) -> pakbus.TableDef:
        field = pakbus.FieldDef("Value", 9, True, "Smp", "", "", 1, 1, [])
        return pakbus.TableDef(4, "T", 100, 14, interval_sec, [field], 0)

    @staticmethod
    def stamp(seconds: int) -> bytes:
        return struct.pack(">LL", seconds, 0)

    def test_field_stores_piece_values_not_its_source_dimensions(self) -> None:
        element = pakbus.FieldDef("FinApp_Neutrons_Avg", 9, True, "Avg", "", "", 2, 1, [2])
        whole = pakbus.FieldDef("TString_Temp_C", 9, True, "Smp", "C", "", 1, 48, [48])
        self.assertEqual((element.count, element.size), (1, 4))
        self.assertEqual((whole.count, whole.size), (48, 192))

    def test_boolean_field_is_four_bytes(self) -> None:
        flag = pakbus.FieldDef("FinApp_Responding", 28, True, "Smp", "", "", 1, 1, [])
        volts = pakbus.FieldDef("Batt_volt", 9, True, "Smp", "", "", 1, 1, [])
        table = pakbus.TableDef(3, "FinApp_Starts", 100, 14, 0, [flag, volts], 0)
        record = pakbus.decode_record(table, b"\xff\xff\xff\xff" + struct.pack(">f", 12.9))
        self.assertEqual(table.record_size, 8)
        self.assertIs(record["FinApp_Responding"], True)
        self.assertAlmostEqual(record["Batt_volt"], 12.9, places=5)

    def test_fp2_matches_value_read_off_the_logger(self) -> None:
        self.assertAlmostEqual(pakbus.decode_fp2(0x44E9), 12.57)

    def test_interval_response_with_two_blocks_is_read_to_the_end(self) -> None:
        body = b"\x00"
        body += struct.pack(">HLH", 4, 10, 2) + self.stamp(1000)
        body += struct.pack(">ff", 1.5, 2.5)
        body += struct.pack(">HLH", 4, 12, 1) + self.stamp(5000)
        body += struct.pack(">f", 3.5)
        body += b"\x01"
        begin, records, more = pakbus.parse_collect_response(self.table(60), body)
        self.assertEqual(begin, 10)
        self.assertEqual([r[0] for r in records], [10, 11, 12])
        self.assertEqual([r[2]["Value"] for r in records], [1.5, 2.5, 3.5])
        self.assertEqual((records[1][1] - records[0][1]).total_seconds(), 60)
        self.assertEqual((records[2][1] - records[0][1]).total_seconds(), 4000)
        self.assertTrue(more)

    def test_event_response_carries_a_timestamp_per_record(self) -> None:
        body = b"\x00" + struct.pack(">HLH", 4, 7, 2)
        body += self.stamp(100) + struct.pack(">f", 1.0)
        body += self.stamp(160) + struct.pack(">f", 2.0)
        body += b"\x00"
        begin, records, more = pakbus.parse_collect_response(self.table(0), body)
        self.assertEqual(begin, 7)
        self.assertEqual((records[1][1] - records[0][1]).total_seconds(), 60)
        self.assertFalse(more)

    def test_bare_response_code_means_no_records(self) -> None:
        self.assertEqual(pakbus.parse_collect_response(self.table(60), b"\x00"), (0, [], False))

    def test_error_response_code_raises(self) -> None:
        with self.assertRaises(pakbus.PakBusError):
            pakbus.parse_collect_response(self.table(60), b"\x07")


class GrowingTable(pakbus.PakBusLink):
    """Stands in for a logger whose table keeps growing while it is being collected."""

    max_reply_frame = None

    def __init__(self, stored: int, page: int, grow_every: int) -> None:
        self.stored, self.page, self.grow_every, self.requests = stored, page, grow_every, 0

    def _collect_fragment(self, table, count, timeout=25.0):
        self.requests += 1
        stored = self.stored
        if self.requests % self.grow_every == 0:
            self.stored += 1
        if count > stored:
            return 0, [], False
        begin = stored - count
        span = min(self.page, count)
        records = [(n, None, {"Value": float(n)}) for n in range(begin, begin + span)]
        return begin, records, begin + span < stored


class CollectRecentTests(unittest.TestCase):
    def test_growth_during_collection_leaves_no_gap(self) -> None:
        table = pakbus.TableDef(2, "T", 100000, 14, 60, [], 0)
        link = GrowingTable(stored=500, page=7, grow_every=3)
        records = link.collect_recent(table, 100000)
        numbers = [r[0] for r in records]
        self.assertEqual(numbers, list(range(numbers[0], numbers[-1] + 1)))
        self.assertGreaterEqual(numbers[-1], 499)


class LimitedLogger(pakbus.PakBusLink):
    """Stands in for a logger whose replies are lost beyond `fits` records."""

    max_reply_frame, model = 64, "CR300"

    def __init__(self, oldest: int, newest: int, fits: int, below_oldest_empty: bool) -> None:
        self.oldest, self.newest, self.fits = oldest, newest, fits
        self.below_oldest_empty = below_oldest_empty

    def _records(self, begin, end):
        return [(n, None, {"Value": float(n)}) for n in range(max(begin, self.oldest), min(end, self.newest + 1))]

    def _collect_fragment(self, table, count, timeout=25.0):
        return self.newest, self._records(self.newest, self.newest + 1), False

    def _collect_range(self, table, begin, end, timeout=5.0):
        if self.below_oldest_empty and begin < self.oldest:
            return []
        records = self._records(begin, end)
        if len(records) > self.fits:
            raise pakbus.PakBusTimeout("reply lost")
        return records


class ReplyLimitTests(unittest.TestCase):
    def table(self, fields) -> pakbus.TableDef:
        return pakbus.TableDef(2, "Table1", 100000, 14, 60, fields, 0)

    def field(self, size_type: int) -> pakbus.FieldDef:
        return pakbus.FieldDef("Value", size_type, True, "", "", "", 1, 1, [])

    def collect(self, logger, count) -> list:
        table = self.table([self.field(7)] * 3)  # three FP2 values, 6 bytes per record
        return [r[0] for r in logger._collect_recent_limited(table, count, 64)]

    def test_budget_leaves_room_for_quoting(self) -> None:
        self.assertEqual(pakbus.reply_budget(64), 43)

    def test_reads_every_held_record_when_ranges_reach_past_the_oldest(self) -> None:
        for empty in (True, False):
            numbers = self.collect(LimitedLogger(5, 40, 3, empty), 1000)
            self.assertEqual(numbers, list(range(5, 41)), empty)

    def test_lost_replies_are_split(self) -> None:
        self.assertEqual(self.collect(LimitedLogger(0, 20, 1, False), 10), list(range(11, 21)))

    def test_record_wider_than_a_reply_is_refused(self) -> None:
        table = self.table([self.field(7)] * 40)
        with self.assertRaises(pakbus.PakBusError):
            LimitedLogger(0, 5, 3, True)._collect_recent_limited(table, 5, 64)


if __name__ == "__main__":
    unittest.main()
