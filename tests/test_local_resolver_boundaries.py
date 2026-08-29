from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.models import ParsedInput
from agent_skill_to_plugin.resolvers.local import LocalResolver

from tests.helpers import copy_fixture


def local_input(source: Path, *, snapshot_exact_root: bool = False) -> ParsedInput:
    return ParsedInput(
        kind="local",
        raw_input=str(source),
        normalized_input=str(source),
        source=str(source),
        metadata={"snapshot_exact_root": True} if snapshot_exact_root else {},
    )


class LocalResolverBoundaryTests(unittest.TestCase):
    def test_destination_inside_direct_source_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            destination = source / "converted" / "resolutions" / "fixed.snapshot"
            destination.parent.mkdir(parents=True)

            with mock.patch.object(LocalResolver, "_git_metadata", return_value=(None, None, None)):
                with self.assertRaises(SkillToPluginError) as raised:
                    LocalResolver().resolve(
                        local_input(source),
                        destination,
                        source_base=root,
                    )

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual(
                "destination_within_source",
                raised.exception.details["relationship"],
            )
            self.assertFalse(destination.exists())
            self.assertEqual(
                "example",
                (source / "skills" / "example" / "SKILL.md")
                .read_text(encoding="utf-8")
                .split("name: ", 1)[1]
                .splitlines()[0],
            )

    def test_destination_inside_detected_git_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = copy_fixture("single-skill-repo", root / "repository")
            selected_skill = repository / "skills" / "example"
            destination = repository / "converted" / "resolutions" / "fixed.snapshot"
            destination.parent.mkdir(parents=True)

            with mock.patch.object(
                LocalResolver,
                "_git_metadata",
                return_value=("a" * 40, False, repository.resolve()),
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    LocalResolver().resolve(
                        local_input(selected_skill),
                        destination,
                        source_base=root,
                    )

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual(
                "destination_within_source",
                raised.exception.details["relationship"],
            )
            self.assertEqual(
                str(repository.resolve()).casefold(),
                raised.exception.details["source_boundary"].casefold(),
            )
            self.assertFalse(destination.exists())

    def test_disjoint_output_still_produces_a_valid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            destination = root / "output" / "resolutions" / "fixed.snapshot"

            with mock.patch.object(LocalResolver, "_git_metadata", return_value=(None, None, None)):
                resolved = LocalResolver().resolve(
                    local_input(source),
                    destination,
                    source_base=root,
                )

            self.assertEqual(destination.resolve(), Path(resolved.snapshot_path))
            self.assertTrue((destination / "skills" / "example" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
