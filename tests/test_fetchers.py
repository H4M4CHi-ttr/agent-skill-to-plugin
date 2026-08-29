from __future__ import annotations

import base64
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.fetchers import archive as archive_module
from agent_skill_to_plugin.fetchers.archive import extract_archive
from agent_skill_to_plugin.fetchers.http import HttpFetchResult, HttpFetcher
from agent_skill_to_plugin.fetchers.npm import NpmFetcher


PUBLIC_IP = "93.184.216.34"


def resolver(host: str, port: int):
    address = "127.0.0.1" if host in {"private.example", "localhost"} else PUBLIC_IP
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True
        self._body.close()


class SequenceOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[str] = []

    def open(self, request, timeout):
        self.requests.append(request.full_url)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def make_zip(path: Path, entries: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)


def make_tgz_bytes(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, value in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return stream.getvalue()


class HttpFetcherTests(unittest.TestCase):
    def test_redirect_target_is_revalidated_before_second_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "download.bin"
            opener = SequenceOpener(
                [FakeResponse(b"", 302, {"Location": "https://private.example/secret"})]
            )
            fetcher = HttpFetcher(resolver=resolver, opener=opener)
            with self.assertRaises(SkillToPluginError) as raised:
                fetcher.fetch("https://public.example/start", destination)
            self.assertEqual("security_rejected", raised.exception.code)
            self.assertEqual(["https://public.example/start"], opener.requests)
            self.assertFalse(destination.exists())

    def test_public_redirect_and_bounded_download_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "download.bin"
            opener = SequenceOpener(
                [
                    FakeResponse(b"", 302, {"Location": "https://cdn.example/file"}),
                    FakeResponse(b"payload", 200, {"Content-Length": "7"}),
                ]
            )
            result = HttpFetcher(resolver=resolver, opener=opener).fetch(
                "https://public.example/start", destination
            )
            self.assertEqual(b"payload", destination.read_bytes())
            self.assertEqual(("https://cdn.example/file",), result.redirects)
            self.assertEqual(hashlib.sha256(b"payload").hexdigest(), result.sha256)

    def test_streaming_limit_does_not_trust_missing_content_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "too-large.bin"
            opener = SequenceOpener([FakeResponse(b"12345")])
            fetcher = HttpFetcher(resolver=resolver, opener=opener, max_bytes=4)
            with self.assertRaises(SkillToPluginError) as raised:
                fetcher.fetch("https://public.example/file", destination)
            self.assertEqual("security_rejected", raised.exception.code)
            self.assertFalse(destination.exists())

    def test_userinfo_and_secret_query_are_rejected_before_request(self) -> None:
        opener = SequenceOpener([])
        fetcher = HttpFetcher(resolver=resolver, opener=opener)
        for url in (
            "https://user:password@public.example/file",
            "https://public.example/file?api_key=secret",
        ):
            with self.subTest(url=url):
                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaises(SkillToPluginError) as raised:
                        fetcher.fetch(url, Path(temporary) / "file")
                    self.assertEqual("security_rejected", raised.exception.code)
        self.assertEqual([], opener.requests)


class ArchiveFetcherTests(unittest.TestCase):
    def test_tar_member_limit_is_enforced_while_headers_are_streamed(self) -> None:
        class StreamingTar:
            def __init__(self) -> None:
                self.headers_read = 0

            def __iter__(self):
                for index in range(10):
                    self.headers_read += 1
                    if self.headers_read > 3:
                        raise AssertionError("tar headers were read beyond the configured limit")
                    yield tarfile.TarInfo(f"member-{index}")

            def getmembers(self):
                raise AssertionError("getmembers must not materialize untrusted tar headers")

        archive = StreamingTar()
        with mock.patch.object(archive_module, "MAX_FILES", 2):
            with self.assertRaises(SkillToPluginError) as raised:
                archive_module._tar_entries(archive)  # type: ignore[arg-type]
        self.assertEqual("security_rejected", raised.exception.code)
        self.assertEqual(3, archive.headers_read)

    def test_safe_zip_and_tgz_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "safe.zip"
            make_zip(zip_path, {"skill/SKILL.md": "fixture"})
            zip_result = extract_archive(zip_path, root / "zip-out")
            self.assertEqual("zip", zip_result.archive_format)
            self.assertEqual(1, zip_result.file_count)

            tgz_path = root / "safe.tgz"
            tgz_path.write_bytes(make_tgz_bytes({"skill/SKILL.md": b"fixture"}))
            tgz_result = extract_archive(tgz_path, root / "tgz-out")
            self.assertEqual("tar.gz", tgz_result.archive_format)
            self.assertEqual(1, tgz_result.file_count)

    def test_zip_slip_and_tar_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "slip.zip"
            make_zip(zip_path, {"../outside.txt": "owned"})
            with self.assertRaises(SkillToPluginError) as zip_error:
                extract_archive(zip_path, root / "zip-out")
            self.assertEqual("security_rejected", zip_error.exception.code)
            self.assertFalse((root / "outside.txt").exists())

            tar_path = root / "traversal.tar"
            with tarfile.open(tar_path, "w") as archive:
                value = b"owned"
                info = tarfile.TarInfo("../../outside.txt")
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            with self.assertRaises(SkillToPluginError) as tar_error:
                extract_archive(tar_path, root / "tar-out")
            self.assertEqual("security_rejected", tar_error.exception.code)
            self.assertFalse((root / "outside.txt").exists())

    def test_zip_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "symlink.zip"
            with zipfile.ZipFile(path, "w") as archive:
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "target")
            with self.assertRaises(SkillToPluginError) as raised:
                extract_archive(path, root / "out")
            self.assertEqual("security_rejected", raised.exception.code)

    def test_tar_hardlink_and_special_members_are_rejected(self) -> None:
        member_types = {
            "hardlink": tarfile.LNKTYPE,
            "character-device": tarfile.CHRTYPE,
            "fifo": tarfile.FIFOTYPE,
        }
        for label, member_type in member_types.items():
            with self.subTest(member=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / f"{label}.tar"
                with tarfile.open(path, "w") as archive:
                    info = tarfile.TarInfo(label)
                    info.type = member_type
                    if member_type == tarfile.LNKTYPE:
                        info.linkname = "target"
                    archive.addfile(info)
                with self.assertRaises(SkillToPluginError) as raised:
                    extract_archive(path, root / "out")
                self.assertEqual("security_rejected", raised.exception.code)

    def test_case_unicode_and_file_directory_collisions_are_rejected(self) -> None:
        archives = {
            "case": {"A/file": "a", "a/other": "b"},
            "unicode": {"caf\u00e9/file": "a", "cafe\u0301/other": "b"},
            "file-dir": {"boundary": "a", "boundary/child": "b"},
        }
        for label, entries in archives.items():
            with self.subTest(collision=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / f"{label}.zip"
                make_zip(path, entries)
                with self.assertRaises(SkillToPluginError) as raised:
                    extract_archive(path, root / "out")
                self.assertEqual("security_rejected", raised.exception.code)

    def test_all_archive_limits_are_enforced_before_output(self) -> None:
        cases = [
            ("MAX_FILES", 1, {"one": "1", "two": "2"}),
            ("MAX_MEMBER_BYTES", 1, {"member": "12"}),
            ("MAX_TOTAL_BYTES", 1, {"one": "1", "two": "2"}),
            ("MAX_DEPTH", 2, {"a/b/c": "1"}),
            ("MAX_PATH_CHARS", 3, {"long-name": "1"}),
        ]
        for constant, limit, entries in cases:
            with self.subTest(limit=constant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / "limited.zip"
                make_zip(path, entries)
                with mock.patch.object(archive_module, constant, limit):
                    with self.assertRaises(SkillToPluginError) as raised:
                        extract_archive(path, root / "out")
                self.assertEqual("security_rejected", raised.exception.code)
                self.assertFalse((root / "out").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "compressed.zip"
            make_zip(path, {"file": "data"})
            with mock.patch.object(archive_module, "MAX_COMPRESSED_BYTES", 1):
                with self.assertRaises(SkillToPluginError) as raised:
                    extract_archive(path, root / "out")
            self.assertEqual("security_rejected", raised.exception.code)


class FakeNpmHttpFetcher:
    def __init__(self, metadata: dict, tarball: bytes) -> None:
        self.metadata = metadata
        self.tarball = tarball
        self.calls: list[tuple[str, int | None]] = []

    def fetch(self, url: str, destination: Path, *, max_bytes: int | None = None) -> HttpFetchResult:
        self.calls.append((url, max_bytes))
        body = json.dumps(self.metadata).encode("utf-8") if len(self.calls) == 1 else self.tarball
        destination.write_bytes(body)
        return HttpFetchResult(
            url=url,
            path=destination,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )


class NpmFetcherTests(unittest.TestCase):
    def test_registry_tarball_is_extracted_without_lifecycle_or_subprocess(self) -> None:
        package_json = json.dumps(
            {"name": "fixture-package", "scripts": {"prepare": "echo owned > lifecycle-ran"}}
        ).encode("utf-8")
        tarball = make_tgz_bytes(
            {
                "package/package.json": package_json,
                "package/SKILL.md": b"---\nname: npm-fixture\ndescription: fixture\n---\nbody\n",
            }
        )
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode("ascii")
        metadata = {
            "name": "fixture-package",
            "dist-tags": {"latest": "1.2.3"},
            "versions": {
                "1.2.3": {
                    "version": "1.2.3",
                    "dist": {
                        "tarball": "https://cdn.example/fixture-package.tgz",
                        "integrity": integrity,
                    },
                }
            },
        }
        fake_http = FakeNpmHttpFetcher(metadata, tarball)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "snapshot"
            with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess must not run")):
                result = NpmFetcher(fake_http).fetch("fixture-package@latest", destination)
            self.assertEqual("1.2.3", result.version)
            self.assertEqual(destination / "package", result.package_root)
            self.assertTrue((result.package_root / "SKILL.md").is_file())
            self.assertFalse((root / "lifecycle-ran").exists())
            self.assertEqual(2, len(fake_http.calls))
            self.assertIn("registry.npmjs.org/fixture-package", fake_http.calls[0][0])
            self.assertEqual("https://cdn.example/fixture-package.tgz", fake_http.calls[1][0])

    def test_bad_registry_integrity_rejects_before_extraction(self) -> None:
        tarball = make_tgz_bytes({"package/SKILL.md": b"fixture"})
        metadata = {
            "name": "fixture-package",
            "dist-tags": {"latest": "1.0.0"},
            "versions": {
                "1.0.0": {
                    "version": "1.0.0",
                    "dist": {
                        "tarball": "https://cdn.example/package.tgz",
                        "integrity": "sha512-" + base64.b64encode(b"wrong").decode("ascii"),
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with self.assertRaises(SkillToPluginError) as raised:
                NpmFetcher(FakeNpmHttpFetcher(metadata, tarball)).fetch("fixture-package", destination)
            self.assertEqual("security_rejected", raised.exception.code)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
