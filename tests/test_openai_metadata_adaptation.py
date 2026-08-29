from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.models import SkillCandidate
from agent_skill_to_plugin.packaging import (
    _normalize_openai_skill_metadata,
    _validate_openai_skill_metadata,
)


class OpenAiMetadataAdaptationTests(unittest.TestCase):
    @staticmethod
    def _candidate(**manifest_overrides: object) -> SkillCandidate:
        manifest = {
            "name": "explicit-skill",
            "description": "Explicit-only fixture for metadata tests.",
            "disable-model-invocation": True,
        }
        manifest.update(manifest_overrides)
        return SkillCandidate(
            id="candidate",
            name="explicit-skill",
            description="Explicit-only fixture for metadata tests.",
            path="skills/explicit-skill",
            plugin=None,
            selection_reason="fixture",
            valid=True,
            priority=0,
            manifest=manifest,
        )

    def test_explicit_only_claude_flag_is_represented_as_openai_policy(self) -> None:
        candidate = self._candidate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text(
                "---\nname: explicit-skill\ndescription: Explicit-only fixture.\n"
                "disable-model-invocation: true\n---\nBody.\n",
                encoding="utf-8",
            )

            adaptations = _normalize_openai_skill_metadata(root, candidate)
            _validate_openai_skill_metadata(root)

            self.assertIn("disable-model-invocation: false", (root / "SKILL.md").read_text(encoding="utf-8"))
            agent = yaml.safe_load((root / "agents" / "openai.yaml").read_text(encoding="utf-8"))
            self.assertIs(agent["policy"]["allow_implicit_invocation"], False)
            self.assertIn("$explicit-skill", agent["interface"]["default_prompt"])
            self.assertEqual(2, len(adaptations))

    def test_scalar_key_variants_preserve_comments_and_fail_closed_on_flow_yaml(self) -> None:
        candidate = self._candidate()
        for declaration in (
            "disable_model_invocation: true # source intent",
            '"disable-model-invocation": TRUE # source intent',
        ):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "SKILL.md").write_text(
                    "---\nname: explicit-skill\ndescription: Explicit-only fixture.\n"
                    f"{declaration}\n---\nBody.\n",
                    encoding="utf-8",
                )
                _normalize_openai_skill_metadata(root, candidate)
                generated = (root / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn("false # source intent", generated)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text(
                "---\n{name: explicit-skill, description: Explicit-only fixture., "
                "disable-model-invocation: true}\n---\nBody.\n",
                encoding="utf-8",
            )
            with self.assertRaises(SkillToPluginError):
                _normalize_openai_skill_metadata(root, candidate)

    def test_frontmatter_delimiter_is_normalized_without_losing_crlf(self) -> None:
        candidate = self._candidate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_bytes(
                b"---\r\nname: explicit-skill\r\ndescription: Explicit-only fixture.\r\n"
                b"disable-model-invocation: true\r\n...\r\nBody.\r\n"
            )
            adaptations = _normalize_openai_skill_metadata(root, candidate)
            generated = (root / "SKILL.md").read_bytes()
            self.assertIn(b"disable-model-invocation: false\r\n---\r\nBody.\r\n", generated)
            self.assertNotIn(b"\n...\r\n", generated)
            self.assertIn("closing delimiter", adaptations[0]["change"])
            _validate_openai_skill_metadata(root)

    def test_existing_agent_metadata_changes_are_field_level_and_prompt_invokes_skill(self) -> None:
        candidate = self._candidate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text(
                "---\nname: explicit-skill\ndescription: Explicit-only fixture.\n"
                "disable-model-invocation: true\n---\nBody.\n",
                encoding="utf-8",
            )
            (root / "assets").mkdir()
            (root / "assets" / "icon.svg").write_text("<svg/>", encoding="utf-8")
            (root / "agents").mkdir()
            (root / "agents" / "openai.yaml").write_text(
                "interface:\n"
                "  display_name: Explicit Skill\n"
                "  short_description: Tiny\n"
                "  icon_small: ./assets/icon.svg\n"
                "  default_prompt: Help me with this task.\n"
                "  unrecognized: remove me\n"
                "policy:\n"
                "  products: [CHAT]\n"
                "  allow_implicit_invocation: true\n"
                "unknown_top: remove me\n",
                encoding="utf-8",
            )

            adaptations = _normalize_openai_skill_metadata(root, candidate)
            _validate_openai_skill_metadata(root)

            agent_adaptation = next(item for item in adaptations if item["path"].endswith("openai.yaml"))
            self.assertIn("interface.unrecognized", agent_adaptation["removed_fields"])
            self.assertIn("policy.products", agent_adaptation["removed_fields"])
            self.assertIn("unknown_top", agent_adaptation["removed_fields"])
            self.assertIn("interface.default_prompt", agent_adaptation["changed_fields"])
            self.assertIn("policy.allow_implicit_invocation", agent_adaptation["changed_fields"])
            generated = yaml.safe_load((root / "agents" / "openai.yaml").read_text(encoding="utf-8"))
            self.assertIn("$explicit-skill", generated["interface"]["default_prompt"])
            self.assertGreaterEqual(len(generated["interface"]["short_description"]), 25)

    def test_missing_and_traversing_icons_are_rejected(self) -> None:
        for icon_path in ("./assets/missing.svg", "../../outside.svg"):
            with self.subTest(icon_path=icon_path), tempfile.TemporaryDirectory() as temporary:
                skill_root = Path(temporary) / "plugin" / "skills" / "explicit-skill"
                (skill_root / "agents").mkdir(parents=True)
                (skill_root / "SKILL.md").write_text(
                    "---\nname: explicit-skill\ndescription: Explicit-only fixture.\n---\nBody.\n",
                    encoding="utf-8",
                )
                (skill_root / "agents" / "openai.yaml").write_text(
                    "interface:\n"
                    "  display_name: Explicit Skill\n"
                    "  short_description: Explicit fixture description\n"
                    f"  icon_small: {icon_path}\n",
                    encoding="utf-8",
                )
                with self.assertRaises(SkillToPluginError):
                    _validate_openai_skill_metadata(skill_root)


if __name__ == "__main__":
    unittest.main()
