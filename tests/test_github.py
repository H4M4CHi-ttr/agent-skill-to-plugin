from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from agent_skill_to_plugin.fetchers.git import GitFetchResult, GitFetcher, GitRefs
from agent_skill_to_plugin.fetchers.github_api import parse_github_url
from agent_skill_to_plugin.input_parser import parse_input
from agent_skill_to_plugin.resolvers.github import GitHubResolver

from tests.helpers import FIXTURES


TRUNK_COMMIT = "1" * 40
FEATURE_COMMIT = "2" * 40
TAG_COMMIT = "3" * 40
DIRECT_COMMIT = "4" * 40


class FakeGitFetcher:
    """Local-only Git transport double that still exercises ref pinning."""

    timeout = 1

    def __init__(self) -> None:
        self.refs = GitRefs(
            default_ref="refs/heads/trunk",
            head_commit=TRUNK_COMMIT,
            refs={
                "refs/heads/trunk": TRUNK_COMMIT,
                "refs/heads/feature/x": FEATURE_COMMIT,
                "refs/tags/v1.2.3": TAG_COMMIT,
            },
        )
        self.list_calls: list[str] = []
        self.fetch_calls: list[dict[str, object]] = []

    def list_refs(self, source: str) -> GitRefs:
        self.list_calls.append(source)
        return self.refs

    @staticmethod
    def resolve_ref(refs: GitRefs, requested_ref: str | None) -> tuple[str | None, str]:
        return GitFetcher.resolve_ref(refs, requested_ref)

    def fetch(
        self,
        source: str,
        destination: Path,
        *,
        requested_ref: str | None = None,
        expected_commit: str | None = None,
    ) -> GitFetchResult:
        resolved_ref, commit = self.resolve_ref(self.refs, requested_ref)
        if expected_commit != commit:
            raise AssertionError("resolver did not pin the advertised commit")
        self.fetch_calls.append(
            {
                "source": source,
                "requested_ref": requested_ref,
                "expected_commit": expected_commit,
            }
        )
        shutil.copytree(FIXTURES / "single-skill-repo", destination)
        return GitFetchResult(
            source=source,
            requested_ref=requested_ref,
            resolved_ref=resolved_ref,
            commit=commit,
            snapshot_path=destination,
        )


REF_NAMES = {"trunk", "feature/x", "v1.2.3"}


class GitHubUrlParsingTests(unittest.TestCase):
    def test_git_transport_accepts_non_secret_ssh_user_only(self) -> None:
        GitFetcher.validate_source("ssh://git@github.com/acme/porter.git")
        GitFetcher.validate_source("git@github.com:acme/porter.git")
        with self.assertRaisesRegex(Exception, "credentials"):
            GitFetcher.validate_source("ssh://git:password@github.com/acme/porter.git")

    def test_repository_root(self) -> None:
        location = parse_github_url("https://github.com/acme/porter")
        self.assertEqual("repository", location.kind)
        self.assertIsNone(location.ref)
        self.assertIsNone(location.path)

    def test_tree_uses_longest_slash_branch(self) -> None:
        location = parse_github_url(
            "https://github.com/acme/porter/tree/feature/x/skills/example",
            REF_NAMES,
        )
        self.assertEqual("tree", location.kind)
        self.assertEqual("feature/x", location.ref)
        self.assertEqual("skills/example", location.path)

    def test_blob_tag_and_direct_manifest(self) -> None:
        location = parse_github_url(
            "https://github.com/acme/porter/blob/v1.2.3/skills/example/SKILL.md",
            REF_NAMES,
        )
        self.assertEqual("blob", location.kind)
        self.assertEqual("v1.2.3", location.ref)
        self.assertEqual("skills/example/SKILL.md", location.path)

    def test_raw_url_with_slash_branch(self) -> None:
        location = parse_github_url(
            "https://raw.githubusercontent.com/acme/porter/feature/x/skills/example/SKILL.md",
            REF_NAMES,
        )
        self.assertEqual("raw", location.kind)
        self.assertEqual("feature/x", location.ref)
        self.assertEqual("skills/example/SKILL.md", location.path)

    def test_commit_sha_is_accepted_without_ref_guessing(self) -> None:
        location = parse_github_url(
            f"https://github.com/acme/porter/tree/{DIRECT_COMMIT}/skills/example",
            REF_NAMES,
        )
        self.assertEqual(DIRECT_COMMIT, location.ref)
        self.assertEqual("skills/example", location.path)

    def test_encoded_path_query_and_fragment_are_normalized(self) -> None:
        parsed = parse_input(
            "[skill](https://github.com/acme/porter/tree/trunk/skills/%65xample"
            "?tab=readme#section)"
        )
        location = parse_github_url(parsed.source or "", REF_NAMES)
        self.assertEqual("trunk", location.ref)
        self.assertEqual("skills/example", location.path)
        self.assertNotIn("?", parsed.normalized_input)
        self.assertNotIn("#", parsed.normalized_input)

    def test_encoded_slash_in_ref_does_not_consume_repository_path(self) -> None:
        location = parse_github_url(
            "https://github.com/acme/porter/tree/feature%2Fx/skills/example",
            REF_NAMES,
        )
        self.assertEqual("feature/x", location.ref)
        self.assertEqual("skills/example", location.path)


class GitHubResolverTests(unittest.TestCase):
    def resolve(self, value: str):
        parsed = parse_input(value)
        fetcher = FakeGitFetcher()
        resolver = GitHubResolver(fetcher)
        temporary = tempfile.TemporaryDirectory()
        destination = Path(temporary.name) / "snapshot"
        patcher = mock.patch.object(
            GitHubResolver,
            "_license_metadata",
            return_value={"github_license_status": "unavailable"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(temporary.cleanup)
        return resolver.resolve(parsed, destination), fetcher

    def test_default_branch_need_not_be_main(self) -> None:
        resolved, fetcher = self.resolve("https://github.com/acme/porter")
        self.assertEqual("refs/heads/trunk", resolved.requested_ref)
        self.assertEqual(TRUNK_COMMIT, resolved.resolved_commit)
        self.assertEqual("github_repository", resolved.kind)
        self.assertEqual(["https://github.com/acme/porter"], fetcher.list_calls)
        self.assertEqual(TRUNK_COMMIT, fetcher.fetch_calls[0]["expected_commit"])

    def test_slash_branch_is_pinned_before_fetch(self) -> None:
        resolved, fetcher = self.resolve(
            "https://github.com/acme/porter/tree/feature/x/skills/example"
        )
        self.assertEqual("feature/x", resolved.requested_ref)
        self.assertEqual(FEATURE_COMMIT, resolved.resolved_commit)
        self.assertEqual("skills/example", resolved.requested_path)
        self.assertEqual("feature/x", fetcher.fetch_calls[0]["requested_ref"])

    def test_tag_blob_path_is_preserved(self) -> None:
        resolved, _ = self.resolve(
            "https://github.com/acme/porter/blob/v1.2.3/skills/example/SKILL.md"
        )
        self.assertEqual("v1.2.3", resolved.requested_ref)
        self.assertEqual(TAG_COMMIT, resolved.resolved_commit)
        self.assertEqual("skills/example/SKILL.md", resolved.requested_path)

    def test_full_sha_is_the_immutable_fetch_target(self) -> None:
        resolved, fetcher = self.resolve(
            f"https://github.com/acme/porter/tree/{DIRECT_COMMIT}/skills/example"
        )
        self.assertEqual(DIRECT_COMMIT, resolved.requested_ref)
        self.assertEqual(DIRECT_COMMIT, resolved.resolved_commit)
        self.assertEqual(DIRECT_COMMIT, fetcher.fetch_calls[0]["expected_commit"])

    def test_raw_manifest_url_uses_the_same_git_resolution(self) -> None:
        resolved, _ = self.resolve(
            "https://raw.githubusercontent.com/acme/porter/feature/x/skills/example/SKILL.md"
        )
        self.assertEqual("feature/x", resolved.requested_ref)
        self.assertEqual(FEATURE_COMMIT, resolved.resolved_commit)
        self.assertEqual("skills/example/SKILL.md", resolved.requested_path)


if __name__ == "__main__":
    unittest.main()
