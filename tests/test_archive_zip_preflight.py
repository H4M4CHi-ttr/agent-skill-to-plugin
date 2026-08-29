from __future__ import annotations

from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.fetchers import archive as archive_module


EOCD = struct.Struct("<4s4H2LH")
ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
ZIP64_LOCATOR = struct.Struct("<4sLQL")


def ordinary_eocd(
    entries: int,
    *,
    disk: int = 0,
    central_disk: int = 0,
    entries_this_disk: int | None = None,
    central_size: int = 0,
    central_offset: int = 0,
    comment: bytes = b"",
) -> bytes:
    return EOCD.pack(
        b"PK\x05\x06",
        disk,
        central_disk,
        entries if entries_this_disk is None else entries_this_disk,
        entries,
        central_size,
        central_offset,
        len(comment),
    ) + comment


def zip64_eocd(
    entries: int,
    *,
    disk: int = 0,
    central_disk: int = 0,
    entries_this_disk: int | None = None,
    record_size: int = 44,
    locator_disk: int = 0,
    locator_offset: int = 0,
    disk_count: int = 1,
    ordinary_entries: int = 0xFFFF,
) -> bytes:
    record = ZIP64_EOCD.pack(
        b"PK\x06\x06",
        record_size,
        45,
        45,
        disk,
        central_disk,
        entries if entries_this_disk is None else entries_this_disk,
        entries,
        0,
        0,
    )
    locator = ZIP64_LOCATOR.pack(
        b"PK\x06\x07",
        locator_disk,
        locator_offset,
        disk_count,
    )
    end = EOCD.pack(
        b"PK\x05\x06",
        0,
        0,
        ordinary_entries,
        ordinary_entries,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    return record + locator + end


class ZipEocdPreflightTests(unittest.TestCase):
    def test_standard_declared_count_is_rejected_before_zipfile_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "too-many.zip"
            archive.write_bytes(ordinary_eocd(archive_module.MAX_FILES + 1))

            with mock.patch.object(
                archive_module.zipfile,
                "ZipFile",
                side_effect=AssertionError("central directory must not be opened"),
            ) as zip_file:
                with self.assertRaises(SkillToPluginError) as raised:
                    archive_module._extract_zip(archive, root / "out")

            self.assertEqual("security_rejected", raised.exception.code)
            self.assertEqual(
                archive_module.MAX_FILES + 1,
                raised.exception.details["declared_entries"],
            )
            zip_file.assert_not_called()

    def test_zip64_declared_count_is_rejected_before_zipfile_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "too-many-zip64.zip"
            archive.write_bytes(zip64_eocd(archive_module.MAX_FILES + 1))

            with mock.patch.object(
                archive_module.zipfile,
                "ZipFile",
                side_effect=AssertionError("central directory must not be opened"),
            ) as zip_file:
                with self.assertRaises(SkillToPluginError) as raised:
                    archive_module._extract_zip(archive, root / "out")

            self.assertEqual("security_rejected", raised.exception.code)
            self.assertEqual(
                archive_module.MAX_FILES + 1,
                raised.exception.details["declared_entries"],
            )
            zip_file.assert_not_called()

    def test_standard_and_zip64_multidisk_declarations_are_rejected(self) -> None:
        payloads = {
            "standard-disk": ordinary_eocd(0, disk=1, central_disk=1),
            "standard-split-count": ordinary_eocd(1, entries_this_disk=0),
            "zip64-locator-disks": zip64_eocd(0, disk_count=2),
            "zip64-record-disk": zip64_eocd(0, disk=1),
            "zip64-split-count": zip64_eocd(1, entries_this_disk=0),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, payload in payloads.items():
                with self.subTest(label=label):
                    archive = root / f"{label}.zip"
                    archive.write_bytes(payload)
                    with self.assertRaises(SkillToPluginError) as raised:
                        archive_module.extract_archive(archive, root / f"{label}-out")
                    self.assertEqual("security_rejected", raised.exception.code)

    def test_missing_or_malformed_zip64_records_are_rejected(self) -> None:
        missing_locator = ordinary_eocd(
            0xFFFF,
            entries_this_disk=0xFFFF,
            central_size=0xFFFFFFFF,
            central_offset=0xFFFFFFFF,
        )
        malformed_record_size = zip64_eocd(0, record_size=43)
        impossible_locator_offset = zip64_eocd(0, locator_offset=1)
        contradictory_records = zip64_eocd(0, ordinary_entries=1)
        payloads = {
            "missing-locator": missing_locator,
            "short-record": malformed_record_size,
            "impossible-offset": impossible_locator_offset,
            "contradictory-records": contradictory_records,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, payload in payloads.items():
                with self.subTest(label=label):
                    archive = root / f"{label}.zip"
                    archive.write_bytes(payload)
                    with self.assertRaises(SkillToPluginError) as raised:
                        archive_module.extract_archive(archive, root / f"{label}-out")
                    self.assertEqual("security_rejected", raised.exception.code)

    def test_small_empty_zip64_fixture_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "empty-zip64.zip"
            archive.write_bytes(zip64_eocd(0))

            result = archive_module.extract_archive(archive, root / "out")

            self.assertEqual("zip", result.archive_format)
            self.assertEqual(0, result.file_count)
            self.assertEqual(0, result.total_bytes)
            self.assertTrue((root / "out").is_dir())


if __name__ == "__main__":
    unittest.main()
