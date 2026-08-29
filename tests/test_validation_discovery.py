from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_skill_to_plugin.discovery import (
    PRIORITY_ANCESTOR,
    PRIORITY_DESCENDANT,
    PRIORITY_EXACT,
    discover_skills,
)
from agent_skill_to_plugin.selection import select_candidates
from agent_skill_to_plugin.validation import build_skill_candidate, parse_skill_manifest, validate_tree

from tests.helpers import FIXTURES, copy_fixture, resolved_source


class ManifestValidationTests(unittest.TestCase):
    def test_folded_yaml_is_parsed_by_safe_loader(self) -> None:
        result = parse_skill_manifest(FIXTURES / "single-skill-repo" / "skills" / "example" / "SKILL.md")
        self.assertTrue(result.valid)
        self.assertEqual(result.name, "example")
        self.assertEqual(result.description, "A deterministic local fixture Skill.")

    def test_invalid_frontmatter_remains_an_invalid_candidate_with_path(self) -> None:
        root = FIXTURES / "invalid-frontmatter"
        tree = validate_tree(root)
        manifest = root / "skills" / "broken" / "SKILL.md"
        candidate = build_skill_candidate(
            root,
            manifest,
            snapshot_sha256=tree.tree_sha256,
            selection_reason="fixture",
            priority=0,
        )
        self.assertFalse(candidate.valid)
        self.assertEqual(candidate.path, "skills/broken")
        self.assertTrue(candidate.diagnostics)
        self.assertEqual(candidate.diagnostics[0].path, "skills/broken/SKILL.md")
        self.assertEqual(candidate.diagnostics[0].code, "skill_manifest_yaml_invalid")

    def test_safe_loader_rejects_python_object_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "SKILL.md"
            skill.write_text(
                "---\nname: tagged\ndescription: !!python/object/apply:os.system ['echo unsafe']\n---\nBody.\n",
                encoding="utf-8",
            )
            result = parse_skill_manifest(skill)
            self.assertFalse(result.valid)
            self.assertTrue(any(item.code == "skill_manifest_yaml_invalid" for item in result.diagnostics))

    def test_duplicate_explicit_yaml_keys_are_diagnosed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "SKILL.md"
            skill.write_text(
                "---\nname: first\nname: second\ndescription: Ambiguous input.\n---\nBody.\n",
                encoding="utf-8",
            )
            result = parse_skill_manifest(skill)
            self.assertFalse(result.valid)
            self.assertTrue(any(item.code == "skill_manifest_yaml_invalid" for item in result.diagnostics))


class DiscoveryTests(unittest.TestCase):
    def test_repository_root_enumerates_multiple_skills_without_auto_selection(self) -> None:
        resolved = resolved_source(FIXTURES / "multi-skill-repo")
        candidates = discover_skills(resolved)
        self.assertEqual([item.name for item in candidates], ["alpha", "beta"])
        self.assertEqual(select_candidates(candidates).status, "needs_selection")

    def test_exact_skill_directory_has_highest_priority(self) -> None:
        resolved = resolved_source(FIXTURES / "multi-skill-repo", requested_path="skills/alpha")
        candidates = discover_skills(resolved)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "alpha")
        self.assertEqual(candidates[0].priority, PRIORITY_EXACT)
        self.assertEqual(select_candidates(candidates).status, "selected")

    def test_nearest_ancestor_skill_boundary_wins(self) -> None:
        resolved = resolved_source(
            FIXTURES / "multi-skill-repo",
            requested_path="skills/alpha/references/note.md",
        )
        candidates = discover_skills(resolved)
        self.assertEqual([item.name for item in candidates], ["alpha"])
        self.assertEqual(candidates[0].priority, PRIORITY_ANCESTOR)

    def test_requested_subtree_uses_descendant_candidates_only(self) -> None:
        resolved = resolved_source(FIXTURES / "multi-skill-repo", requested_path="skills")
        candidates = discover_skills(resolved)
        self.assertEqual({item.name for item in candidates}, {"alpha", "beta"})
        self.assertTrue(all(item.priority == PRIORITY_DESCENDANT for item in candidates))

    def test_invalid_repository_manifest_is_not_silently_dropped(self) -> None:
        resolved = resolved_source(FIXTURES / "invalid-frontmatter")
        candidates = discover_skills(resolved)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].valid)
        self.assertEqual(candidates[0].path, "skills/broken")

    def test_claude_plugin_scope_returns_all_valid_skills(self) -> None:
        root = FIXTURES / "claude-plugin-multiple-skills"
        resolved = resolved_source(root, plugin_name="fixture-claude-plugin", metadata={"plugin_root": "."})
        candidates = discover_skills(resolved, plugin_scope=True)
        self.assertEqual({item.name for item in candidates if item.valid}, {"first", "second"})
        self.assertFalse(any("commands" in item.path for item in candidates))
        decision = select_candidates(candidates, plugin_scope=True, selection_policy="claude_plugin_all")
        self.assertEqual(decision.status, "selected")
        self.assertEqual({item.name for item in decision.selected}, {"first", "second"})


if __name__ == "__main__":
    unittest.main()
