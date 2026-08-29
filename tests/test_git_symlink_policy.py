from __future__ import annotations

import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from agent_skill_to_plugin.application import _reject_selected_skipped_links, _source_diagnostics
from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.fetchers.archive import extract_archive
from agent_skill_to_plugin.models import ParsedInput, ResolutionState, ResolvedSource, SkillCandidate


class GitSymlinkPolicyTests(unittest.TestCase):
    def test_git_tar_can_skip_links_but_generic_tar_still_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "git.tar"
            with tarfile.open(archive_path, "w") as archive:
                content = b"regular"
                regular = tarfile.TarInfo("skills/example/SKILL.md")
                regular.size = len(content)
                archive.addfile(regular, io.BytesIO(content))
                link = tarfile.TarInfo("AGENTS.md")
                link.type = tarfile.SYMTYPE
                link.linkname = "README.md"
                archive.addfile(link)

            with self.assertRaises(SkillToPluginError):
                extract_archive(archive_path, root / "strict")
            result = extract_archive(
                archive_path,
                root / "git-snapshot",
                skip_symbolic_links=True,
            )
            self.assertEqual(("AGENTS.md",), result.skipped_symbolic_links)
            self.assertFalse((root / "git-snapshot" / "AGENTS.md").exists())

    def test_selected_skill_boundary_may_not_contain_a_skipped_link(self) -> None:
        resolved = ResolvedSource(
            kind="git_repository",
            normalized_source="https://example.invalid/repo.git",
            snapshot_path="snapshot",
            snapshot_sha256="0" * 64,
            metadata={"skipped_symbolic_links": ["skills/example/reference.md"]},
        )
        state = ResolutionState(
            resolution_id="fixture",
            status="resolved",
            created_at="2026-08-29T00:00:00Z",
            input=ParsedInput(kind="git_url", raw_input="fixture", normalized_input="fixture"),
            resolved_source=resolved,
            candidates=(),
            selection_policy="structural",
            resolution_file="resolution.json",
            output_root="output",
        )
        candidate = SkillCandidate(
            id="candidate",
            name="example",
            description="fixture",
            path="skills/example",
            plugin=None,
            selection_reason="fixture",
            valid=True,
            priority=0,
        )
        self.assertTrue(_source_diagnostics(resolved))
        with self.assertRaises(SkillToPluginError) as raised:
            _reject_selected_skipped_links(state, (candidate,))
        self.assertEqual("security_rejected", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
