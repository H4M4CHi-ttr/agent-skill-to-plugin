"""Deterministic discovery of Agent Skill boundaries in a pinned snapshot."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Iterable
import urllib.parse

from .errors import SkillToPluginError
from .models import ResolvedSource, SkillCandidate
from .utils import ensure_within, sanitize_text
from .validation import build_skill_candidate, unicodedata_sort_key, validate_tree


PRIORITY_EXACT = 0
PRIORITY_DIRECT = 10
PRIORITY_ANCESTOR = 20
PRIORITY_DESCENDANT = 30
PRIORITY_STANDARD = 40
PRIORITY_REPOSITORY = 50


def _relative_request_path(value: str | None) -> PurePosixPath | None:
    if value is None or not value.strip():
        return None
    decoded = urllib.parse.unquote(value.strip()).replace("\\", "/")
    if decoded.startswith(("/", "//")) or (len(decoded) >= 2 and decoded[1] == ":"):
        raise SkillToPluginError(
            f"Requested repository path must be relative: `{sanitize_text(value)}`.",
            code="security_rejected",
        )
    path = PurePosixPath(decoded)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SkillToPluginError(
            f"Requested repository path is unsafe: `{sanitize_text(value)}`.",
            code="security_rejected",
        )
    return path


def _sorted_manifests(paths: Iterable[Path], root: Path) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        if path.is_file() and path.name == "SKILL.md":
            relative = path.resolve().relative_to(root).as_posix()
            unique[relative] = path.resolve()
    return [unique[key] for key in sorted(unique, key=unicodedata_sort_key)]


def _manifest_name(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _nearest_plugin_name(skill_md: Path, root: Path, fallback: str | None) -> str | None:
    current = skill_md.parent
    while True:
        plugin_manifest = current / ".claude-plugin" / "plugin.json"
        if plugin_manifest.is_file():
            return _manifest_name(plugin_manifest) or current.name or fallback
        if current == root:
            break
        current = current.parent

    relative_parts = skill_md.relative_to(root).parts
    # Common marketplace shape: plugins/<plugin-name>/skills/<skill>/SKILL.md
    if len(relative_parts) >= 3 and relative_parts[0].casefold() == "plugins":
        return relative_parts[1]
    return fallback


def _declared_skill_manifests(plugin_root: Path, snapshot_root: Path) -> list[Path]:
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        return []
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Invalid plugin manifests are diagnosed by the Claude resolver.  They
        # must not turn into path instructions here.
        return []
    if not isinstance(value, dict):
        return []
    declared = value.get("skills")
    if isinstance(declared, str):
        entries = [declared]
    elif isinstance(declared, list):
        entries = [item for item in declared if isinstance(item, str)]
    else:
        entries = []

    results: list[Path] = []
    for entry in entries:
        relative = _relative_request_path(entry.removeprefix("./"))
        if relative is None:
            continue
        target = ensure_within(plugin_root / Path(*relative.parts), snapshot_root)
        if target.is_file() and target.name == "SKILL.md":
            results.append(target)
        elif target.is_dir():
            direct = target / "SKILL.md"
            if direct.is_file():
                results.append(direct)
            else:
                results.extend(target.rglob("SKILL.md"))
    return _sorted_manifests(results, snapshot_root)


def _plugin_boundary_manifests(boundary: Path, root: Path) -> list[Path]:
    results: list[Path] = []
    direct = boundary / "SKILL.md"
    if direct.is_file():
        results.append(direct)
    results.extend(_declared_skill_manifests(boundary, root))
    for conventional in (
        boundary / "skills",
        boundary / ".agents" / "skills",
        boundary / ".claude" / "skills",
    ):
        if conventional.is_dir():
            results.extend(conventional.rglob("SKILL.md"))
    return _sorted_manifests(results, root)


def _standard_manifests(root: Path) -> list[Path]:
    results: list[Path] = []
    direct = root / "SKILL.md"
    if direct.is_file():
        results.append(direct)
    for conventional in (root / "skills", root / ".agents" / "skills", root / ".claude" / "skills"):
        if conventional.is_dir():
            results.extend(conventional.rglob("SKILL.md"))
    plugins = root / "plugins"
    if plugins.is_dir():
        for plugin_dir in plugins.iterdir():
            if plugin_dir.is_dir() and (plugin_dir / "skills").is_dir():
                results.extend((plugin_dir / "skills").rglob("SKILL.md"))
    return _sorted_manifests(results, root)


def _plugin_root(resolved: ResolvedSource, root: Path) -> Path:
    raw = resolved.metadata.get("plugin_root") or resolved.metadata.get("plugin_path")
    if isinstance(raw, str) and raw.strip() and raw.strip() != ".":
        relative = _relative_request_path(raw)
        if relative is not None:
            candidate = ensure_within(root / Path(*relative.parts), root)
            if not candidate.is_dir():
                raise SkillToPluginError(
                    f"Resolved Claude plugin root does not exist: `{sanitize_text(raw)}`.",
                    code="invalid_manifest",
                )
            return candidate
    return root


def _add_candidates(
    target: dict[str, SkillCandidate],
    manifests: Iterable[Path],
    *,
    root: Path,
    snapshot_sha256: str,
    reason: str,
    priority: int,
    fallback_plugin: str | None,
) -> None:
    for manifest in _sorted_manifests(manifests, root):
        relative = manifest.relative_to(root).as_posix()
        previous = target.get(relative)
        if previous is not None and previous.priority <= priority:
            continue
        target[relative] = build_skill_candidate(
            root,
            manifest,
            snapshot_sha256=snapshot_sha256,
            selection_reason=reason,
            priority=priority,
            plugin=_nearest_plugin_name(manifest, root, fallback_plugin),
        )


def discover_skills(
    resolved: ResolvedSource,
    *,
    requested_path: str | None = None,
    plugin_scope: bool = False,
) -> tuple[SkillCandidate, ...]:
    """Discover candidates using structural, deterministic precedence.

    With a concrete path, the first applicable boundary wins: exact Skill,
    adjacent Skill, nearest ancestor Skill/Plugin, descendants, then repository
    conventions and finally the whole repository.  A repository-root request
    intentionally enumerates every SKILL.md, while preserving conventional
    placement as a more informative ``selection_reason``.

    Claude Plugin scope is different by design: every discovered Skill inside
    the resolved plugin boundary is returned so selection can include all valid
    Skills without prompting.
    """

    root = Path(resolved.snapshot_path).resolve()
    tree = validate_tree(root)
    if resolved.snapshot_sha256 and tree.tree_sha256 != resolved.snapshot_sha256:
        raise SkillToPluginError(
            "Resolved source snapshot hash does not match its materialized content.",
            code="resolution_integrity_failed",
            details={"expected": resolved.snapshot_sha256, "actual": tree.tree_sha256},
        )
    snapshot_sha = resolved.snapshot_sha256 or tree.tree_sha256
    fallback_plugin = resolved.original_plugin_name
    candidates: dict[str, SkillCandidate] = {}

    if plugin_scope:
        boundary = _plugin_root(resolved, root)
        manifests = _plugin_boundary_manifests(boundary, root)
        # Existing SKILL.md files in non-conventional but valid plugin layouts
        # remain eligible; commands/agents without SKILL.md are never converted.
        manifests = _sorted_manifests([*manifests, *boundary.rglob("SKILL.md")], root)
        _add_candidates(
            candidates,
            manifests,
            root=root,
            snapshot_sha256=snapshot_sha,
            reason="inside the explicitly selected Claude Plugin boundary",
            priority=PRIORITY_EXACT,
            fallback_plugin=fallback_plugin,
        )
        return tuple(candidates[key] for key in sorted(candidates, key=unicodedata_sort_key))

    requested = _relative_request_path(requested_path or resolved.requested_path)
    if requested is not None:
        target = ensure_within(root / Path(*requested.parts), root)

        # Tier 1: a literal SKILL.md URL/path or a Skill directory itself.
        exact: list[Path] = []
        if target.is_file() and target.name == "SKILL.md":
            exact.append(target)
        elif target.is_dir() and (target / "SKILL.md").is_file():
            exact.append(target / "SKILL.md")
        if exact:
            _add_candidates(
                candidates,
                exact,
                root=root,
                snapshot_sha256=snapshot_sha,
                reason="the input directly identifies this Skill boundary",
                priority=PRIORITY_EXACT,
                fallback_plugin=fallback_plugin,
            )
            return tuple(candidates.values())

        # Tier 2: a file next to SKILL.md.
        containing = target if target.is_dir() else target.parent
        direct = containing / "SKILL.md"
        if direct.is_file():
            _add_candidates(
                candidates,
                [direct],
                root=root,
                snapshot_sha256=snapshot_sha,
                reason="SKILL.md is directly beside the requested repository path",
                priority=PRIORITY_DIRECT,
                fallback_plugin=fallback_plugin,
            )
            return tuple(candidates.values())

        # Tier 3: nearest ancestor Skill boundary or Claude Plugin boundary.
        current = containing
        while True:
            ancestor_skill = current / "SKILL.md"
            if ancestor_skill.is_file():
                _add_candidates(
                    candidates,
                    [ancestor_skill],
                    root=root,
                    snapshot_sha256=snapshot_sha,
                    reason="nearest ancestor Skill boundary of the requested path",
                    priority=PRIORITY_ANCESTOR,
                    fallback_plugin=fallback_plugin,
                )
                return tuple(candidates.values())
            if (current / ".claude-plugin" / "plugin.json").is_file():
                manifests = _plugin_boundary_manifests(current, root)
                if manifests:
                    _add_candidates(
                        candidates,
                        manifests,
                        root=root,
                        snapshot_sha256=snapshot_sha,
                        reason="nearest ancestor Claude Plugin boundary of the requested path",
                        priority=PRIORITY_ANCESTOR,
                        fallback_plugin=fallback_plugin,
                    )
                    return tuple(candidates[key] for key in sorted(candidates, key=unicodedata_sort_key))
            if current == root:
                break
            current = current.parent

        # Tier 4: Skills below the requested path.
        if target.is_dir():
            descendants = _sorted_manifests(target.rglob("SKILL.md"), root)
            if descendants:
                _add_candidates(
                    candidates,
                    descendants,
                    root=root,
                    snapshot_sha256=snapshot_sha,
                    reason="inside the requested repository subtree",
                    priority=PRIORITY_DESCENDANT,
                    fallback_plugin=fallback_plugin,
                )
                return tuple(candidates[key] for key in sorted(candidates, key=unicodedata_sort_key))

    # Repository-root or peripheral-path fallback: enumerate the full repository
    # so valid unconventional Skill layouts are not silently omitted.
    standard = _standard_manifests(root)
    _add_candidates(
        candidates,
        standard,
        root=root,
        snapshot_sha256=snapshot_sha,
        reason="found in a standard Agent Skills repository placement",
        priority=PRIORITY_STANDARD,
        fallback_plugin=fallback_plugin,
    )
    all_manifests = _sorted_manifests(root.rglob("SKILL.md"), root)
    _add_candidates(
        candidates,
        all_manifests,
        root=root,
        snapshot_sha256=snapshot_sha,
        reason="found by the complete repository Skill scan",
        priority=PRIORITY_REPOSITORY,
        fallback_plugin=fallback_plugin,
    )
    return tuple(candidates[key] for key in sorted(candidates, key=unicodedata_sort_key))


discover_skill_candidates = discover_skills


__all__ = [
    "PRIORITY_ANCESTOR",
    "PRIORITY_DESCENDANT",
    "PRIORITY_DIRECT",
    "PRIORITY_EXACT",
    "PRIORITY_REPOSITORY",
    "PRIORITY_STANDARD",
    "discover_skill_candidates",
    "discover_skills",
]
