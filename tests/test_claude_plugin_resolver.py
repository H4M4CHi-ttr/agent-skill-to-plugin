from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from agent_skill_to_plugin.discovery import discover_skills
from agent_skill_to_plugin.errors import NeedsInputError, SkillToPluginError
from agent_skill_to_plugin.models import ParsedInput, PluginSource, ResolvedSource
from agent_skill_to_plugin.resolvers.claude_plugin import (
    ClaudePluginResolver,
    KNOWN_MARKETPLACES,
    _parse_plugin_source,
)
from agent_skill_to_plugin.selection import select_candidates
from agent_skill_to_plugin.utils import hash_tree


FIXTURES = PROJECT_ROOT / "fixtures"


def claude_input(
    marketplace: str,
    plugin: str,
    source: str | None,
) -> ParsedInput:
    return ParsedInput(
        kind="claude_plugin",
        raw_input=f"/plugin install {plugin}@{marketplace}",
        normalized_input=f"{plugin}@{marketplace}",
        source=source,
        marketplace_source=source,
        marketplace_name=marketplace,
        plugin_name=plugin,
        plugin_scope=True,
    )


class ClaudePluginResolverTests(unittest.TestCase):
    def test_known_marketplace_shorthand_cannot_be_shadowed_by_local_tree(self) -> None:
        shorthand = KNOWN_MARKETPLACES["claude-plugins-official"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shadow = root / Path(shorthand)
            (shadow / ".claude-plugin").mkdir(parents=True)
            (shadow / ".claude-plugin/marketplace.json").write_text(
                '{"name":"attacker-controlled","plugins":[]}',
                encoding="utf-8",
            )
            destination = root / "snapshot"
            remote = ResolvedSource(
                kind="github_repository",
                normalized_source=f"https://github.com/{shorthand}",
                snapshot_path=str(destination),
                snapshot_sha256="a" * 64,
                resolved_commit="b" * 40,
                resolution_method="fixture-remote",
            )

            with mock.patch(
                "agent_skill_to_plugin.resolvers.claude_plugin.GitHubResolver.resolve",
                return_value=remote,
            ) as github_resolve:
                result = ClaudePluginResolver._acquire_marketplace(
                    shorthand,
                    destination,
                    source_base=root,
                    timeout=30,
                )

            self.assertIs(remote, result)
            parsed = github_resolve.call_args.args[0]
            self.assertEqual("github_repository", parsed.kind)
            self.assertEqual(shorthand, parsed.source)

    def test_explicit_relative_marketplace_path_remains_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "owner" / "repo"
            (local / ".claude-plugin").mkdir(parents=True)
            (local / ".claude-plugin/marketplace.json").write_text(
                '{"name":"local-marketplace","plugins":[]}',
                encoding="utf-8",
            )

            result = ClaudePluginResolver._acquire_marketplace(
                "./owner/repo",
                root / "snapshot",
                source_base=root,
                timeout=30,
            )

            self.assertEqual("local_skill", result.kind)
            self.assertEqual(str(local.resolve()), result.normalized_source)

    def test_local_marketplace_does_not_expand_to_enclosing_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            marketplace = repository / "marketplace"
            shutil.copytree(FIXTURES / "claude-marketplace-relative", marketplace)
            (repository / ".env").write_text("SHOULD_NOT_BE_COPIED=1", encoding="utf-8")
            parsed = claude_input("fixture-marketplace", "demo-relative", str(marketplace))

            with mock.patch(
                "agent_skill_to_plugin.resolvers.local.LocalResolver._git_metadata",
                return_value=("a" * 40, False, repository),
            ):
                resolved = ClaudePluginResolver().resolve(
                    parsed,
                    root / "snapshot",
                    root,
                    30,
                )

            self.assertEqual("claude_plugin", resolved.kind)
            self.assertFalse((Path(resolved.snapshot_path) / ".env").exists())
            candidates = discover_skills(resolved, plugin_scope=True)
            self.assertEqual(["alpha", "beta"], [candidate.name for candidate in candidates])

    def test_relative_plugin_uses_canonical_marketplace_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marketplace = root / "marketplace"
            plugin = marketplace / "plugins" / "demo"
            manifest = marketplace / ".claude-plugin" / "marketplace.json"
            plugin.mkdir(parents=True)
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"fixture","plugins":[]}', encoding="utf-8")
            (plugin / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Canonical path fixture.\n---\nBody.\n",
                encoding="utf-8",
            )
            alias = root / "marketplace-alias"
            try:
                alias.symlink_to(marketplace, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            resolved, plugin_root = ClaudePluginResolver._acquire_plugin(
                PluginSource(kind="relative", value="./plugins/demo"),
                root / "snapshot",
                marketplace_snapshot=alias,
                marketplace_manifest=alias / ".claude-plugin" / "marketplace.json",
                source_base=root,
                timeout=30,
            )

            self.assertEqual("plugins/demo", plugin_root)
            self.assertTrue((Path(resolved.snapshot_path) / "plugins/demo/SKILL.md").is_file())

    def test_relative_source_resolves_plugin_boundary_and_selects_all_skills(self) -> None:
        source = FIXTURES / "claude-marketplace-relative"
        parsed = claude_input("fixture-marketplace", "demo-relative", str(source))
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshot"
            resolved = ClaudePluginResolver().resolve(parsed, snapshot, PROJECT_ROOT, 30)
            self.assertEqual("claude_plugin", resolved.kind)
            self.assertEqual("plugins/demo-relative", resolved.metadata["plugin_root"])
            self.assertEqual("relative", resolved.plugin_source.kind)

            candidates = discover_skills(resolved, plugin_scope=True)
            self.assertEqual(["alpha", "beta"], [candidate.name for candidate in candidates])
            decision = select_candidates(candidates, plugin_scope=True)
            self.assertEqual("selected", decision.status)
            self.assertEqual(2, len(decision.selected))
            self.assertFalse(any("commands" in candidate.path for candidate in candidates))

    def test_source_dot_uses_marketplace_root_as_plugin_root(self) -> None:
        source = FIXTURES / "claude-marketplace-root"
        parsed = claude_input("root-marketplace", "root-plugin", str(source))
        with tempfile.TemporaryDirectory() as temporary:
            resolved = ClaudePluginResolver().resolve(
                parsed, Path(temporary) / "snapshot", PROJECT_ROOT, 30
            )
            self.assertEqual(".", resolved.metadata["plugin_root"])
            candidates = discover_skills(resolved, plugin_scope=True)
            self.assertEqual(["root-skill"], [candidate.name for candidate in candidates])

    def test_plugin_with_no_skill_resolves_but_selection_reports_no_candidates(self) -> None:
        source = FIXTURES / "claude-marketplace-relative"
        parsed = claude_input("fixture-marketplace", "no-skills", str(source))
        with tempfile.TemporaryDirectory() as temporary:
            resolved = ClaudePluginResolver().resolve(
                parsed, Path(temporary) / "snapshot", PROJECT_ROOT, 30
            )
            candidates = discover_skills(resolved, plugin_scope=True)
            self.assertEqual((), candidates)
            with self.assertRaises(SkillToPluginError) as raised:
                select_candidates(candidates, plugin_scope=True)
            self.assertEqual("no_skill_candidates", raised.exception.code)

    def test_command_source_is_rejected_without_executor(self) -> None:
        source = FIXTURES / "claude-marketplace-relative"
        parsed = claude_input("fixture-marketplace", "command-source", str(source))
        # Resolving the local Marketplace may legitimately invoke read-only Git
        # metadata probes.  The security boundary is that a command-backed
        # plugin source never reaches any plugin acquisition/execution path.
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            ClaudePluginResolver,
            "_acquire_plugin",
            side_effect=AssertionError("command source must not reach acquisition"),
        ):
            with self.assertRaises(SkillToPluginError) as raised:
                ClaudePluginResolver().resolve(
                    parsed, Path(temporary) / "snapshot", PROJECT_ROOT, 30
                )
            self.assertEqual("security_rejected", raised.exception.code)

    def test_unknown_marketplace_and_plugin_have_distinct_outcomes(self) -> None:
        source = FIXTURES / "claude-marketplace-relative"
        cases = (
            (claude_input("missing-marketplace", "demo-relative", str(source)), "unknown_marketplace"),
            (claude_input("fixture-marketplace", "missing-plugin", str(source)), "unknown_plugin"),
        )
        for parsed, expected_code in cases:
            with self.subTest(code=expected_code), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(SkillToPluginError) as raised:
                    ClaudePluginResolver().resolve(
                        parsed, Path(temporary) / "snapshot", PROJECT_ROOT, 30
                    )
                self.assertEqual(expected_code, raised.exception.code)

    def test_unresolved_marketplace_requests_source_instead_of_guessing(self) -> None:
        parsed = claude_input("unknown-marketplace", "demo", None)
        resolver = ClaudePluginResolver(
            claude_executable="",
            marketplace_searcher=lambda marketplace, plugin, work, timeout: [],
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(NeedsInputError) as raised:
                resolver.resolve(parsed, Path(temporary) / "snapshot", PROJECT_ROOT, 30)
            self.assertEqual("marketplace_source", raised.exception.details["prompt_kind"])

    def test_registered_marketplace_cli_is_read_only_fixed_argv(self) -> None:
        source = FIXTURES / "claude-marketplace-relative"
        parsed = claude_input("fixture-marketplace", "demo-relative", None)
        calls: list[tuple[list[str], dict]] = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            stdout = json.dumps(
                {"marketplaces": [{"name": "fixture-marketplace", "source": str(source)}]}
            )
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        resolver = ClaudePluginResolver(claude_executable="claude", subprocess_runner=runner)
        with tempfile.TemporaryDirectory() as temporary:
            result = resolver.resolve(parsed, Path(temporary) / "snapshot", PROJECT_ROOT, 30)
        self.assertEqual(
            ["claude", "plugin", "marketplace", "list", "--json"],
            calls[0][0],
        )
        self.assertIs(False, calls[0][1]["shell"])
        self.assertEqual("claude-cli-marketplace-list", result.marketplace.resolution_method)

    def test_duplicate_marketplace_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "marketplace"
            (source / ".claude-plugin").mkdir(parents=True)
            (source / ".claude-plugin/marketplace.json").write_text(
                '{"name":"duplicate","name":"duplicate","plugins":[]}',
                encoding="utf-8",
            )
            parsed = claude_input("duplicate", "plugin", str(source))
            with self.assertRaises(SkillToPluginError) as raised:
                ClaudePluginResolver().resolve(parsed, root / "snapshot", PROJECT_ROOT, 30)
            self.assertEqual("invalid_manifest", raised.exception.code)

    def test_relative_source_cannot_escape_marketplace_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "marketplace"
            (source / ".claude-plugin").mkdir(parents=True)
            (source / ".claude-plugin/marketplace.json").write_text(
                json.dumps(
                    {
                        "name": "escape-marketplace",
                        "plugins": [{"name": "escape", "source": "../outside"}],
                    }
                ),
                encoding="utf-8",
            )
            parsed = claude_input("escape-marketplace", "escape", str(source))
            with self.assertRaises(SkillToPluginError) as raised:
                ClaudePluginResolver().resolve(parsed, root / "snapshot", PROJECT_ROOT, 30)
            self.assertEqual("security_rejected", raised.exception.code)


class ClaudePluginSourceTests(unittest.TestCase):
    def test_ref_and_sha_mismatch_is_rejected_and_snapshot_removed(self) -> None:
        expected = "a" * 40
        actual = "b" * 40
        plugin_source = _parse_plugin_source(
            {
                "name": "p",
                "source": {
                    "source": "github",
                    "repo": "owner/repo",
                    "ref": "release/v1",
                    "sha": expected,
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "snapshot"
            marketplace = root / "marketplace"
            (marketplace / ".claude-plugin").mkdir(parents=True)
            manifest = marketplace / ".claude-plugin/marketplace.json"
            manifest.write_text("{}", encoding="utf-8")

            def fake_resolve(_self, parsed, target):
                self.assertEqual("release/v1", parsed.requested_ref)
                target.mkdir(parents=True)
                (target / "payload.txt").write_text("untrusted", encoding="utf-8")
                return ResolvedSource(
                    kind="github_repository",
                    normalized_source="https://github.com/owner/repo",
                    snapshot_path=str(target),
                    snapshot_sha256=hash_tree(target),
                    requested_ref=parsed.requested_ref,
                    resolved_commit=actual,
                    resolution_method="fixture",
                )

            with mock.patch(
                "agent_skill_to_plugin.resolvers.claude_plugin.GitHubResolver.resolve",
                autospec=True,
                side_effect=fake_resolve,
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    ClaudePluginResolver._acquire_plugin(
                        plugin_source,
                        destination,
                        marketplace_snapshot=marketplace,
                        marketplace_manifest=manifest,
                        source_base=root,
                        timeout=30,
                    )

            self.assertEqual("resolution_integrity_failed", raised.exception.code)
            self.assertEqual(expected, raised.exception.details["expected"])
            self.assertEqual(actual, raised.exception.details["actual"])
            self.assertFalse(destination.exists())

    def test_ref_and_matching_sha_are_both_preserved(self) -> None:
        commit = "a" * 40
        plugin_source = _parse_plugin_source(
            {
                "name": "p",
                "source": {
                    "source": "git",
                    "url": "https://gitlab.com/owner/repo.git",
                    "ref": "release/v1",
                    "sha": commit,
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "snapshot"
            marketplace = root / "marketplace"
            (marketplace / ".claude-plugin").mkdir(parents=True)
            manifest = marketplace / ".claude-plugin/marketplace.json"
            manifest.write_text("{}", encoding="utf-8")

            def fake_resolve(_self, parsed, target):
                self.assertEqual("release/v1", parsed.requested_ref)
                target.mkdir(parents=True)
                return ResolvedSource(
                    kind="git_repository",
                    normalized_source=parsed.source or "",
                    snapshot_path=str(target),
                    snapshot_sha256=hash_tree(target),
                    requested_ref=parsed.requested_ref,
                    resolved_commit=commit,
                    resolution_method="fixture",
                )

            with mock.patch(
                "agent_skill_to_plugin.resolvers.claude_plugin.GitResolver.resolve",
                autospec=True,
                side_effect=fake_resolve,
            ):
                resolved, plugin_root = ClaudePluginResolver._acquire_plugin(
                    plugin_source,
                    destination,
                    marketplace_snapshot=marketplace,
                    marketplace_manifest=manifest,
                    source_base=root,
                    timeout=30,
                )

            self.assertEqual(commit, resolved.resolved_commit)
            self.assertEqual("release/v1", resolved.requested_ref)
            self.assertEqual(".", plugin_root)

    def test_git_source_sha_must_be_full_commit(self) -> None:
        with self.assertRaises(SkillToPluginError) as raised:
            _parse_plugin_source(
                {
                    "name": "p",
                    "source": {
                        "source": "github",
                        "repo": "owner/repo",
                        "ref": "main",
                        "sha": "abc1234",
                    },
                }
            )
        self.assertEqual("invalid_manifest", raised.exception.code)

    def test_source_models_cover_required_marketplace_variants(self) -> None:
        sha = "a" * 40
        cases = (
            ("relative", {"name": "p", "source": "./plugins/p"}, "relative", None, None),
            (
                "github",
                {"name": "p", "source": {"source": "github", "repo": "owner/repo", "sha": sha}},
                "github",
                sha,
                None,
            ),
            (
                "url-git",
                {"name": "p", "source": {"source": "url", "url": "https://github.com/owner/repo.git", "sha": sha}},
                "github",
                sha,
                None,
            ),
            (
                "git-subdir",
                {
                    "name": "p",
                    "source": {
                        "source": "git-subdir",
                        "url": "https://gitlab.com/owner/repo.git",
                        "path": "plugins/p",
                        "ref": "release/v1",
                        "sha": sha,
                    },
                },
                "git",
                "release/v1",
                "plugins/p",
            ),
            (
                "archive",
                {"name": "p", "source": {"source": "archive", "url": "https://downloads.example/p.tgz"}},
                "archive",
                None,
                None,
            ),
            (
                "npm",
                {"name": "p", "source": {"source": "npm", "package": "@scope/p", "version": "1.2.3"}},
                "npm",
                "1.2.3",
                None,
            ),
        )
        for label, entry, kind, ref, subdir in cases:
            with self.subTest(source=label):
                parsed = _parse_plugin_source(entry)
                self.assertEqual(kind, parsed.kind)
                self.assertEqual(ref, parsed.ref)
                self.assertEqual(subdir, parsed.subdir)

    def test_structured_sources_flow_from_manifest_to_acquisition(self) -> None:
        variants = (
            ("github", {"source": "github", "repo": "owner/repo"}, "github"),
            (
                "git-subdir",
                {"source": "git-subdir", "url": "https://gitlab.com/owner/repo.git", "path": "plugin"},
                "git",
            ),
            ("archive", {"source": "archive", "url": "https://downloads.example/plugin.zip"}, "archive"),
            ("npm", {"source": "npm", "package": "fixture-package"}, "npm"),
        )
        for label, source_value, expected_kind in variants:
            with self.subTest(source=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marketplace = root / "marketplace"
                (marketplace / ".claude-plugin").mkdir(parents=True)
                (marketplace / ".claude-plugin/marketplace.json").write_text(
                    json.dumps(
                        {
                            "name": "variant-marketplace",
                            "plugins": [{"name": "variant-plugin", "source": source_value}],
                        }
                    ),
                    encoding="utf-8",
                )
                seen = []

                def fake_acquire(plugin_source, destination, **kwargs):
                    seen.append(plugin_source)
                    (destination / ".claude-plugin").mkdir(parents=True)
                    (destination / ".claude-plugin/plugin.json").write_text(
                        '{"name":"variant-plugin"}', encoding="utf-8"
                    )
                    return (
                        ResolvedSource(
                            kind="fixture",
                            normalized_source="fixture",
                            snapshot_path=str(destination),
                            snapshot_sha256=hash_tree(destination),
                            resolution_method="fixture",
                        ),
                        ".",
                    )

                parsed = claude_input("variant-marketplace", "variant-plugin", str(marketplace))
                with mock.patch.object(ClaudePluginResolver, "_acquire_plugin", side_effect=fake_acquire):
                    result = ClaudePluginResolver().resolve(parsed, root / "snapshot", PROJECT_ROOT, 30)
                self.assertEqual(expected_kind, seen[0].kind)
                self.assertEqual(expected_kind, result.plugin_source.kind)

    def test_command_object_is_rejected_during_structured_parsing(self) -> None:
        with self.assertRaises(SkillToPluginError) as raised:
            _parse_plugin_source(
                {"name": "command", "source": {"source": "command", "command": "curl example"}}
            )
        self.assertEqual("security_rejected", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
