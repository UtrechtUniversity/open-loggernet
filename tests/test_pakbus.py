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


class RangeLogger(pakbus.PakBusLink):
    """Answers Collect Data range requests (mode 6) as the CR300 did on 2026-11-02: from the oldest
    record held when the range starts below it, at most `page` records per reply, and replies in
    the order the requests arrived. The table grows by one record every `grow_every` requests."""

    max_reply_frame = None

    def __init__(self, oldest: int, newest: int, page: int, grow_every: int) -> None:
        self.oldest, self.newest, self.page, self.grow_every = oldest, newest, page, grow_every
        self.requests, self.replies, self._tran, self.node, self.my_node = 0, [], 0, 1, 4094
        self.table = pakbus.TableDef(2, "T", 100000, 14, 60, [pakbus.FieldDef("V", 9, True, "", "", "", 1, 1, [])], 0)

    def reply_body(self, begin: int, end: int) -> bytes:
        self.requests += 1
        if self.requests % self.grow_every == 0:
            self.newest += 1
        numbers = list(range(max(begin, self.oldest), min(end, self.newest + 1)))[: self.page]
        if not numbers:
            return b"\x00\x00"
        body = b"\x00" + struct.pack(">HLH", 2, numbers[0], len(numbers)) + struct.pack(">LL", 0, 0)
        return body + b"".join(struct.pack(">f", n) for n in numbers) + b"\x00"

    def _collect_raw(self, table, mode, params, timeout):
        if mode == pakbus.COLLECT_MOST_RECENT:
            return self.reply_body(self.newest, self.newest + 1)
        return self.reply_body(*struct.unpack(">LL", params))

    def send(self, packet: bytes) -> None:
        lo, hi = struct.unpack_from(">LL", packet, 17)
        self.replies.append(pakbus.Packet(4094, 1, 1, 0x89, packet[9], self.reply_body(lo, hi), b""))

    def receive(self, timeout: float = 5.0):
        return self.replies.pop(0) if self.replies else None


class CollectRangeTests(unittest.TestCase):
    def test_pipelined_collection_of_a_growing_table_has_no_gap(self) -> None:
        for depth in (1, 2, 4):
            link = RangeLogger(oldest=0, newest=999, page=7, grow_every=3)
            numbers = [r[0] for r in link.collect_range(link.table, 0, 1000, timeout=1, depth=depth)]
            self.assertEqual(numbers, list(range(0, 1000)), depth)

    def test_range_below_the_oldest_record_starts_at_the_oldest(self) -> None:
        link = RangeLogger(oldest=40, newest=99, page=9, grow_every=10**9)
        numbers = [r[0] for r in link.collect_recent(link.table, 100000)]
        self.assertEqual(numbers, list(range(40, 100)))

    def test_partial_reply_header(self) -> None:
        # Second fragment of CR6 Status record 1, as LoggerNet received it on 2026-11-02.
        body = bytes.fromhex("00 0001 00000001 800003c6") + b"\x0a" * 3 + b"\x00"
        self.assertTrue(pakbus.is_partial_response(body))
        self.assertEqual(pakbus.parse_partial_response(body), (1, 966, b"\x0a" * 3))


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


class PseudoFileTests(unittest.TestCase):
    """`.DIR2` and `.TDFS` as the CR6 (serial 27946) sent them to LoggerNet on 2026-11-02."""

    DIR2 = bytes.fromhex(
        "02 43 50 55 3a 00 4b ef a0 00 00 00"
        "43 50 55 3a 74 65 6d 70 6c 61 74 65 65 78 61 6d 70 6c 65 2e 63 72 36 00 00 00 03 0b"
        "32 30 32 36 2d 30 32 2d 32 34 20 31 33 3a 31 37 3a 32 32 00 00"
        "43 50 55 3a 54 44 52 41 63 63 65 70 74 61 6e 63 65 2e 43 52 36 00 00 00 04 e7"
        "32 30 32 36 2d 30 32 2d 32 34 20 31 33 3a 34 30 3a 34 38 00 01 02 00"
    )

    def test_directory_lists_drive_free_space_and_files(self) -> None:
        drive, template, program = pakbus.parse_directory(self.DIR2)
        self.assertEqual((drive.name, drive.size, drive.is_drive), ("CPU:", 31408128, True))
        self.assertEqual((template.name, template.size, template.attributes), ("CPU:templateexample.cr6", 779, []))
        self.assertEqual((program.size, program.modified, program.attributes), (1255, "2026-02-24 13:40:48", [1, 2]))

    def test_dir_version_one_gives_free_space_as_an_integer(self) -> None:
        dir1 = b"\x01" + self.DIR2[1:6] + bytes.fromhex("01 df 40 00") + self.DIR2[10:]
        self.assertEqual(pakbus.parse_directory(dir1)[0].size, 31408128)

    def test_tdfs_gives_model_and_tables(self) -> None:
        tdfs = bytes.fromhex(
            "11 43 52 36 2e 31 34 2e 34 2e 30 00 0c ce 32 37 39 34 36 00 00 00 00 00 00 44 03 15 23 02 eb ae 40"
            "4e 6f 20 50 72 6f 67 72 61 6d 0d 0a 00 43 52 36 00 53 74 61 74 75 73 00 00 00 00 01 01 99"
        )
        summary = pakbus.parse_tdfs(tdfs)
        self.assertEqual(
            (summary["os_version"], summary["serial_number"], summary["model"]), ("CR6.14.4.0", "27946", "CR6")
        )
        self.assertEqual(summary["compile_result"], "No Program\r\n")
        self.assertEqual(summary["tables"], [{"name": "Status", "size": 1, "signature": 0x0199}])


if __name__ == "__main__":
    unittest.main()
