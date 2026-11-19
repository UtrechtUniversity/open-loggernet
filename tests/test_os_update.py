import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openloggernet import os_update, pakbus


class FakeLogger:
    """Answers like the CR300 did on 2026-11-02, refusing the image with `load_code`."""

    def __init__(self, load_code: int) -> None:
        self.load_code, self.calls, self.model = load_code, [], "CR300"

    def get_program_stats(self) -> dict:
        return {"os_version": "CR300.11.3.0", "program_name": "CPU:P.CR300", "power_up_program": "CPU:P.CR300"}

    def file_control(self, name: str, code: int) -> tuple[int, int]:
        self.calls.append((name, code))
        if name.endswith(".obj"):
            return self.load_code, 40
        return 0, 0

    def file_download(self, name: str, data: bytes, **kw) -> int:
        self.calls.append((name, "download", len(data)))
        self.download_kw = kw
        return 0


class SendOsTests(unittest.TestCase):
    def run_send(self, load_code: int) -> FakeLogger:
        link = FakeLogger(load_code)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(os_update.flash, "reconnect", return_value="COM4"),
            mock.patch.object(os_update.time, "sleep"),
        ):
            image = Path(directory) / "CR300.11.03.obj"
            image.write_bytes(b"\0" * 2000)
            if load_code:
                with self.assertRaisesRegex(pakbus.PakBusError, "code 13"):
                    os_update.send_os(link, image, say=lambda _: None)  # type: ignore[arg-type]
            else:
                os_update.send_os(link, image, say=lambda _: None)  # type: ignore[arg-type]
        return link

    def test_sequence_matches_devconfig(self) -> None:
        calls = self.run_send(0).calls
        self.assertEqual(
            calls,
            [
                ("", 8),
                ("CPU:CR300.11.03.obj", "download", 2000),
                ("CPU:CR300.11.03.obj", 1),
                ("CPU:P.CR300", 2),
                ("CPU:P.CR300", 6),
            ],
        )

    def test_image_uses_the_platform_swath(self) -> None:
        # 976-byte fragments go unanswered from a Mac (docs/lessons.md); the platform default applies.
        link = self.run_send(0)
        self.assertNotIn("swath", link.download_kw)

    def test_programs_come_back_when_the_image_is_refused(self) -> None:
        calls = self.run_send(13).calls
        self.assertEqual(calls[-2:], [("CPU:P.CR300", 2), ("CPU:P.CR300", 6)])


class OsCheckTests(unittest.TestCase):
    def test_versions_compare_numerically_without_model_or_trailing_zeros(self) -> None:
        self.assertEqual(os_update.version_tuple("CR6.14.4.0"), (14, 4))
        self.assertEqual(os_update.version_tuple("CR1000X.Std.03.02"), (3, 2))
        self.assertEqual(os_update.version_tuple("11.03.00"), os_update.version_tuple("CR300.11.03"))

    def test_outdated_only_below_the_latest_known(self) -> None:
        self.assertTrue(os_update.os_check("CR6", "CR6.14.4.0")["outdated"])
        latest = os_update.LATEST_OS["CR6"][0]
        self.assertFalse(os_update.os_check(None, f"CR6.{latest}")["outdated"])
        self.assertEqual(os_update.os_check("CR800", "CR800.Std.32")["latest"], None)


if __name__ == "__main__":
    unittest.main()
