"""Provenance and license-evidence collection for conversion reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .limits import TOOL_VERSION
from .models import ParsedInput, Provenance, ResolvedSource, SelectedSkill, SkillCandidate
from .utils import sanitize_text, sha256_file, utc_now


@dataclass(frozen=True)
class LicenseFinding:
    source: str
    value: str | None = None
    path: str | None = None
    evidence_path: str | None = None
    sha256: str | None = None
    included_in_plugin: bool = False
    status: str = "detected"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _license_filename(name: str) -> bool:
    folded = name.casefold()
    return (
        folded in {"license", "copying", "notice"}
        or folded.startswith("license.")
        or folded.startswith("copying.")
        or folded.startswith("notice.")
    )


def _manifest_license(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    license_value = value.get("license")
    if isinstance(license_value, str) and license_value.strip():
        return license_value.strip()
    if isinstance(license_value, dict):
        for key in ("spdx", "type", "name"):
            item = license_value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def detect_licenses(
    snapshot_root: Path,
    *,
    candidates: Sequence[SkillCandidate] = (),
    resolved_source: ResolvedSource | None = None,
    included_paths: Iterable[str] = (),
) -> tuple[dict[str, Any], ...]:
    """Collect license evidence without inferring redistribution rights."""

    root = snapshot_root.resolve()
    included = {PurePosixPath(value).as_posix().casefold() for value in included_paths}
    findings: list[LicenseFinding] = []

    license_roots: set[Path] = {root}
    for candidate in candidates:
        current = root if candidate.path == "." else root / Path(*PurePosixPath(candidate.path).parts)
        while True:
            license_roots.add(current)
            if current == root:
                break
            current = current.parent

    for evidence_root in sorted(license_roots, key=lambda item: item.relative_to(root).as_posix()):
        if not evidence_root.is_dir():
            continue
        for path in sorted(evidence_root.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
            if path.is_file() and not path.is_symlink() and _license_filename(path.name):
                relative = path.relative_to(root).as_posix()
                findings.append(
                    LicenseFinding(
                        source="source_boundary_file" if evidence_root != root else "repository_root_file",
                        path=relative,
                        sha256=sha256_file(path),
                        included_in_plugin=relative.casefold() in included,
                    )
                )

    for candidate in candidates:
        value = candidate.manifest.get("license") if isinstance(candidate.manifest, dict) else None
        if isinstance(value, str) and value.strip():
            findings.append(
                LicenseFinding(
                    source="skill_frontmatter",
                    value=value.strip(),
                    # ``path`` is reserved for actual LICENSE/NOTICE files that
                    # packaging may copy.  Manifest evidence must not cause the
                    # entire SKILL.md to be mistaken for a license file.
                    evidence_path=(PurePosixPath(candidate.path) / "SKILL.md").as_posix(),
                )
            )

    # Plugin manifest evidence is declarative only.  It never overrides or
    # silently resolves a conflicting repository/Skill declaration.
    plugin_manifests: set[Path] = set()
    direct_plugin_manifest = root / ".claude-plugin" / "plugin.json"
    if direct_plugin_manifest.is_file():
        plugin_manifests.add(direct_plugin_manifest)
    for candidate in candidates:
        current = root if candidate.path == "." else root / Path(*PurePosixPath(candidate.path).parts)
        while current != root:
            plugin_manifest = current / ".claude-plugin" / "plugin.json"
            if plugin_manifest.is_file():
                plugin_manifests.add(plugin_manifest)
                break
            current = current.parent
    for manifest_path in sorted(plugin_manifests, key=lambda item: item.relative_to(root).as_posix()):
        value = _manifest_license(manifest_path)
        if value:
            findings.append(
                LicenseFinding(
                    source="claude_plugin_manifest",
                    value=value,
                    evidence_path=manifest_path.relative_to(root).as_posix(),
                    sha256=sha256_file(manifest_path),
                )
            )

    if resolved_source is not None:
        if resolved_source.marketplace and resolved_source.marketplace.license:
            findings.append(
                LicenseFinding(
                    source="claude_marketplace_manifest",
                    value=resolved_source.marketplace.license,
                    evidence_path=resolved_source.marketplace.manifest_path,
                )
            )
        metadata = resolved_source.metadata
        for key, source in (
            ("github_license", "github_api"),
            ("license_spdx", "github_api"),
            ("license", "resolver_metadata"),
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                findings.append(LicenseFinding(source=source, value=value.strip()))

    # De-duplicate identical evidence while retaining conflicts as separate
    # entries for the report author/user to resolve.
    unique: dict[tuple[Any, ...], LicenseFinding] = {}
    for finding in findings:
        key = (
            finding.source,
            finding.value,
            finding.path,
            finding.evidence_path,
            finding.sha256,
            finding.included_in_plugin,
            finding.status,
        )
        unique[key] = finding

    if not unique:
        return (
            LicenseFinding(
                source="aggregate",
                status="unknown",
                value="No license evidence was detected; redistribution rights were not verified.",
            ).to_dict(),
        )
    return tuple(item.to_dict() for item in unique.values())


def build_provenance(
    parsed: ParsedInput,
    resolved: ResolvedSource,
    *,
    candidates: Sequence[SkillCandidate] = (),
    selected_skills: Sequence[SelectedSkill] = (),
    acquired_at: str | None = None,
    tool_version: str = TOOL_VERSION,
    license_findings: Sequence[dict[str, Any]] | None = None,
    included_license_paths: Iterable[str] = (),
) -> Provenance:
    """Build the common provenance model from normalized source information."""

    snapshot = Path(resolved.snapshot_path)
    if license_findings is None:
        license_findings = detect_licenses(
            snapshot,
            candidates=candidates,
            resolved_source=resolved,
            included_paths=included_license_paths,
        )

    reasons = []
    for candidate in candidates:
        if candidate.selection_reason and candidate.selection_reason not in reasons:
            reasons.append(candidate.selection_reason)
    if not reasons and selected_skills:
        reasons.append("selected from the fixed resolution by candidate ID")

    marketplace_name = resolved.marketplace.name if resolved.marketplace else parsed.marketplace_name
    normalized_source = resolved.normalized_source or parsed.normalized_input
    recorded_acquired_at = acquired_at
    if recorded_acquired_at is None:
        metadata_time = resolved.metadata.get("acquired_at") or resolved.metadata.get("fetched_at")
        recorded_acquired_at = metadata_time if isinstance(metadata_time, str) and metadata_time else utc_now()

    return Provenance(
        original_input=sanitize_text(parsed.raw_input),
        input_kind=parsed.kind,
        normalized_source=sanitize_text(normalized_source),
        repository_url=sanitize_text(resolved.repository_url) if resolved.repository_url else None,
        requested_ref=sanitize_text(resolved.requested_ref or parsed.requested_ref) if (resolved.requested_ref or parsed.requested_ref) else None,
        resolved_commit=sanitize_text(resolved.resolved_commit) if resolved.resolved_commit else None,
        marketplace_name=sanitize_text(marketplace_name) if marketplace_name else None,
        original_plugin_name=sanitize_text(resolved.original_plugin_name or parsed.plugin_name) if (resolved.original_plugin_name or parsed.plugin_name) else None,
        acquired_at=recorded_acquired_at,
        tool_version=tool_version,
        source_snapshot_sha256=resolved.snapshot_sha256,
        license_findings=tuple(dict(item) for item in license_findings),
        selection_reason="; ".join(reasons) if reasons else None,
    )


def selected_skill_provenance(selected_skills: Sequence[SelectedSkill]) -> tuple[dict[str, Any], ...]:
    """Return per-Skill source hashes for JSON/Markdown report rendering."""

    return tuple(
        {
            "candidate_id": skill.candidate_id,
            "name": skill.name,
            "path": skill.path,
            "tree_sha256": skill.tree_sha256,
            "file_hashes": dict(sorted(skill.file_hashes.items())),
        }
        for skill in selected_skills
    )


collect_provenance = build_provenance
collect_license_findings = detect_licenses


__all__ = [
    "LicenseFinding",
    "build_provenance",
    "collect_license_findings",
    "collect_provenance",
    "detect_licenses",
    "selected_skill_provenance",
]
