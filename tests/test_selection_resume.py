from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.selection import load_resolution_state, resume_selection, select_candidates

from tests.helpers import persisted_state


class SelectionTests(unittest.TestCase):
    def test_generic_multiple_candidates_need_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, _ = persisted_state(Path(temporary) / "out", "multi-skill-repo")
            decision = select_candidates(state.candidates)
            self.assertTrue(decision.needs_selection)
            self.assertFalse(decision.selected)

    def test_all_selects_all_valid_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, _ = persisted_state(Path(temporary) / "out", "multi-skill-repo")
            decision = select_candidates(state.candidates, selected="all")
            self.assertEqual({item.name for item in decision.selected}, {"alpha", "beta"})

    def test_duplicate_skill_names_cannot_be_selected_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, _ = persisted_state(Path(temporary) / "out", "multi-skill-repo")
            first, second = state.candidates
            duplicate = replace(second, name=first.name)
            with self.assertRaises(SkillToPluginError) as raised:
                select_candidates((first, duplicate), selected="all")
            self.assertEqual(raised.exception.code, "package_validation_failed")


class ResolutionResumeTests(unittest.TestCase):
    def test_resume_by_candidate_id_reuses_the_fixed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, resolution_file = persisted_state(Path(temporary) / "out", "multi-skill-repo")
            loaded, decision = resume_selection(resolution_file, state.candidates[0].id)
            self.assertEqual(loaded.resolution_id, state.resolution_id)
            self.assertEqual(decision.selected_ids, (state.candidates[0].id,))

    def test_snapshot_mutation_is_rejected_instead_of_refetching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, resolution_file = persisted_state(Path(temporary) / "out", "multi-skill-repo")
            snapshot = Path(state.resolved_source.snapshot_path)
            (snapshot / "skills" / "alpha" / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: changed\n---\nChanged.\n",
                encoding="utf-8",
            )
            with self.assertRaises(SkillToPluginError) as raised:
                load_resolution_state(resolution_file)
            self.assertEqual(raised.exception.code, "resolution_integrity_failed")

    def test_candidate_record_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _state, resolution_file = persisted_state(Path(temporary) / "out", "single-skill-repo")
            data = json.loads(resolution_file.read_text(encoding="utf-8"))
            data["candidates"][0]["id"] = "skill-tampered"
            resolution_file.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SkillToPluginError) as raised:
                load_resolution_state(resolution_file)
            self.assertEqual(raised.exception.code, "resolution_integrity_failed")

    def test_schema_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _state, resolution_file = persisted_state(Path(temporary) / "out", "single-skill-repo")
            data = json.loads(resolution_file.read_text(encoding="utf-8"))
            data["schema_version"] = "999"
            resolution_file.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SkillToPluginError) as raised:
                load_resolution_state(resolution_file)
            self.assertEqual(raised.exception.code, "resolution_integrity_failed")

    def test_snapshot_must_remain_inside_output_resolutions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state, resolution_file = persisted_state(base / "out", "single-skill-repo")
            outside = base / "outside"
            outside.mkdir()
            escaped = replace(
                state,
                resolved_source=replace(
                    state.resolved_source,
                    snapshot_path=str(outside.resolve()),
                    snapshot_sha256="0" * 64,
                ),
            )
            resolution_file.write_text(json.dumps(escaped.to_dict()), encoding="utf-8")
            with self.assertRaises(SkillToPluginError) as raised:
                load_resolution_state(resolution_file)
            self.assertEqual(raised.exception.code, "resolution_integrity_failed")


if __name__ == "__main__":
    unittest.main()
