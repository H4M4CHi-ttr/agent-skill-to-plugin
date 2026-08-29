from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from agent_skill_to_plugin.discovery import discover_skills
from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.models import ParsedInput, ResolutionState
from agent_skill_to_plugin.packaging import package_selected
from agent_skill_to_plugin.provenance import build_provenance, detect_licenses
from agent_skill_to_plugin.selection import select_candidates, validate_selected_references
from agent_skill_to_plugin.validation import detect_external_references

from tests.helpers import copy_fixture, resolved_source


class ExternalReferenceTests(unittest.TestCase):
    def test_safe_external_reference_produces_a_copy_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture("external-reference-skill", Path(temporary) / "snapshot")
            references = detect_external_references(root / "skills" / "external", root)
            self.assertEqual(len(references), 1)
            self.assertEqual(references[0].source_path, "shared/guide.md")
            self.assertEqual(references[0].destination_path, "shared/guide.md")
            self.assertFalse(references[0].is_directory)

    def test_missing_relative_reference_is_reported_without_silent_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture("external-reference-skill", Path(temporary) / "snapshot")
            (root / "shared" / "guide.md").unlink()
            diagnostics = []
            references = detect_external_references(
                root / "skills" / "external",
                root,
                diagnostics=diagnostics,
            )
            self.assertEqual((), references)
            self.assertEqual("external_reference_missing", diagnostics[0].code)
            self.assertEqual("../../shared/guide.md", diagnostics[0].details["reference"])

    def test_reference_outside_snapshot_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = copy_fixture("external-reference-skill", base / "snapshot")
            (base / "outside.md").write_text("outside", encoding="utf-8")
            (root / "skills" / "external" / "SKILL.md").write_text(
                "---\nname: external\ndescription: Escape fixture.\n---\n"
                "Read [outside](../../../outside.md).\n",
                encoding="utf-8",
            )
            with self.assertRaises(SkillToPluginError) as raised:
                detect_external_references(root / "skills" / "external", root)
            self.assertEqual(raised.exception.code, "package_validation_failed")


def _package_fixture(base: Path, fixture_name: str = "single-skill-repo"):
    output_root = base / "market"
    resolutions = output_root / "resolutions"
    resolutions.mkdir(parents=True)
    snapshot = copy_fixture(fixture_name, resolutions / "fixed.snapshot")
    resolved = resolved_source(snapshot)
    candidates = discover_skills(resolved)
    decision = select_candidates(candidates)
    decision = validate_selected_references(snapshot, decision)
    parsed = ParsedInput(
        kind="local",
        raw_input=f"fixture:{fixture_name}",
        normalized_input=f"fixture:{fixture_name}",
        source=f"fixture:{fixture_name}",
    )
    state = ResolutionState(
        resolution_id="fixed-resolution",
        status="resolved",
        created_at="2026-08-29T00:00:00+00:00",
        input=parsed,
        resolved_source=resolved,
        candidates=candidates,
        selection_policy="structural",
        resolution_file=str((resolutions / "fixed.json").resolve()),
        output_root=str(output_root.resolve()),
    )
    licenses = detect_licenses(snapshot, candidates=decision.selected, resolved_source=resolved)
    provenance = build_provenance(
        parsed,
        resolved,
        candidates=decision.selected,
        acquired_at=state.created_at,
        license_findings=licenses,
    )
    result = package_selected(
        state,
        decision.selected,
        output_root=output_root,
        provenance=provenance,
        license_findings=licenses,
        external_references=decision.external_references,
    )
    return result


class DeterministicPackagingTests(unittest.TestCase):
    def test_same_snapshot_produces_identical_plugin_tree_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = _package_fixture(base / "first")
            second = _package_fixture(base / "second")
            self.assertEqual(first.plugin_tree_sha256, second.plugin_tree_sha256)
            self.assertEqual(first.zip_sha256, second.zip_sha256)
            self.assertEqual(Path(first.zip_path).read_bytes(), Path(second.zip_path).read_bytes())
            with zipfile.ZipFile(first.zip_path) as archive:
                names = archive.namelist()
                self.assertTrue(names)
                prefixes = {name.split("/", 1)[0] for name in names}
                self.assertEqual(prefixes, {first.plugin_name})
                self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_license_manifest_is_not_mistaken_for_a_license_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = _package_fixture(Path(temporary))
            license_root = Path(result.plugin_dir) / "THIRD_PARTY_LICENSES"
            names = {path.name for path in license_root.iterdir()}
            self.assertTrue(names)
            self.assertNotIn("SKILL.md", names)
            self.assertFalse(any(path.read_text(encoding="utf-8").startswith("---") for path in license_root.iterdir()))

    def test_safe_external_reference_is_copied_without_rewriting_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = _package_fixture(Path(temporary), "external-reference-skill")
            plugin = Path(result.plugin_dir)
            self.assertTrue((plugin / "shared" / "guide.md").is_file())
            skill_text = (plugin / "skills" / "external" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("../../shared/guide.md", skill_text)


if __name__ == "__main__":
    unittest.main()
