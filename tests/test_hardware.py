"""Tests against real loggers on USB. Skipped unless asked for:

    OLN_HARDWARE=1 uv run pytest tests/test_hardware.py            # read-only, plus a scratch file
    OLN_HARDWARE=1 OLN_HARDWARE_PROGRAM=1 uv run pytest tests/test_hardware.py

Every attached Campbell logger is tested; `OLN_HARDWARE_PORTS=COM5,COM7` limits them. The program
tests replace the running program with `tests/hardware/oln_test.crbasic`, which deletes the old
program's data tables, and put the old program back afterwards: run them only on bench loggers.
The tests write and delete `CPU:oln_hw_test.bin`.
"""

import os
import struct
import time
from pathlib import Path

import pytest

from openloggernet import cli, flash, pakbus

HARDWARE = os.environ.get("OLN_HARDWARE") == "1"
PROGRAM = os.environ.get("OLN_HARDWARE_PROGRAM") == "1"
EXTENSIONS = {"CR6": ".CR6", "CR1000X": ".CR1X", "CR300": ".CR300", "CR350": ".CR350"}
TEST_PROGRAM = Path(__file__).parent / "hardware" / "oln_test.crbasic"

pytestmark = pytest.mark.skipif(not HARDWARE, reason="set OLN_HARDWARE=1 to test attached loggers")


def ports() -> list[str]:
    wanted = [p for p in os.environ.get("OLN_HARDWARE_PORTS", "").split(",") if p]
    if not HARDWARE:
        return []
    return wanted or [p.device for p in pakbus.list_loggers()]


@pytest.fixture(scope="module", params=ports())
def link(request):
    with pakbus.PakBusLink(request.param) as opened:
        assert opened.hello(), f"no answer on {request.param}"
        yield opened


def test_identity_agrees_across_messages(link) -> None:
    stats = link.get_program_stats()
    described = link.string_settings()
    summary = pakbus.parse_tdfs(link.file_upload(".TDFS", swath=983))
    assert stats["resp_code"] == 0
    assert stats["model"] == described["Model"] == summary["model"]
    assert link.model in (None, stats["model"])
    assert summary["os_version"] == stats["os_version"]
    assert summary["serial_number"] == stats["serial_number"]


def test_clock_answers_within_50_ms(link) -> None:
    before = time.monotonic()
    link.clock()
    assert time.monotonic() - before < 0.05


def test_status_is_read_whole_and_matches_program_stats(link) -> None:
    stats = link.get_program_stats()
    status = cli.find_table(link.table_definitions(), "Status")
    _, _, values = link.read_record(status)
    assert values["OSVersion"] == stats["os_version"]
    assert str(values["SerialNumber"]) == stats["serial_number"]


def test_directory_shows_the_loaded_program(link) -> None:
    stats = link.get_program_stats()
    entries = {e.name: e for e in link.directory()}
    assert any(e.is_drive and e.size > 0 for e in entries.values())
    if stats["program_name"]:
        assert 1 in entries[stats["program_name"]].attributes


def test_settings_decode_and_name_the_model(link) -> None:
    from openloggernet import settings

    got = link.get_settings()
    defs = settings.load_definitions(got["device_type"], got["major"])
    assert len(got["settings"]) > 50
    if defs:
        model_id = (
            next(sid for sid, d in defs.items() if d.name == "Model")
            if any(d.name == "Model" for d in defs.values())
            else None
        )
        if model_id is not None and model_id in got["settings"]:
            assert settings.decode(defs[model_id], got["settings"][model_id][1]) == link.get_program_stats()["model"]


def test_terminal_answers_with_the_model_prompt(link) -> None:
    _, prompt = link.terminal_open()
    assert prompt.strip().decode("latin-1").endswith(f"{link.get_program_stats()['model']}>")


@pytest.mark.parametrize("size", [1, 975, 976, 977, 2000, 20_000])
def test_file_round_trip(link, size: int) -> None:
    data = os.urandom(size)
    try:
        assert link.file_download("CPU:oln_hw_test.bin", data) == 0
        assert link.file_upload("CPU:oln_hw_test.bin", swath=983) == data
    finally:
        link.file_control("CPU:oln_hw_test.bin", pakbus.FILECTL_DELETE)


# --- tests that replace the program --------------------------------------------------------


@pytest.fixture(scope="module")
def test_program(link):
    if not PROGRAM:
        pytest.skip("set OLN_HARDWARE_PROGRAM=1 to replace the program on the logger")
    before = link.get_program_stats()
    name = f"CPU:oln_hw_test{EXTENSIONS[before['model']]}"
    assert link.file_download(name, TEST_PROGRAM.read_bytes()) == 0
    run(link, name, pakbus.FILECTL_COMPILE_RUN_POWERUP)
    assert "compiled" in link.get_program_stats()["compile_result"].lower()
    time.sleep(7)  # the 50-record Ring table, at 10 Hz, wraps in 5 s
    yield link
    if before["program_name"]:
        run(link, before["program_name"], pakbus.FILECTL_COMPILE_RUN_POWERUP)
    link.file_control(name, pakbus.FILECTL_DELETE)


def run(link, name: str, code: int) -> None:
    resp, hold_off = link.file_control(name, code)
    assert resp == 0
    if hold_off:
        time.sleep(hold_off)
        assert flash.reconnect(link, link._port_name, model=link.model)


def tables(link) -> dict:
    return {t.name: t for t in link.table_definitions()}


def test_every_output_type_reads_back_what_the_program_wrote(test_program) -> None:
    record = test_program.collect_recent(tables(test_program)["Types"], 1)[-1][2]
    assert record["FIeee4"] == 12.5
    assert record["FFp2"] == -3.25
    assert record["FIeee8"] == pytest.approx(0.1, rel=1e-6)
    assert record["LLong"] == -123456
    assert record["LUint2"] == 54321
    # 3e9 does not fit the program's Long: the CR6 and CR1000X give 2^31, the CR300 2^31 - 1.
    assert record["LUint4"] in (2**31 - 1, 2**31)
    assert record["BBool"] is True
    assert record["BBool8"] == [True, False]
    assert record["UUint1"] == [7, 250]
    assert record["SStr"] == "hello"
    assert record["Arr"] == [1.5, -2.25, 1000.125]


def test_a_wrapped_table_is_collected_from_its_oldest_record(test_program) -> None:
    ring = tables(test_program)["Ring"]
    got = test_program.collect_recent(ring, 10**6)
    numbers = [r[0] for r in got]
    assert len(got) == ring.size == 50
    assert numbers == list(range(numbers[0], numbers[0] + 50))
    # The program counts scans; a recompile of the same program keeps the records but restarts the
    # count, so only consecutive differences are fixed.
    counts = [r[2]["Cnt"] for r in got]
    assert [b - a for a, b in zip(counts, counts[1:])] == [1] * 49
    body = test_program._collect_raw(ring, pakbus.COLLECT_RANGE, struct.pack(">LL", 0, numbers[-1] + 1), 5)
    _, from_zero, _ = pakbus.parse_collect_response(ring, body)
    if test_program.get_program_stats()["model"] == "CR300":
        # The CR300 (OS 11.03) answered this range with a block numbered from record 0, 2026-11-02;
        # unexplained, so collect_range on a wrapped CR300 table is not to be trusted yet.
        pytest.xfail("CR300 numbers a range below its oldest record from 0")
    assert from_zero[0][0] >= numbers[0]


def test_pipelining_does_not_change_the_records(test_program) -> None:
    ring = tables(test_program)["Ring"]
    run(test_program, "", pakbus.FILECTL_PAUSE)  # hold the table still
    try:
        newest = test_program.newest_record(ring)
        one = test_program.collect_range(ring, newest - 49, newest + 1, depth=1)
        two = test_program.collect_range(ring, newest - 49, newest + 1, depth=2)
        assert one == two and len(one) == 50
    finally:
        run(test_program, test_program.get_program_stats()["program_name"], pakbus.FILECTL_RESUME)
    assert test_program.get_program_stats()["compile_state"] == 1


def test_values_are_written_and_read_back(test_program) -> None:
    test_program.set_values("Public", "LLong", -5, value_type="long")
    try:
        assert test_program.get_values("Public", "LLong", value_type="long") == [-5]
        assert test_program.get_values("Public", "Arr", value_type="ieee4", swath=3) == [1.5, -2.25, 1000.125]
        assert test_program.get_values("Public", "SStr") == ["hello"]
    finally:
        test_program.set_values("Public", "LLong", -123456, value_type="long")
