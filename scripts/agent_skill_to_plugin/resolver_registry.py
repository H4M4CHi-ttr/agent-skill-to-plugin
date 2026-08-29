"""Resolver dispatch and multi-source composition."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any

from .errors import NeedsInputError, SkillToPluginError
from .limits import DEFAULT_TIMEOUT_SECONDS
from .models import ParsedInput, ResolvedSource
from .utils import hash_tree


class ResolverRegistry:
    """Select a resolver from structured input; no semantic guessing is used."""

    def resolve(
        self,
        parsed: ParsedInput,
        snapshot_dir: Path,
        *,
        source_base: Path,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ResolvedSource:
        if parsed.kind == "multi_source":
            return self._resolve_multi(
                parsed, snapshot_dir, source_base=source_base, timeout_seconds=timeout_seconds,
            )
        if parsed.kind == "npx_skills":
            from .resolvers.npx_skills import NpxSkillsResolver

            return NpxSkillsResolver().resolve(
                parsed, snapshot_dir, source_base=source_base, timeout_seconds=timeout_seconds,
            )
        if parsed.kind == "claude_plugin":
            from .resolvers.claude_plugin import ClaudePluginResolver

            return ClaudePluginResolver().resolve(
                parsed,
                snapshot_dir,
                source_base=source_base,
                timeout=timeout_seconds,
            )
        if parsed.kind == "claude_marketplace":
            raise SkillToPluginError(
                "A Marketplace source was provided without a Claude Plugin install request.",
                code="unknown_plugin",
            )

        source = parsed.source or ""
        from .resolvers.archive import ArchiveResolver
        from .resolvers.github import GitHubResolver
        from .resolvers.git import GitResolver
        from .resolvers.http import HttpSkillResolver
        from .resolvers.local import LocalResolver

        from .fetchers.git import GitFetcher
        from .fetchers.http import HttpFetcher

        archive = ArchiveResolver(HttpFetcher(timeout_seconds=timeout_seconds))
        http_skill = HttpSkillResolver(HttpFetcher(timeout_seconds=timeout_seconds))
        if parsed.kind in {"archive_url"} or archive.supports(parsed, source_base):
            return archive.resolve(parsed, snapshot_dir, source_base=source_base)
        if "github.com/" in source.casefold() or "raw.githubusercontent.com/" in source.casefold() or parsed.kind in {
            "github_repository", "github_path", "github_shorthand",
        }:
            local_candidate = Path(os.path.expanduser(source))
            if not local_candidate.is_absolute():
                local_candidate = source_base / local_candidate
            if parsed.kind == "github_shorthand" and local_candidate.exists():
                raise NeedsInputError(
                    "The value is both a local path and GitHub shorthand; choose the intended source.",
                    prompt_kind="ambiguous_source_kind",
                    choices=[
                        {"id": "github", "label": "GitHub repository", "source": source},
                        {"id": "local", "label": "Local path", "source": str(local_candidate.resolve())},
                    ],
                )
            return GitHubResolver(GitFetcher(timeout=timeout_seconds)).resolve(parsed, snapshot_dir)
        if parsed.kind == "skill_manifest_url" and http_skill.supports(parsed):
            return http_skill.resolve(parsed, snapshot_dir)
        if parsed.kind == "local" or LocalResolver.supports(parsed, source_base):
            return LocalResolver().resolve(parsed, snapshot_dir, source_base=source_base)
        if parsed.kind in {"git_url", "url"} or GitResolver.supports(parsed):
            return GitResolver(GitFetcher(timeout=timeout_seconds)).resolve(parsed, snapshot_dir)
        raise SkillToPluginError(
            "No resolver accepts the normalized source.",
            code="unknown_source",
            details={"input_kind": parsed.kind},
        )

    def _resolve_multi(
        self,
        parsed: ParsedInput,
        snapshot_dir: Path,
        *,
        source_base: Path,
        timeout_seconds: int,
    ) -> ResolvedSource:
        raw_sources = parsed.metadata.get("sources")
        if not isinstance(raw_sources, list) or len(raw_sources) < 2:
            raise SkillToPluginError("Multi-source input has no structured child sources.", code="unknown_input_format")
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        children: list[dict[str, Any]] = []
        try:
            for index, raw_child in enumerate(raw_sources, start=1):
                if not isinstance(raw_child, dict):
                    raise SkillToPluginError("Multi-source child is not structured input.", code="unknown_input_format")
                child = ParsedInput.from_dict(raw_child)
                child_dir = snapshot_dir / f"source-{index}"
                resolved = self.resolve(
                    child,
                    child_dir,
                    source_base=source_base,
                    timeout_seconds=timeout_seconds,
                )
                child_data = resolved.to_dict()
                child_data["snapshot_path"] = child_dir.relative_to(snapshot_dir).as_posix()
                children.append({"input": child.to_dict(), "resolved": child_data})
        except Exception:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise
        labels = [str(item["resolved"].get("normalized_source", "")) for item in children]
        return ResolvedSource(
            kind="multi_source",
            normalized_source=json.dumps(labels, ensure_ascii=False),
            snapshot_path=str(snapshot_dir.resolve()),
            snapshot_sha256=hash_tree(snapshot_dir),
            resolution_method="explicit-multi-source-composition",
            metadata={"sources": children, "source_count": len(children)},
        )
