"""Normalize ``npx skills add`` acquisition into a pinned ``ResolvedSource``."""

from __future__ import annotations

from pathlib import Path
import re
import urllib.parse

from ..errors import SkillToPluginError
from ..fetchers.npx import NpxFetcher
from ..limits import DEFAULT_TIMEOUT_SECONDS
from ..models import ParsedInput, ResolvedSource
from ..utils import hash_tree, sanitize_text


def _repository_url(source: str) -> str | None:
    shorthand = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", source)
    if shorthand:
        return f"https://github.com/{shorthand.group(1)}/{shorthand.group(2)}"

    candidate = source[4:] if source.casefold().startswith("git+") else source
    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    if host in {"github.com", "www.github.com"}:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 2:
            owner = urllib.parse.unquote(segments[0])
            repository = urllib.parse.unquote(segments[1]).removesuffix(".git")
            return f"https://github.com/{owner}/{repository}"
    if parsed.scheme in {"git", "ssh", "http", "https"} and host:
        path = parsed.path
        if path.endswith(".git"):
            path = path[:-4]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))

    scp = re.fullmatch(r"(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)", source)
    if scp:
        path = scp.group("path").removesuffix(".git")
        return f"ssh://{scp.group('host')}/{path}"
    return None


class NpxSkillsResolver:
    """Acquire one parsed npx request and pin its exact on-disk snapshot hash."""

    def __init__(self, fetcher: NpxFetcher | None = None) -> None:
        self.fetcher = fetcher or NpxFetcher()

    @staticmethod
    def supports(parsed: ParsedInput) -> bool:
        return parsed.kind == "npx_skills"

    def resolve(
        self,
        parsed: ParsedInput,
        snapshot_dir: Path,
        *,
        source_base: Path | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ResolvedSource:
        if parsed.kind != "npx_skills":
            raise SkillToPluginError("NpxSkillsResolver requires an `npx_skills` input.", code="unknown_input_format")
        if parsed.source is None:
            raise SkillToPluginError("The parsed npx request has no source.", code="unknown_source")

        fetched = self.fetcher.fetch(
            parsed,
            snapshot_dir,
            source_base=source_base,
            timeout_seconds=timeout_seconds,
        )
        snapshot = fetched.snapshot_path.resolve()
        digest = hash_tree(snapshot)
        installed_paths = tuple(
            directory.relative_to(snapshot).as_posix()
            for directory in fetched.installed_skill_dirs
        )
        metadata = {
            "fetcher": "npx_skills",
            "package_spec": str(parsed.metadata.get("package_spec", "skills")),
            "requested_skills": list(parsed.requested_skills),
            "select_all": parsed.select_all,
            "full_depth": bool(parsed.metadata.get("full_depth", False)),
            "installed_skill_paths": list(installed_paths),
            "rewritten_argv": [sanitize_text(part) for part in fetched.argv],
            "npx_stdout": fetched.stdout,
            "npx_stderr": fetched.stderr,
            "snapshot_pin": "sha256",
        }
        return ResolvedSource(
            kind="npx_skills",
            normalized_source=fetched.resolved_source,
            snapshot_path=str(snapshot),
            snapshot_sha256=digest,
            repository_url=_repository_url(fetched.resolved_source),
            requested_ref=parsed.requested_ref,
            resolved_commit=None,
            requested_path=parsed.requested_path,
            original_plugin_name=None,
            resolution_method="npx_skills_cli",
            metadata=metadata,
        )


def resolve_npx_skills(
    parsed: ParsedInput,
    snapshot_dir: Path,
    *,
    source_base: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    fetcher: NpxFetcher | None = None,
) -> ResolvedSource:
    """Functional convenience wrapper used by lightweight callers and tests."""

    return NpxSkillsResolver(fetcher).resolve(
        parsed,
        snapshot_dir,
        source_base=source_base,
        timeout_seconds=timeout_seconds,
    )


__all__ = ["NpxSkillsResolver", "resolve_npx_skills"]
