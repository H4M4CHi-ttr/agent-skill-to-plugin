from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock

from agent_skill_to_plugin.errors import SkillToPluginError
import agent_skill_to_plugin.validation as validation


class TreeSecurityTests(unittest.TestCase):
    def test_env_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("TOKEN=fixture\n", encoding="utf-8")
            with self.assertRaisesRegex(SkillToPluginError, "credential") as raised:
                validation.validate_tree(root)
            self.assertEqual(raised.exception.code, "security_rejected")

    def test_private_key_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "innocent.txt").write_text(
                "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n",
                encoding="utf-8",
            )
            with self.assertRaises(SkillToPluginError) as raised:
                validation.validate_tree(root)
            self.assertEqual(raised.exception.code, "security_rejected")

    def test_private_key_after_prefix_and_across_stream_boundary_is_rejected(self) -> None:
        marker = b"-----BEGIN PRIVATE KEY-----\nfixture\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Split the marker across the validator's 1 MiB read boundary and
            # place it well beyond the former 128 KiB prefix-only scan.
            prefix_size = validation._FILE_SCAN_CHUNK_BYTES - 10
            (root / "late-key.txt").write_bytes(b"x" * prefix_size + marker)
            with self.assertRaises(SkillToPluginError) as raised:
                validation.validate_tree(root)
            self.assertEqual("security_rejected", raised.exception.code)

    def test_pgp_private_key_block_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "innocent.asc").write_text(
                "-----BEGIN PGP PRIVATE KEY BLOCK-----\nfixture\n",
                encoding="utf-8",
            )
            with self.assertRaises(SkillToPluginError) as raised:
                validation.validate_tree(root)
            self.assertEqual("security_rejected", raised.exception.code)

    def test_large_safe_file_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"safe fixture\n" * 100_000
            (root / "large-safe.txt").write_bytes(payload)
            result = validation.validate_tree(root)
            self.assertEqual(1, result.file_count)
            self.assertEqual(len(payload), result.total_bytes)

    def test_file_count_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.txt").write_text("1", encoding="utf-8")
            (root / "two.txt").write_text("2", encoding="utf-8")
            with mock.patch.object(validation, "MAX_FILES", 1):
                with self.assertRaises(SkillToPluginError) as raised:
                    validation.validate_tree(root)
            self.assertEqual(raised.exception.code, "security_rejected")

    def test_case_and_nfc_collision_is_platform_independent(self) -> None:
        seen: dict[str, tuple[str, str]] = {}
        validation._register_path(PurePosixPath("Caf\u00e9.txt"), "file", seen)
        with self.assertRaises(SkillToPluginError):
            validation._register_path(PurePosixPath("cafe\u0301.TXT"), "file", seen)

    def test_file_directory_collision_is_rejected(self) -> None:
        seen: dict[str, tuple[str, str]] = {}
        validation._register_path(PurePosixPath("Resource"), "file", seen)
        with self.assertRaises(SkillToPluginError):
            validation._register_path(PurePosixPath("resource"), "directory", seen)

    def test_windows_reserved_name_is_rejected_on_every_os(self) -> None:
        with self.assertRaises(SkillToPluginError):
            validation._validate_relative_path(PurePosixPath("references/CON.txt"))

    def test_windows_trailing_dot_and_space_are_rejected_on_every_os(self) -> None:
        for path in (PurePosixPath("references/name."), PurePosixPath("references/name ")):
            with self.subTest(path=path.as_posix()), self.assertRaises(SkillToPluginError):
                validation._validate_relative_path(path)

    def test_symbolic_link_is_rejected_when_platform_can_create_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.txt"
            target.write_text("fixture", encoding="utf-8")
            link = root / "link.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(SkillToPluginError) as raised:
                validation.validate_tree(root)
            self.assertEqual(raised.exception.code, "security_rejected")

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "mkfifo"), "FIFO fixture requires POSIX")
    def test_special_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / "pipe")
            with self.assertRaises(SkillToPluginError) as raised:
                validation.validate_tree(root)
            self.assertEqual(raised.exception.code, "security_rejected")


if __name__ == "__main__":
    unittest.main()
