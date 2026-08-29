"""Resolve non-GitHub Git URLs to immutable snapshots."""

from __future__ import annotations

from pathlib import Path
import urllib.parse

from ..fetchers.git import GitFetcher
from ..models import ParsedInput, ResolvedSource
from ..utils import hash_tree


class GitResolver:
    def __init__(self, fetcher: GitFetcher | None = None) -> None:
        self.fetcher = fetcher or GitFetcher()

    @staticmethod
    def supports(parsed: ParsedInput) -> bool:
        source = (parsed.source or "").strip()
        if "github.com/" in source.casefold() or "raw.githubusercontent.com/" in source.casefold():
            return False
        return source.startswith(("https://", "ssh://", "git://")) or ("@" in source and ":" in source)

    def resolve(self, parsed: ParsedInput, snapshot_dir: Path) -> ResolvedSource:
        source = (parsed.source or "").strip()
        if source.casefold().startswith("git+"):
            source = source[4:]
        requested_ref = parsed.requested_ref
        if "#" in source and source.startswith(("https://", "ssh://", "git://")):
            split = urllib.parse.urlsplit(source)
            if split.fragment:
                requested_ref = requested_ref or urllib.parse.unquote(split.fragment)
                source = urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))
        result = self.fetcher.fetch(source, snapshot_dir, requested_ref=requested_ref)
        return ResolvedSource(
            kind="git_repository",
            normalized_source=source,
            snapshot_path=str(snapshot_dir.resolve()),
            snapshot_sha256=hash_tree(snapshot_dir),
            repository_url=source,
            requested_ref=requested_ref or result.resolved_ref,
            resolved_commit=result.commit,
            requested_path=parsed.requested_path,
            resolution_method="git-ls-remote-and-archive",
            metadata={"skipped_symbolic_links": list(result.skipped_symbolic_links)},
        )
