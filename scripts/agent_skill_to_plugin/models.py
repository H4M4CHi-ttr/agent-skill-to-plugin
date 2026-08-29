"""Common data models shared by parsers, resolvers, and packaging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .limits import SCHEMA_VERSION


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: str = "warning"
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Diagnostic":
        return cls(**value)


@dataclass(frozen=True)
class ParsedInput:
    kind: str
    raw_input: str
    normalized_input: str
    source: str | None = None
    marketplace_source: str | None = None
    marketplace_name: str | None = None
    plugin_name: str | None = None
    requested_skills: tuple[str, ...] = ()
    select_all: bool = False
    requested_ref: str | None = None
    requested_path: str | None = None
    plugin_scope: bool = False
    logical_sources: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requested_skills"] = list(self.requested_skills)
        data["logical_sources"] = list(self.logical_sources)
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParsedInput":
        data = dict(value)
        data["requested_skills"] = tuple(data.get("requested_skills", ()))
        data["logical_sources"] = tuple(data.get("logical_sources", ()))
        return cls(**data)


@dataclass(frozen=True)
class MarketplaceInfo:
    name: str | None = None
    source: str | None = None
    manifest_path: str | None = None
    resolution_method: str | None = None
    license: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "MarketplaceInfo | None":
        return None if value is None else cls(**value)


@dataclass(frozen=True)
class PluginSource:
    kind: str
    value: str
    ref: str | None = None
    sha: str | None = None
    subdir: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "PluginSource | None":
        return None if value is None else cls(**value)


@dataclass(frozen=True)
class ResolvedSource:
    kind: str
    normalized_source: str
    snapshot_path: str
    snapshot_sha256: str
    repository_url: str | None = None
    requested_ref: str | None = None
    resolved_commit: str | None = None
    requested_path: str | None = None
    marketplace: MarketplaceInfo | None = None
    plugin_source: PluginSource | None = None
    original_plugin_name: str | None = None
    resolution_method: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResolvedSource":
        data = dict(value)
        data["marketplace"] = MarketplaceInfo.from_dict(data.get("marketplace"))
        data["plugin_source"] = PluginSource.from_dict(data.get("plugin_source"))
        return cls(**data)


@dataclass(frozen=True)
class SkillCandidate:
    id: str
    name: str | None
    description: str | None
    path: str
    plugin: str | None
    selection_reason: str
    valid: bool
    priority: int
    manifest: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["diagnostics"] = [asdict(item) for item in self.diagnostics]
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillCandidate":
        data = dict(value)
        data["diagnostics"] = tuple(Diagnostic.from_dict(item) for item in data.get("diagnostics", ()))
        return cls(**data)


@dataclass(frozen=True)
class ResolutionState:
    resolution_id: str
    status: str
    created_at: str
    input: ParsedInput
    resolved_source: ResolvedSource
    candidates: tuple[SkillCandidate, ...]
    selection_policy: str
    resolution_file: str
    output_root: str
    schema_version: str = SCHEMA_VERSION
    diagnostics: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "resolution_id": self.resolution_id,
            "created_at": self.created_at,
            "input_kind": self.input.kind,
            "input": self.input.to_dict(),
            "resolved_source": self.resolved_source.to_dict(),
            "resolved_commit": self.resolved_source.resolved_commit,
            "candidates": [item.to_dict() for item in self.candidates],
            "selection_policy": self.selection_policy,
            "resolution_file": self.resolution_file,
            "output_root": self.output_root,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResolutionState":
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported resolution schema: {value.get('schema_version')!r}")
        return cls(
            schema_version=value["schema_version"],
            status=value["status"],
            resolution_id=value["resolution_id"],
            created_at=value["created_at"],
            input=ParsedInput.from_dict(value["input"]),
            resolved_source=ResolvedSource.from_dict(value["resolved_source"]),
            candidates=tuple(SkillCandidate.from_dict(item) for item in value.get("candidates", ())),
            selection_policy=value["selection_policy"],
            resolution_file=value["resolution_file"],
            output_root=value["output_root"],
            diagnostics=tuple(Diagnostic.from_dict(item) for item in value.get("diagnostics", ())),
        )


@dataclass(frozen=True)
class SelectedSkill:
    candidate_id: str
    name: str
    description: str
    path: str
    tree_sha256: str
    file_hashes: dict[str, str]


@dataclass(frozen=True)
class Provenance:
    original_input: str
    input_kind: str
    normalized_source: str
    repository_url: str | None
    requested_ref: str | None
    resolved_commit: str | None
    marketplace_name: str | None
    original_plugin_name: str | None
    acquired_at: str
    tool_version: str
    source_snapshot_sha256: str
    license_findings: tuple[dict[str, Any], ...] = ()
    selection_reason: str | None = None


@dataclass(frozen=True)
class ConversionResult:
    plugin_name: str
    plugin_dir: str
    zip_path: str
    zip_sha256: str
    marketplace_root: str
    marketplace_file: str
    marketplace_add_command: str
    report_json: str
    report_markdown: str
    skills: tuple[SelectedSkill, ...]
    provenance: Provenance
    plugin_tree_sha256: str
    warnings: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = "ok"
        data["skills"] = [asdict(item) for item in self.skills]
        data["provenance"] = asdict(self.provenance)
        return data
