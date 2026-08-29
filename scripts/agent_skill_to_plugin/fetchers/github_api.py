"""Pure GitHub URL/ref parsing plus optional public API metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Callable
import urllib.parse

from ..errors import SkillToPluginError
from ..utils import validate_url_credentials


@dataclass(frozen=True)
class GitHubLocation:
    owner: str
    repo: str
    kind: str
    ref: str | None
    path: str | None
    canonical_repository_url: str


def is_github_shorthand(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value.strip()))


def _longest_ref_and_path(parts: list[str], ref_names: set[str]) -> tuple[str | None, str | None]:
    decoded = [urllib.parse.unquote(part) for part in parts]
    joined_candidates = [
        ("/".join(decoded[:index]), index)
        for index in range(1, len(decoded) + 1)
    ]
    matching = [item for item in joined_candidates if item[0] in ref_names]
    if matching:
        # Keep the count of original URL path segments. A slash may have been
        # encoded inside one segment (`feature%2Fx`), so splitting the decoded
        # ref would consume one unrelated repository path segment.
        ref, consumed = max(matching, key=lambda item: (item[0].count("/"), len(item[0])))
        path = "/".join(decoded[consumed:]) or None
        return ref, path
    if decoded and re.fullmatch(r"[0-9a-fA-F]{7,40}", decoded[0]):
        return decoded[0].lower(), "/".join(decoded[1:]) or None
    return None, "/".join(decoded) or None


def parse_github_url(value: str, ref_names: set[str] | None = None) -> GitHubLocation:
    """Parse GitHub URLs using the longest advertised ref match.

    ``ref_names`` should contain branch and tag names without ``refs/heads`` or
    ``refs/tags`` prefixes. It is what makes ``feature/x`` distinguishable from
    ref ``feature`` plus path ``x``.
    """
    raw = value.strip()
    if is_github_shorthand(raw):
        owner, repo = raw.removesuffix(".git").split("/", 1)
        return GitHubLocation(owner, repo, "repository", None, None, f"https://github.com/{owner}/{repo}")
    parsed = urllib.parse.urlsplit(raw)
    validate_url_credentials(raw, allow_username=parsed.scheme.casefold() == "ssh")
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise SkillToPluginError("Incomplete raw GitHub URL.", code="unknown_source")
        owner, repo = urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[1]).removesuffix(".git")
        ref, path = _longest_ref_and_path(parts[2:], ref_names or set())
        return GitHubLocation(owner, repo, "raw", ref, path, f"https://github.com/{owner}/{repo}")
    if host not in {"github.com", "www.github.com"} or len(parts) < 2:
        raise SkillToPluginError("The URL is not a supported GitHub repository URL.", code="unknown_source")
    owner = urllib.parse.unquote(parts[0])
    repo = urllib.parse.unquote(parts[1]).removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise SkillToPluginError("Invalid GitHub owner or repository name.", code="unknown_source")
    canonical = f"https://github.com/{owner}/{repo}"
    if len(parts) == 2:
        return GitHubLocation(owner, repo, "repository", None, None, canonical)
    marker = parts[2].casefold()
    if marker not in {"tree", "blob", "raw"}:
        # A repository-adjacent path is preserved for deterministic discovery.
        return GitHubLocation(owner, repo, "adjacent", None, "/".join(map(urllib.parse.unquote, parts[2:])), canonical)
    ref, path = _longest_ref_and_path(parts[3:], ref_names or set())
    if ref is None:
        raise SkillToPluginError(
            "Could not distinguish the GitHub ref from the repository path.",
            code="unknown_source",
            details={"url_kind": marker},
        )
    return GitHubLocation(owner, repo, marker, ref, path, canonical)


class GitHubApiClient:
    """Small injectable client for license metadata and marketplace search.

    The resolver does not depend on API success: rate-limit or authentication
    failures become diagnostics and Git remains the content transport.
    """

    def __init__(self, get_json: Callable[[str, dict[str, str]], dict[str, Any]]) -> None:
        self._get_json = get_json

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def license(self, owner: str, repo: str, ref: str | None = None) -> dict[str, Any] | None:
        suffix = f"?ref={urllib.parse.quote(ref, safe='')}" if ref else ""
        data = self._get_json(
            f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/license{suffix}",
            self._headers(),
        )
        license_data = data.get("license") if isinstance(data, dict) else None
        if not isinstance(license_data, dict):
            return None
        return {
            "source": "github_api",
            "spdx_id": license_data.get("spdx_id"),
            "name": license_data.get("name"),
            "url": data.get("html_url"),
        }

    def search_marketplaces(self, marketplace: str, plugin: str) -> list[dict[str, Any]]:
        query = f'path:.claude-plugin filename:marketplace.json "{marketplace}" "{plugin}"'
        data = self._get_json(
            "https://api.github.com/search/code?q=" + urllib.parse.quote(query),
            self._headers(),
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]
