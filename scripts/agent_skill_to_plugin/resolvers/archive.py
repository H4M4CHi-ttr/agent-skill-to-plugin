"""Resolve local and public HTTPS archives into safe snapshots."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import urllib.parse

from ..errors import SkillToPluginError
from ..fetchers.archive import extract_archive
from ..fetchers.http import HttpFetcher
from ..models import ParsedInput, ResolvedSource
from ..utils import hash_tree, sha256_file


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


def _looks_like_archive(value: str) -> bool:
    path = urllib.parse.urlsplit(value).path.casefold()
    return path.endswith(ARCHIVE_SUFFIXES)


class ArchiveResolver:
    def __init__(self, http_fetcher: HttpFetcher | None = None) -> None:
        self.http_fetcher = http_fetcher or HttpFetcher()

    @staticmethod
    def supports(parsed: ParsedInput, source_base: Path | None = None) -> bool:
        source = parsed.source or ""
        if source.casefold().startswith("https://"):
            return _looks_like_archive(source)
        candidate = Path(os.path.expanduser(source))
        if not candidate.is_absolute() and source_base:
            candidate = source_base / candidate
        return candidate.is_file() and _looks_like_archive(candidate.name)

    def resolve(self, parsed: ParsedInput, snapshot_dir: Path, *, source_base: Path) -> ResolvedSource:
        source = parsed.source or ""
        remote = source.casefold().startswith("https://")
        if remote:
            with tempfile.TemporaryDirectory(prefix="agent-skill-to-plugin-archive-", dir=snapshot_dir.parent) as temporary_name:
                download = Path(temporary_name) / "source.archive"
                result = self.http_fetcher.fetch(source, download)
                extraction = extract_archive(download, snapshot_dir)
                final_url = result.url
                archive_sha256 = extraction.archive_sha256
                redirects = list(result.redirects)
        else:
            archive = Path(os.path.expanduser(source))
            if not archive.is_absolute():
                archive = source_base / archive
            archive = archive.resolve()
            if not archive.is_file():
                raise SkillToPluginError("Local archive does not exist.", code="unknown_source")
            extraction = extract_archive(archive, snapshot_dir)
            final_url = str(archive)
            archive_sha256 = extraction.archive_sha256
            redirects = []
        return ResolvedSource(
            kind="https_archive" if remote else "local_archive",
            normalized_source=final_url,
            snapshot_path=str(snapshot_dir.resolve()),
            snapshot_sha256=hash_tree(snapshot_dir),
            requested_path=parsed.requested_path,
            resolution_method="bounded-http-and-safe-extract" if remote else "safe-local-extract",
            metadata={
                "archive_sha256": archive_sha256,
                "archive_format": extraction.archive_format,
                "redirects": redirects,
                "file_count": extraction.file_count,
                "total_bytes": extraction.total_bytes,
            },
        )
