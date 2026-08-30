from __future__ import annotations

import atexit
import json
from pathlib import Path
import shutil
import tempfile

from agent_skill_to_plugin.discovery import discover_skills
from agent_skill_to_plugin.models import ParsedInput, ResolutionState, ResolvedSource
from agent_skill_to_plugin.utils import hash_tree


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TEMPLATES = PROJECT_ROOT / "fixtures"


def _materialize_fixtures() -> Path:
    """Restore non-discoverable fixture manifests in an isolated test tree."""
    temporary_root = Path(tempfile.mkdtemp(prefix="agent-skill-to-plugin-fixtures-"))
    atexit.register(shutil.rmtree, temporary_root, ignore_errors=True)
    destination = temporary_root / "fixtures"
    shutil.copytree(FIXTURE_TEMPLATES, destination)
    for stored_manifest in destination.rglob("SKILL.fixture.md"):
        manifest = stored_manifest.with_name("SKILL.md")
        if manifest.exists():
            raise RuntimeError(f"fixture contains both stored and live manifests: {manifest}")
        stored_manifest.replace(manifest)
    return destination


FIXTURES = _materialize_fixtures()


def copy_fixture(name: str, destination: Path) -> Path:
    source = FIXTURES / name
    shutil.copytree(source, destination)
    return destination


def resolved_source(
    snapshot: Path,
    *,
    requested_path: str | None = None,
    plugin_name: str | None = None,
    metadata: dict | None = None,
) -> ResolvedSource:
    return ResolvedSource(
        kind="local_fixture",
        normalized_source=f"fixture:{snapshot.name}",
        snapshot_path=str(snapshot.resolve()),
        snapshot_sha256=hash_tree(snapshot),
        requested_path=requested_path,
        original_plugin_name=plugin_name,
        metadata=metadata or {},
    )


def persisted_state(
    output_root: Path,
    fixture_name: str,
    *,
    plugin_scope: bool = False,
    requested_path: str | None = None,
    selection_policy: str = "structural",
) -> tuple[ResolutionState, Path]:
    resolutions = output_root / "resolutions"
    resolutions.mkdir(parents=True)
    snapshot = copy_fixture(fixture_name, resolutions / "fixture.snapshot")
    resolved = resolved_source(
        snapshot,
        requested_path=requested_path,
        plugin_name="fixture-claude-plugin" if plugin_scope else None,
        metadata={"plugin_root": "."} if plugin_scope else {},
    )
    parsed = ParsedInput(
        kind="claude_plugin" if plugin_scope else "local",
        raw_input=f"fixture:{fixture_name}",
        normalized_input=f"fixture:{fixture_name}",
        source=f"fixture:{fixture_name}",
        requested_path=requested_path,
        plugin_scope=plugin_scope,
    )
    candidates = discover_skills(resolved, requested_path=requested_path, plugin_scope=plugin_scope)
    resolution_file = resolutions / "fixture.json"
    state = ResolutionState(
        resolution_id="fixture-resolution",
        status="needs_selection" if len([item for item in candidates if item.valid]) > 1 else "resolved",
        created_at="2026-08-29T00:00:00+00:00",
        input=parsed,
        resolved_source=resolved,
        candidates=candidates,
        selection_policy=selection_policy,
        resolution_file=str(resolution_file.resolve()),
        output_root=str(output_root.resolve()),
    )
    resolution_file.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return state, resolution_file
