"""Application service for resolve, run, and offline convert operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
from typing import Any, Sequence
import uuid

from .compatibility import compatibility_diagnostics
from .discovery import discover_skills
from .errors import SkillToPluginError
from .input_parser import parse_input
from .limits import DEFAULT_PLUGIN_VERSION, DEFAULT_TIMEOUT_SECONDS
from .models import ConversionResult, Diagnostic, ParsedInput, ResolutionState
from .packaging import package_selected
from .provenance import build_provenance, detect_licenses
from .resolver_registry import ResolverRegistry
from .selection import (
    SelectionDecision,
    resume_selection,
    select_candidates,
    selected_skills_from_candidates,
    validate_selected_references,
)
from .utils import atomic_write_json, ensure_within, utc_now


@dataclass(frozen=True)
class ResolutionOutcome:
    state: ResolutionState
    decision: SelectionDecision

    def to_dict(self) -> dict[str, Any]:
        data = self.state.to_dict()
        data["status"] = "needs_selection" if self.decision.needs_selection else "resolved"
        data["automatic_selection"] = list(self.decision.selected_ids)
        diagnostics = {
            (item.code, item.path, item.message): item
            for item in (*self.state.diagnostics, *self.decision.diagnostics)
        }
        data["diagnostics"] = [asdict(item) for item in diagnostics.values()]
        return data


def _source_diagnostics(resolved_source: Any) -> tuple[Diagnostic, ...]:
    raw = resolved_source.metadata.get("skipped_symbolic_links", [])
    if not isinstance(raw, list) or not raw:
        return ()
    paths = [str(item) for item in raw if isinstance(item, str)]
    if not paths:
        return ()
    return (
        Diagnostic(
            code="git_symbolic_links_skipped",
            message="Git symbolic links were excluded from the fixed snapshot and were not followed or packaged.",
            severity="warning",
            details={"paths": paths, "count": len(paths)},
        ),
    )


def _reject_selected_skipped_links(state: ResolutionState, selected: Sequence[Any]) -> None:
    raw = state.resolved_source.metadata.get("skipped_symbolic_links", [])
    paths = [str(item).replace("\\", "/").strip("/") for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
    for candidate in selected:
        boundary = str(candidate.path).replace("\\", "/").strip("/")
        inside = paths if boundary in {"", "."} else [
            path for path in paths if path == boundary or path.startswith(boundary + "/")
        ]
        if inside:
            raise SkillToPluginError(
                "A selected Skill contains symbolic links that were excluded from its Git snapshot; refusing to emit incomplete Skill content.",
                code="security_rejected",
                details={"candidate_id": candidate.id, "paths": inside},
            )


def resolve_request(
    raw_input: str | ParsedInput,
    *,
    output_root: Path,
    source_base: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    registry: ResolverRegistry | None = None,
) -> ResolutionOutcome:
    parsed = raw_input if isinstance(raw_input, ParsedInput) else parse_input(raw_input)
    output_root = output_root.resolve()
    source_base = source_base.resolve()
    resolutions = output_root / "resolutions"
    resolutions.mkdir(parents=True, exist_ok=True)
    resolution_id = str(uuid.uuid4())
    snapshot_dir = resolutions / f"{resolution_id}.snapshot"
    resolution_file = resolutions / f"{resolution_id}.json"
    resolver_registry = registry or ResolverRegistry()
    try:
        resolved = resolver_registry.resolve(
            parsed,
            snapshot_dir,
            source_base=source_base,
            timeout_seconds=timeout_seconds,
        )
        candidates = discover_skills(
            resolved,
            requested_path=parsed.requested_path or resolved.requested_path,
            plugin_scope=parsed.plugin_scope,
        )
        if parsed.plugin_scope and not candidates:
            raise SkillToPluginError(
                "The Claude Plugin contains no existing SKILL.md, so it cannot be extracted automatically as a skills-only plugin. Commands and agents require a separate explicit semantic conversion.",
                code="no_skill_candidates",
                details={"commands_or_agents_converted": False},
            )
        selection_policy = (
            "claude_plugin_all" if parsed.plugin_scope
            else "all_valid" if parsed.select_all or parsed.kind == "multi_source"
            else "structural"
        )
        decision = select_candidates(
            candidates,
            requested_skills=parsed.requested_skills,
            select_all=parsed.select_all or parsed.kind == "multi_source",
            plugin_scope=parsed.plugin_scope,
            selection_policy=selection_policy,
        )
        source_diagnostics = _source_diagnostics(resolved)
        state = ResolutionState(
            resolution_id=resolution_id,
            status="needs_selection" if decision.needs_selection else "resolved",
            created_at=utc_now(),
            input=parsed,
            resolved_source=resolved,
            candidates=tuple(candidates),
            selection_policy=selection_policy,
            resolution_file=str(resolution_file),
            output_root=str(output_root),
            diagnostics=source_diagnostics + tuple(decision.diagnostics),
        )
        atomic_write_json(resolution_file, state.to_dict())
        return ResolutionOutcome(state=state, decision=decision)
    except Exception:
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        resolution_file.unlink(missing_ok=True)
        raise


def convert_resolution(
    resolution_file: Path,
    *,
    selected: str | Sequence[str] | None = None,
    requested_plugin_name: str | None = None,
    display_name: str | None = None,
    author_name: str = "Local conversion",
    version: str = DEFAULT_PLUGIN_VERSION,
    force: bool = False,
) -> ConversionResult | ResolutionOutcome:
    state, decision = resume_selection(resolution_file, selected=selected)
    if decision.needs_selection:
        return ResolutionOutcome(state=state, decision=decision)
    _reject_selected_skipped_links(state, decision.selected)
    snapshot = Path(state.resolved_source.snapshot_path)
    if not snapshot.is_absolute():
        snapshot = Path(state.output_root) / snapshot
    snapshot = ensure_within(snapshot, Path(state.output_root) / "resolutions", code="resolution_integrity_failed")
    decision = validate_selected_references(snapshot, decision)
    selected_models = selected_skills_from_candidates(snapshot, decision)

    plugin_root: Path | None = None
    raw_plugin_root = state.resolved_source.metadata.get("plugin_root") or state.resolved_source.metadata.get("plugin_path")
    if isinstance(raw_plugin_root, str) and raw_plugin_root.strip():
        plugin_root = ensure_within(snapshot / Path(raw_plugin_root), snapshot)
    compatibility = compatibility_diagnostics(
        [snapshot / Path(candidate.path) for candidate in decision.selected],
        plugin_root=plugin_root,
    )
    license_findings = detect_licenses(
        snapshot,
        candidates=decision.selected,
        resolved_source=state.resolved_source,
    )
    provenance = build_provenance(
        state.input,
        state.resolved_source,
        candidates=decision.selected,
        selected_skills=selected_models,
        acquired_at=state.created_at,
        license_findings=license_findings,
    )
    diagnostics = tuple(state.diagnostics) + tuple(decision.diagnostics) + tuple(compatibility)
    excluded_components = state.resolved_source.metadata.get("excluded_marketplace_components", [])
    if isinstance(excluded_components, list):
        diagnostics += tuple(
            Diagnostic(
                code="excluded_claude_marketplace_component",
                message=f"Claude Marketplace component `{component}` was detected but was not semantically converted into the skills-only plugin.",
                severity="warning",
                details={"component": component, "converted": False},
            )
            for component in excluded_components
            if isinstance(component, str)
        )
    if (
        state.resolved_source.kind == "npm_package"
        or (state.resolved_source.plugin_source and state.resolved_source.plugin_source.kind == "npm")
    ) and not state.resolved_source.metadata.get("integrity"):
        diagnostics += (
            Diagnostic(
                code="npm_integrity_missing",
                message="The npm registry record supplied no supported integrity or shasum value; the downloaded bytes are still recorded by SHA-256.",
                severity="warning",
            ),
        )
    if not any(item.get("status") != "unknown" for item in license_findings):
        diagnostics += (
            Diagnostic(
                code="license_unknown",
                message="No license evidence was detected; redistribution rights were not verified.",
            ),
        )
    diagnostics += (
        Diagnostic(
            code="untrusted_import",
            message="Imported instructions and scripts remain untrusted and were not executed during conversion.",
            severity="warning",
        ),
    )
    unique_diagnostics: dict[tuple[str, str | None, str], Diagnostic] = {}
    for diagnostic in diagnostics:
        unique_diagnostics[(diagnostic.code, diagnostic.path, diagnostic.message)] = diagnostic

    return package_selected(
        state,
        decision.selected,
        output_root=Path(state.output_root),
        provenance=provenance,
        diagnostics=tuple(unique_diagnostics.values()),
        license_findings=license_findings,
        external_references=decision.external_references,
        requested_plugin_name=requested_plugin_name,
        display_name=display_name,
        author_name=author_name,
        version=version,
        force=force,
    )


def run_request(
    raw_input: str | ParsedInput,
    *,
    output_root: Path,
    source_base: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    requested_plugin_name: str | None = None,
    display_name: str | None = None,
    author_name: str = "Local conversion",
    version: str = DEFAULT_PLUGIN_VERSION,
    force: bool = False,
    registry: ResolverRegistry | None = None,
) -> ConversionResult | ResolutionOutcome:
    outcome = resolve_request(
        raw_input,
        output_root=output_root,
        source_base=source_base,
        timeout_seconds=timeout_seconds,
        registry=registry,
    )
    if outcome.decision.needs_selection:
        return outcome
    return convert_resolution(
        Path(outcome.state.resolution_file),
        requested_plugin_name=requested_plugin_name,
        display_name=display_name,
        author_name=author_name,
        version=version,
        force=force,
    )
