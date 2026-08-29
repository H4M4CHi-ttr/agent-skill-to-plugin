from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_skill_to_plugin.validation import detect_external_references

from tests.helpers import copy_fixture


class ExternalReferenceDetectionTests(unittest.TestCase):
    def _fixture_with_body(self, body: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = copy_fixture("external-reference-skill", Path(temporary.name) / "source")
        skill = root / "skills" / "external" / "SKILL.md"
        skill.write_text(
            "---\nname: external\ndescription: External reference fixture.\n---\n\n" + body + "\n",
            encoding="utf-8",
        )
        return temporary, root

    def test_inline_code_relative_reference_is_not_missed(self) -> None:
        temporary, root = self._fixture_with_body("Read `../../shared/guide.md` before use.")
        with temporary:
            plans = detect_external_references(
                root / "skills" / "external",
                root,
            )
        self.assertEqual(1, len(plans))
        self.assertEqual("shared/guide.md", plans[0].source_path)

    def test_plain_prose_relative_reference_is_not_missed(self) -> None:
        temporary, root = self._fixture_with_body("Load ../../shared/guide.md, then continue.")
        with temporary:
            plans = detect_external_references(
                root / "skills" / "external",
                root,
            )
        self.assertEqual(1, len(plans))
        self.assertEqual("../../shared/guide.md", plans[0].raw_reference)


if __name__ == "__main__":
    unittest.main()
