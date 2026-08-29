"""Resolve GitHub shorthand and repository URLs to immutable snapshots."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import urllib.parse

from ..errors import SkillToPluginError
from ..fetchers.git import GitFetcher
from ..fetchers.github_api import is_github_shorthand, parse_github_url
from ..fetchers.http import HttpFetcher
from ..models import ParsedInput, ResolvedSource
from ..utils import hash_tree


class GitHubResolver:
    def __init__(self, fetcher: GitFetcher | None = None) -> None:
        self.fetcher = fetcher or GitFetcher()

    @staticmethod
    def supports(parsed: ParsedInput) -> bool:
        source = (parsed.source or "").strip()
        return is_github_shorthand(source) or "github.com/" in source.casefold() or "raw.githubusercontent.com/" in source.casefold()

    def _license_metadata(self, owner: str, repo: str, commit: str) -> dict[str, str]:
        """Best-effort public GitHub license metadata; content Git remains authoritative."""
        endpoint = (
            f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(repo, safe='')}/license?ref={urllib.parse.quote(commit, safe='')}"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="agent-skill-to-plugin-github-api-") as temporary:
                target = Path(temporary) / "license.json"
                timeout = getattr(self.fetcher, "timeout", 30)
                HttpFetcher(timeout_seconds=min(timeout, 30), max_bytes=2 * 1024 * 1024).fetch(
                    endpoint, target, max_bytes=2 * 1024 * 1024,
                )
                value = json.loads(target.read_text(encoding="utf-8"))
            license_value = value.get("license") if isinstance(value, dict) else None
            if not isinstance(license_value, dict):
                return {"github_license_status": "not-detected"}
            result = {"github_license_status": "detected"}
            for source_key, target_key in (
                ("spdx_id", "github_license"),
                ("name", "github_license_name"),
            ):
                item = license_value.get(source_key)
                if isinstance(item, str) and item:
                    result[target_key] = item
            html_url = value.get("html_url")
            if isinstance(html_url, str) and html_url:
                result["github_license_url"] = html_url
            return result
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SkillToPluginError):
            return {"github_license_status": "unavailable"}

    def resolve(self, parsed: ParsedInput, snapshot_dir: Path) -> ResolvedSource:
        source = (parsed.source or "").strip()
        transport_url: str
        if is_github_shorthand(source):
            repository_url = parse_github_url(source).canonical_repository_url
            transport_url = repository_url
        else:
            parsed_url = urllib.parse.urlsplit(source)
            parts = [urllib.parse.unquote(part) for part in parsed_url.path.split("/") if part]
            if len(parts) < 2:
                raise SkillToPluginError("Incomplete GitHub repository URL.", code="unknown_source")
            repository_url = f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}"
            transport_url = source if parsed_url.scheme.casefold() == "ssh" else repository_url
        refs = self.fetcher.list_refs(transport_url)
        ref_names = {
            name.removeprefix("refs/heads/").removeprefix("refs/tags/")
            for name in refs.refs
        }
        location = parse_github_url(source, ref_names)
        requested_ref = parsed.requested_ref or location.ref
        resolved_ref, commit = self.fetcher.resolve_ref(refs, requested_ref)
        result = self.fetcher.fetch(
            transport_url,
            snapshot_dir,
            requested_ref=requested_ref,
            expected_commit=commit,
        )
        requested_path = parsed.requested_path or location.path
        metadata = {
            "github_url_kind": location.kind,
            "git_transport": "ssh" if transport_url.casefold().startswith("ssh://") else "https",
            "skipped_symbolic_links": list(result.skipped_symbolic_links),
        }
        metadata.update(self._license_metadata(location.owner, location.repo, result.commit))
        return ResolvedSource(
            kind="github_repository" if not requested_path else "github_path",
            normalized_source=location.canonical_repository_url,
            snapshot_path=str(snapshot_dir.resolve()),
            snapshot_sha256=hash_tree(snapshot_dir),
            repository_url=location.canonical_repository_url,
            requested_ref=requested_ref or resolved_ref,
            resolved_commit=result.commit,
            requested_path=requested_path,
            resolution_method="git-ls-remote-and-archive",
            metadata=metadata,
        )
