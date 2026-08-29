"""Resolve a single public HTTPS SKILL.md without executing remote content."""

from __future__ import annotations

from pathlib import Path
import urllib.parse

from ..errors import SkillToPluginError
from ..fetchers.http import HttpFetcher
from ..models import ParsedInput, ResolvedSource
from ..utils import hash_tree


class HttpSkillResolver:
    def __init__(self, http_fetcher: HttpFetcher | None = None) -> None:
        self.http_fetcher = http_fetcher or HttpFetcher()

    @staticmethod
    def supports(parsed: ParsedInput) -> bool:
        source = parsed.source or ""
        return source.startswith("https://") and Path(urllib.parse.urlsplit(source).path).name.casefold() == "skill.md"

    def resolve(self, parsed: ParsedInput, snapshot_dir: Path) -> ResolvedSource:
        source = parsed.source or ""
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        result = self.http_fetcher.fetch(source, snapshot_dir / "SKILL.md")
        return ResolvedSource(
            kind="single_skill_url",
            normalized_source=result.url,
            snapshot_path=str(snapshot_dir.resolve()),
            snapshot_sha256=hash_tree(snapshot_dir),
            requested_path="SKILL.md",
            resolution_method="bounded-https-download",
            metadata={
                "content_type": result.content_type,
                "http_sha256": result.sha256,
                "redirects": list(result.redirects),
                "resource_limit": "Single-file URLs cannot carry sibling scripts, references, or assets.",
            },
        )
