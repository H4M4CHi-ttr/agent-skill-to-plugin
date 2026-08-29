from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_skill_to_plugin.models import ParsedInput, ResolvedSource
from agent_skill_to_plugin.resolver_registry import ResolverRegistry


def parsed(kind: str, source: str) -> ParsedInput:
    return ParsedInput(kind=kind, raw_input=source, normalized_input=source, source=source)


def resolved(destination: Path) -> ResolvedSource:
    return ResolvedSource(
        kind="fixture",
        normalized_source="fixture",
        snapshot_path=str(destination),
        snapshot_sha256="0" * 64,
    )


class TimeoutPropagationTests(unittest.TestCase):
    def test_timeout_reaches_git_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with mock.patch(
                "agent_skill_to_plugin.resolvers.git.GitResolver.resolve",
                return_value=resolved(destination),
            ), mock.patch(
                "agent_skill_to_plugin.fetchers.git.GitFetcher",
            ) as fetcher:
                ResolverRegistry().resolve(
                    parsed("git_url", "https://gitlab.example/owner/repo.git"),
                    destination,
                    source_base=Path(temporary),
                    timeout_seconds=17,
                )
            fetcher.assert_called_once_with(timeout=17)

    def test_timeout_reaches_github_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with mock.patch(
                "agent_skill_to_plugin.resolvers.github.GitHubResolver.resolve",
                return_value=resolved(destination),
            ), mock.patch(
                "agent_skill_to_plugin.fetchers.git.GitFetcher",
            ) as fetcher:
                ResolverRegistry().resolve(
                    parsed("github_repository", "https://github.com/owner/repo"),
                    destination,
                    source_base=Path(temporary),
                    timeout_seconds=19,
                )
            fetcher.assert_called_once_with(timeout=19)

    def test_timeout_reaches_http_archive_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with mock.patch(
                "agent_skill_to_plugin.resolvers.archive.ArchiveResolver.resolve",
                return_value=resolved(destination),
            ), mock.patch(
                "agent_skill_to_plugin.fetchers.http.HttpFetcher",
            ) as fetcher:
                ResolverRegistry().resolve(
                    parsed("archive_url", "https://downloads.example/skill.zip"),
                    destination,
                    source_base=Path(temporary),
                    timeout_seconds=23,
                )
            self.assertEqual(2, fetcher.call_count)
            self.assertTrue(all(call.kwargs == {"timeout_seconds": 23} for call in fetcher.call_args_list))

    def test_timeout_reaches_single_manifest_http_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "snapshot"
            with mock.patch(
                "agent_skill_to_plugin.resolvers.http.HttpSkillResolver.resolve",
                return_value=resolved(destination),
            ), mock.patch(
                "agent_skill_to_plugin.fetchers.http.HttpFetcher",
            ) as fetcher:
                ResolverRegistry().resolve(
                    parsed("skill_manifest_url", "https://downloads.example/SKILL.md"),
                    destination,
                    source_base=Path(temporary),
                    timeout_seconds=29,
                )
            self.assertEqual(2, fetcher.call_count)
            self.assertTrue(all(call.kwargs == {"timeout_seconds": 29} for call in fetcher.call_args_list))


if __name__ == "__main__":
    unittest.main()
