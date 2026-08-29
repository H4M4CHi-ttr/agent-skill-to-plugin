"""Deterministic candidate selection and offline resolution resumption."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .errors import SkillToPluginError
from .models import Diagnostic, ResolutionState, SelectedSkill, SkillCandidate
from .utils import sanitize_text
from .validation import (
    ExternalReference,
    TreeValidation,
    detect_external_references,
    validate_resolution_state,
    validate_tree,
)


MAX_RESOLUTION_JSON_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SelectionDecision:
    status: str
    selected: tuple[SkillCandidate, ...]
    available: tuple[SkillCandidate, ...]
    diagnostics: tuple[Diagnostic, ...] = ()
    external_references: dict[str, tuple[ExternalReference, ...]] = field(default_factory=dict)

    @property
    def needs_selection(self) -> bool:
        return self.status == "needs_selection"

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(candidate.id for candidate in self.selected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected": [candidate.to_dict() for candidate in self.selected],
            "selected_ids": list(self.selected_ids),
            "candidates": [candidate.to_dict() for candidate in self.available],
            "diagnostics": [
                {
                    "code": item.code,
                    "message": item.message,
                    "severity": item.severity,
                    "path": item.path,
                    "details": item.details,
                }
                for item in self.diagnostics
            ],
            "external_references": {
                candidate_id: [
                    {
                        "referenced_from": reference.referenced_from,
                        "raw_reference": reference.raw_reference,
                        "source_path": reference.source_path,
                        "destination_path": reference.destination_path,
                        "is_directory": reference.is_directory,
                        "sha256": reference.sha256,
                    }
                    for reference in references
                ]
                for candidate_id, references in self.external_references.items()
            },
        }


def _invalid_diagnostics(candidates: Sequence[SkillCandidate]) -> tuple[Diagnostic, ...]:
    return tuple(
        diagnostic
        for candidate in candidates
        if not candidate.valid
        for diagnostic in candidate.diagnostics
    )


def _ordered(candidates: Iterable[SkillCandidate]) -> tuple[SkillCandidate, ...]:
    return tuple(sorted(candidates, key=lambda item: (item.priority, item.path.casefold(), item.path, item.id)))


def _ensure_unique_names(candidates: Sequence[SkillCandidate]) -> None:
    by_name: dict[str, list[SkillCandidate]] = {}
    for candidate in candidates:
        assert candidate.name is not None
        by_name.setdefault(candidate.name.casefold(), []).append(candidate)
    conflicts = {
        items[0].name or key: [item.path for item in items]
        for key, items in by_name.items()
        if len(items) > 1
    }
    if conflicts:
        raise SkillToPluginError(
            "Selected Skills contain duplicate front-matter names and cannot share one plugin.",
            code="package_validation_failed",
            details={"duplicate_skill_names": conflicts},
        )


def _matches(candidate: SkillCandidate, selector: str) -> bool:
    normalized = selector.strip().replace("\\", "/")
    return (
        candidate.id == normalized
        or (candidate.name is not None and candidate.name == normalized)
        or candidate.path == normalized.rstrip("/")
        or f"{candidate.path.rstrip('/')}/SKILL.md" == normalized.rstrip("/")
    )


def select_candidates(
    candidates: Sequence[SkillCandidate],
    *,
    selected: str | Sequence[str] | None = None,
    requested_skills: Sequence[str] = (),
    select_all: bool = False,
    plugin_scope: bool = False,
    selection_policy: str | None = None,
) -> SelectionDecision:
    """Apply only explicit and structural selection rules.

    Candidate descriptions never participate in selection.  A generic source
    with several valid candidates returns ``needs_selection``.  An explicitly
    installed Claude Plugin selects all valid Skills in its plugin boundary.
    """

    available = _ordered(candidates)
    valid = tuple(candidate for candidate in available if candidate.valid)
    invalid_diagnostics = _invalid_diagnostics(available)
    if not valid:
        if available:
            raise SkillToPluginError(
                "SKILL.md files were found, but none has a valid Agent Skill manifest.",
                code="invalid_manifest",
                details={
                    "candidates": [candidate.to_dict() for candidate in available],
                },
            )
        raise SkillToPluginError("No SKILL.md candidates were found.", code="no_skill_candidates")

    if isinstance(selected, str):
        selectors = (selected,)
    elif selected is None:
        selectors = ()
    else:
        selectors = tuple(selected)
    selectors = tuple(value.strip() for value in selectors if value and value.strip())

    if any(value.casefold() == "all" for value in selectors):
        if len(selectors) != 1:
            raise SkillToPluginError("`all` cannot be combined with another candidate selector.", code="invalid_selection")
        select_all = True

    if selectors and not select_all:
        chosen: list[SkillCandidate] = []
        ambiguous: dict[str, list[dict[str, str | None]]] = {}
        missing: list[str] = []
        for selector in selectors:
            matches = [candidate for candidate in available if _matches(candidate, selector)]
            if not matches:
                missing.append(selector)
                continue
            if len(matches) > 1:
                ambiguous[selector] = [
                    {"id": item.id, "name": item.name, "path": item.path}
                    for item in matches
                ]
                continue
            match = matches[0]
            if not match.valid:
                raise SkillToPluginError(
                    "The selected candidate has an invalid SKILL.md manifest.",
                    code="invalid_selection",
                    details={"candidate": match.to_dict()},
                )
            if match.id not in {item.id for item in chosen}:
                chosen.append(match)
        if missing:
            raise SkillToPluginError(
                "One or more candidate selectors do not exist in this fixed resolution.",
                code="invalid_selection",
                details={"unknown_selectors": missing},
            )
        if ambiguous:
            return SelectionDecision(
                status="needs_selection",
                selected=(),
                available=available,
                diagnostics=invalid_diagnostics,
            )
        _ensure_unique_names(chosen)
        return SelectionDecision(
            status="selected",
            selected=_ordered(chosen),
            available=available,
            diagnostics=invalid_diagnostics,
        )

    explicit_requests = tuple(item.strip() for item in requested_skills if item and item.strip())
    if explicit_requests and not select_all:
        return select_candidates(
            available,
            selected=explicit_requests,
            select_all=False,
            plugin_scope=plugin_scope,
            selection_policy=selection_policy,
        )

    policy = (selection_policy or "").casefold().replace("-", "_")
    all_policy = policy in {"all", "all_valid", "claude_plugin", "claude_plugin_all", "plugin_all"}
    if select_all or plugin_scope or all_policy:
        _ensure_unique_names(valid)
        return SelectionDecision(
            status="selected",
            selected=_ordered(valid),
            available=available,
            diagnostics=invalid_diagnostics,
        )

    if len(valid) == 1:
        return SelectionDecision(
            status="selected",
            selected=valid,
            available=available,
            diagnostics=invalid_diagnostics,
        )

    return SelectionDecision(
        status="needs_selection",
        selected=(),
        available=available,
        diagnostics=invalid_diagnostics,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_resolution_state(resolution_file: Path) -> tuple[ResolutionState, TreeValidation]:
    """Load and fully revalidate a resolution without invoking any resolver."""

    supplied_path = Path(resolution_file).absolute()
    if supplied_path.is_symlink() or not supplied_path.is_file():
        raise SkillToPluginError("Resolution file is missing or is a symbolic link.", code="resolution_integrity_failed")
    path = supplied_path.resolve()
    try:
        size = path.stat().st_size
        if size > MAX_RESOLUTION_JSON_BYTES:
            raise SkillToPluginError("Resolution file exceeds the safety size limit.", code="resolution_integrity_failed")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except SkillToPluginError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SkillToPluginError(
            f"Could not parse resolution state: {sanitize_text(str(exc))}",
            code="resolution_integrity_failed",
        ) from exc
    if not isinstance(value, dict):
        raise SkillToPluginError("Resolution state must be a JSON object.", code="resolution_integrity_failed")
    try:
        state = ResolutionState.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise SkillToPluginError(
            f"Resolution state schema is invalid: {sanitize_text(str(exc))}",
            code="resolution_integrity_failed",
        ) from exc
    tree = validate_resolution_state(state, path)
    return state, tree


def validate_selected_references(
    snapshot_root: Path,
    decision: SelectionDecision,
) -> SelectionDecision:
    if decision.needs_selection:
        return decision
    plans: dict[str, tuple[ExternalReference, ...]] = {}
    destinations: dict[str, tuple[str, str]] = {}
    reference_diagnostics: list[Diagnostic] = []
    for candidate in decision.selected:
        assert candidate.name is not None
        skill_dir = snapshot_root if candidate.path == "." else snapshot_root / Path(*PurePosixPath(candidate.path).parts)
        references = detect_external_references(
            skill_dir,
            snapshot_root,
            skill_name=candidate.name,
            diagnostics=reference_diagnostics,
        )
        for reference in references:
            key = reference.destination_path.casefold()
            previous = destinations.get(key)
            identity = (reference.source_path, reference.sha256 or "")
            if previous is not None and previous != identity:
                raise SkillToPluginError(
                    "External references from selected Skills collide at different source content.",
                    code="package_validation_failed",
                    details={"destination": reference.destination_path},
                )
            destinations[key] = identity
        plans[candidate.id] = references
    return replace(
        decision,
        external_references=plans,
        diagnostics=decision.diagnostics + tuple(reference_diagnostics),
    )


def selected_skills_from_candidates(
    snapshot_root: Path,
    decision: SelectionDecision,
) -> tuple[SelectedSkill, ...]:
    """Revalidate selected directories and build common SelectedSkill models."""

    if decision.needs_selection:
        raise SkillToPluginError("Candidate selection is still required.", code="invalid_selection")
    root = snapshot_root.resolve()
    selected: list[SelectedSkill] = []
    for candidate in decision.selected:
        if not candidate.valid or not candidate.name or not candidate.description:
            raise SkillToPluginError("An invalid candidate cannot be converted.", code="invalid_selection")
        skill_dir = root if candidate.path == "." else root / Path(*PurePosixPath(candidate.path).parts)
        tree = validate_tree(skill_dir)
        selected.append(
            SelectedSkill(
                candidate_id=candidate.id,
                name=candidate.name,
                description=candidate.description,
                path=candidate.path,
                tree_sha256=tree.tree_sha256,
                file_hashes=tree.file_hashes,
            )
        )
    return tuple(selected)


def resume_selection(
    resolution_file: Path,
    selected: str | Sequence[str] | None = None,
) -> tuple[ResolutionState, SelectionDecision]:
    """Resume selection solely from the pinned and revalidated snapshot."""

    state, _tree = load_resolution_state(resolution_file)
    decision = select_candidates(
        state.candidates,
        selected=selected,
        requested_skills=state.input.requested_skills if selected is None else (),
        select_all=state.input.select_all if selected is None else False,
        plugin_scope=state.input.plugin_scope,
        selection_policy=state.selection_policy,
    )
    snapshot = Path(state.resolved_source.snapshot_path)
    if not snapshot.is_absolute():
        snapshot = Path(state.output_root) / snapshot
    decision = validate_selected_references(snapshot.resolve(), decision)
    return state, decision


resolve_selection = select_candidates


__all__ = [
    "SelectionDecision",
    "load_resolution_state",
    "resolve_selection",
    "resume_selection",
    "select_candidates",
    "selected_skills_from_candidates",
    "validate_selected_references",
]
