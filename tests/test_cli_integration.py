from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_skill_to_plugin.cli import main as cli_main
from agent_skill_to_plugin.errors import ExitCode, SkillToPluginError
from agent_skill_to_plugin.utils import hash_tree
from pluginize import main as legacy_main

from tests.helpers import copy_fixture


def invoke_json(main, arguments: list[str]) -> tuple[int, dict, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    value = stdout.getvalue().strip()
    if not value:
        raise AssertionError(f"CLI emitted no JSON; stderr={stderr.getvalue()!r}")
    return code, json.loads(value), stderr.getvalue()


def invoke_human(main, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class CliIntegrationTests(unittest.TestCase):
    def test_run_registers_standard_personal_marketplace_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = copy_fixture("single-skill-repo", root / "source")
            output = root / "output"
            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ):
                code, payload, stderr = invoke_json(
                    cli_main,
                    [
                        "run",
                        "--input",
                        str(source.resolve()),
                        "--output-root",
                        str(output),
                        "--json",
                    ],
                )

            self.assertEqual(int(ExitCode.OK), code)
            self.assertEqual("registered", payload["personal_marketplace"]["status"])
            self.assertFalse(payload["personal_marketplace"]["installation_performed"])
            self.assertFalse(payload["personal_marketplace"]["reinstall_required"])
            self.assertEqual(
                home / "plugins" / payload["plugin_name"],
                Path(payload["personal_marketplace"]["plugin_dir"]),
            )
            marketplace_file = home / ".agents" / "plugins" / "marketplace.json"
            marketplace = json.loads(marketplace_file.read_text(encoding="utf-8"))
            report = json.loads(Path(payload["report_json"]).read_text(encoding="utf-8"))
            report_markdown = Path(payload["report_markdown"]).read_text(encoding="utf-8")
            self.assertEqual("personal", marketplace["name"])
            self.assertEqual("AVAILABLE", marketplace["plugins"][0]["policy"]["installation"])
            self.assertEqual("ON_INSTALL", marketplace["plugins"][0]["policy"]["authentication"])
            self.assertEqual("registered", report["personal_marketplace"]["status"])
            self.assertFalse(report["personal_marketplace"]["installation_performed"])
            self.assertFalse(report["personal_marketplace"]["reinstall_required"])
            self.assertIn("Personal Marketplace registration", report_markdown)
            self.assertIn("Plugin installation performed: `false`", report_markdown)
            self.assertNotIn("ZIP", report_markdown)
            self.assertNotIn("marketplace_add_command", payload)
            self.assertNotIn("marketplace_add_command", report)
            self.assertNotIn("marketplace_add_command", report_markdown)
            self.assertEqual("", stderr)

    def test_durable_registration_cleanup_error_is_not_reported_as_uncommitted_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            output = root / "output"
            failure = SkillToPluginError(
                "Registration committed but cleanup needs review.",
                code="personal_marketplace_failed",
                details={
                    "registration_status": "succeeded_cleanup_incomplete",
                    "commit_durable": True,
                    "commit_verified": True,
                    "installation_performed": False,
                    "reinstall_required": False,
                    "lock_path": str(root / "retained-lock"),
                    "recovery": "Inspect the retained transaction journal.",
                },
            )

            with mock.patch(
                "agent_skill_to_plugin.application.register_personal_plugin",
                side_effect=failure,
            ):
                code, payload, stderr = invoke_json(
                    cli_main,
                    [
                        "run",
                        "--input",
                        str(source.resolve()),
                        "--output-root",
                        str(output),
                        "--json",
                    ],
                )

            self.assertEqual(int(ExitCode.PERSONAL_MARKETPLACE_FAILED), code)
            self.assertEqual("succeeded_cleanup_incomplete", payload["details"]["registration_status"])
            self.assertTrue(payload["details"]["commit_durable"])
            self.assertNotIn("registration_retry", payload["details"])
            generated_plugin = Path(payload["details"]["generated_plugin_dir"])
            report_json = generated_plugin.parent.parent / "reports" / f"{generated_plugin.name}.json"
            report_markdown = report_json.with_suffix(".md")
            report = json.loads(report_json.read_text(encoding="utf-8"))
            rendered = report_markdown.read_text(encoding="utf-8")
            self.assertEqual("succeeded_cleanup_incomplete", report["personal_marketplace"]["status"])
            self.assertTrue(report["personal_marketplace"]["commit_durable"])
            self.assertNotIn("registration_retry", report["personal_marketplace"])
            self.assertIn("may already contain the new Plugin state", rendered)
            self.assertEqual("", stderr)

    def test_incomplete_rollback_requires_recovery_review_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            output = root / "output"
            lock_path = root / "retained-lock"
            backup_path = root / "retained-backup"
            failure = SkillToPluginError(
                "Registration failed and rollback was incomplete.",
                code="personal_marketplace_failed",
                details={
                    "registration_status": "rollback_incomplete",
                    "commit_durable": False,
                    "lock_retained": True,
                    "lock_path": str(lock_path),
                    "backup_path": str(backup_path),
                    "recovery": "Inspect the retained transaction journal before retrying.",
                },
            )

            with mock.patch(
                "agent_skill_to_plugin.application.register_personal_plugin",
                side_effect=failure,
            ):
                code, payload, stderr = invoke_json(
                    cli_main,
                    [
                        "run",
                        "--input",
                        str(source.resolve()),
                        "--output-root",
                        str(output),
                        "--json",
                    ],
                )

            self.assertEqual(int(ExitCode.PERSONAL_MARKETPLACE_FAILED), code)
            self.assertEqual("rollback_incomplete", payload["details"]["registration_status"])
            self.assertNotIn("registration_retry", payload["details"])
            generated_plugin = Path(payload["details"]["generated_plugin_dir"])
            report_json = generated_plugin.parent.parent / "reports" / f"{generated_plugin.name}.json"
            report_markdown = report_json.with_suffix(".md")
            report = json.loads(report_json.read_text(encoding="utf-8"))
            rendered = report_markdown.read_text(encoding="utf-8")
            registration = report["personal_marketplace"]
            self.assertEqual("failed", registration["status"])
            self.assertEqual("rollback_incomplete", registration["failure_state"])
            self.assertEqual(str(lock_path), registration["lock_path"])
            self.assertEqual(str(backup_path), registration["backup_path"])
            self.assertNotIn("registration_retry", registration)
            self.assertIn("retained recovery state requires inspection", rendered)
            self.assertNotIn("Retry: `agent-skill-to-plugin", rendered)
            self.assertEqual("", stderr)

    def test_failed_registration_is_reported_and_exact_plugin_dir_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = copy_fixture("single-skill-repo", root / "source")
            output = root / "output"
            run_arguments = [
                "run",
                "--input",
                str(source.resolve()),
                "--output-root",
                str(output),
                "--json",
            ]
            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ):
                initial_code, initial, initial_stderr = invoke_json(cli_main, run_arguments)

            self.assertEqual(int(ExitCode.OK), initial_code)
            self.assertEqual("", initial_stderr)
            generated_plugin = Path(initial["plugin_dir"])
            generated_zip = Path(initial["zip_path"])
            personal_plugin = Path(initial["personal_marketplace"]["plugin_dir"])
            divergent_marker = personal_plugin / "divergent-personal-state.txt"
            divergent_marker.write_text("keep unless separately authorized\n", encoding="utf-8")
            personal_hash_before = hash_tree(personal_plugin)
            marketplace_file = Path(initial["personal_marketplace"]["marketplace_file"])
            marketplace_before = marketplace_file.read_bytes()

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ):
                failed_code, failed, failed_stderr = invoke_json(
                    cli_main,
                    [*run_arguments[:-1], "--force", "--json"],
                )

            self.assertEqual(int(ExitCode.OUTPUT_CONFLICT), failed_code)
            self.assertEqual("error", failed["status"])
            self.assertEqual("output_conflict", failed["error_code"])
            self.assertEqual("", failed_stderr)
            retry = failed["details"]["registration_retry"]
            self.assertEqual(
                {
                    "subcommand": "register-personal",
                    "plugin_dir": str(generated_plugin),
                    "force_personal_required": True,
                },
                retry,
            )
            self.assertEqual(personal_hash_before, hash_tree(personal_plugin))
            self.assertEqual(marketplace_before, marketplace_file.read_bytes())

            failed_report = json.loads(Path(initial["report_json"]).read_text(encoding="utf-8"))
            failed_markdown = Path(initial["report_markdown"]).read_text(encoding="utf-8")
            self.assertEqual("failed", failed_report["personal_marketplace"]["status"])
            self.assertEqual("output_conflict", failed_report["personal_marketplace"]["error_code"])
            self.assertIn("explicit `register-personal` retry", failed_markdown)
            self.assertNotIn("marketplace_add_command", failed_report)
            generated_hash_before_retry = hash_tree(generated_plugin)
            generated_zip_mtime_before_retry = generated_zip.stat().st_mtime_ns

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ), mock.patch(
                "agent_skill_to_plugin.application.package_selected",
                side_effect=AssertionError("register-personal must not package again"),
            ):
                retry_code, retried, retry_stderr = invoke_json(
                    cli_main,
                    [
                        "register-personal",
                        "--plugin-dir",
                        retry["plugin_dir"],
                        "--force-personal",
                        "--json",
                    ],
                )

            self.assertEqual(int(ExitCode.OK), retry_code)
            self.assertEqual("ok", retried["status"])
            self.assertEqual("updated", retried["personal_marketplace"]["status"])
            self.assertFalse(retried["personal_marketplace"]["installation_performed"])
            self.assertTrue(retried["personal_marketplace"]["reinstall_required"])
            self.assertEqual("", retry_stderr)
            self.assertEqual(generated_hash_before_retry, hash_tree(generated_plugin))
            self.assertEqual(generated_zip_mtime_before_retry, generated_zip.stat().st_mtime_ns)
            self.assertEqual(generated_hash_before_retry, hash_tree(personal_plugin))
            self.assertFalse(divergent_marker.exists())
            retried_report = json.loads(Path(initial["report_json"]).read_text(encoding="utf-8"))
            self.assertEqual("updated", retried_report["personal_marketplace"]["status"])
            self.assertFalse(retried_report["personal_marketplace"]["installation_performed"])
            self.assertTrue(retried_report["personal_marketplace"]["reinstall_required"])
            self.assertNotIn("marketplace_add_command", retried)
            self.assertNotIn("marketplace_add_command", retried_report)

    def test_human_output_only_presents_zip_when_explicitly_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            code, output, stderr = invoke_human(
                cli_main,
                [
                    "run",
                    "--input",
                    str(source.resolve()),
                    "--output-root",
                    str(root / "hidden"),
                    "--no-register-personal",
                ],
            )
            shown_code, shown, shown_stderr = invoke_human(
                cli_main,
                [
                    "run",
                    "--input",
                    str(source.resolve()),
                    "--output-root",
                    str(root / "shown"),
                    "--no-register-personal",
                    "--show-zip",
                ],
            )

            self.assertEqual(int(ExitCode.OK), code)
            self.assertNotIn("ZIP:", output)
            self.assertNotIn("ZIP SHA-256:", output)
            self.assertEqual("", stderr)
            self.assertEqual(int(ExitCode.OK), shown_code)
            self.assertIn("ZIP:", shown)
            self.assertIn("ZIP SHA-256:", shown)
            self.assertEqual("", shown_stderr)

    def test_run_from_input_file_creates_local_plugin_and_reports_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            request = root / "input.txt"
            request.write_text(str(source.resolve()), encoding="utf-8")
            output = root / "marketplace"
            code, payload, stderr = invoke_json(
                cli_main,
                [
                    "run",
                    "--input-file",
                    str(request),
                    "--output-root",
                    str(output),
                    "--no-register-personal",
                    "--json",
                ],
            )
            self.assertEqual(int(ExitCode.OK), code)
            self.assertEqual("ok", payload["status"])
            self.assertIsNone(payload["error_code"])
            self.assertEqual("example", payload["skills"][0]["name"])
            self.assertTrue(Path(payload["plugin_dir"]).is_dir())
            self.assertTrue(Path(payload["zip_path"]).is_file())
            self.assertTrue(Path(payload["report_json"]).is_file())
            self.assertEqual("", stderr)

    def test_resolve_then_convert_reuses_saved_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("multi-skill-repo", root / "source")
            output = root / "marketplace"
            resolve_code, resolved, resolve_stderr = invoke_json(
                cli_main,
                [
                    "resolve",
                    "--input",
                    str(source.resolve()),
                    "--output-root",
                    str(output),
                    "--json",
                ],
            )
            self.assertEqual(int(ExitCode.NEEDS_SELECTION), resolve_code)
            self.assertEqual("needs_selection", resolved["status"])
            self.assertEqual(2, len(resolved["candidates"]))
            resolution_file = Path(resolved["resolution_file"])
            snapshot = Path(resolved["resolved_source"]["snapshot_path"])
            self.assertTrue(resolution_file.is_file())
            self.assertTrue(snapshot.is_dir())
            self.assertEqual("", resolve_stderr)

            candidate_id = resolved["candidates"][0]["id"]
            convert_code, converted, convert_stderr = invoke_json(
                cli_main,
                [
                    "convert",
                    "--resolution",
                    str(resolution_file),
                    "--select",
                    candidate_id,
                    "--no-register-personal",
                    "--json",
                ],
            )
            self.assertEqual(int(ExitCode.OK), convert_code)
            self.assertEqual("ok", converted["status"])
            self.assertEqual(candidate_id, converted["skills"][0]["candidate_id"])
            self.assertEqual(snapshot, Path(resolved["resolved_source"]["snapshot_path"]))
            self.assertTrue(snapshot.is_dir())
            self.assertEqual("", convert_stderr)

    def test_cli_error_json_has_stable_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, payload, stderr = invoke_json(
                cli_main,
                [
                    "run",
                    "--input",
                    "ordinary prose without a source",
                    "--output-root",
                    str(Path(temporary) / "out"),
                    "--json",
                ],
            )
        self.assertEqual(int(ExitCode.UNKNOWN_INPUT_FORMAT), code)
        self.assertEqual("error", payload["status"])
        self.assertEqual("unknown_input_format", payload["error_code"])
        self.assertEqual("", stderr)

    def test_run_rejects_output_root_nested_in_local_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = copy_fixture("single-skill-repo", root / "source")
            output = source / "converted-marketplace"
            code, payload, stderr = invoke_json(
                cli_main,
                [
                    "run",
                    "--input",
                    str(source.resolve()),
                    "--output-root",
                    str(output),
                    "--no-register-personal",
                    "--json",
                ],
            )

            self.assertEqual(int(ExitCode.OUTPUT_CONFLICT), code)
            self.assertEqual("error", payload["status"])
            self.assertEqual("output_conflict", payload["error_code"])
            self.assertEqual(
                "destination_within_source",
                payload["details"]["relationship"],
            )
            self.assertFalse((output / "plugins").exists())
            self.assertFalse((output / "packages").exists())
            self.assertEqual([], list((output / "resolutions").iterdir()))
            self.assertEqual("", stderr)


class LegacyCliIntegrationTests(unittest.TestCase):
    def test_force_does_not_authorize_personal_overwrite_but_force_personal_does(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            staged = copy_fixture("single-skill-repo", root / "staged")
            output = root / "legacy-marketplace"
            arguments = [
                "--command",
                "npx skills add owner/repo --skill example",
                "--staged-skills-dir",
                str(staged),
                "--output-root",
                str(output),
                "--json",
            ]
            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ):
                initial_code, initial, initial_stderr = invoke_json(legacy_main, arguments)

            self.assertEqual(0, initial_code)
            self.assertEqual("", initial_stderr)
            personal_plugin = Path(initial["personal_marketplace"]["plugin_dir"])
            divergent_marker = personal_plugin / "divergent-personal-state.txt"
            divergent_marker.write_text("legacy --force must preserve this\n", encoding="utf-8")
            personal_hash_before = hash_tree(personal_plugin)
            marketplace_file = Path(initial["personal_marketplace"]["marketplace_file"])
            marketplace_before = marketplace_file.read_bytes()

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ):
                failed_code, failed, failed_stderr = invoke_json(
                    legacy_main,
                    [*arguments[:-1], "--force", "--json"],
                )

            self.assertEqual(2, failed_code)
            self.assertEqual("output_conflict", failed["error_code"])
            self.assertTrue(failed["details"]["registration_retry"]["force_personal_required"])
            self.assertEqual(personal_hash_before, hash_tree(personal_plugin))
            self.assertEqual(marketplace_before, marketplace_file.read_bytes())
            self.assertEqual("", failed_stderr)

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.Path.home",
                return_value=home,
            ):
                forced_code, forced, forced_stderr = invoke_json(
                    legacy_main,
                    [*arguments[:-1], "--force", "--force-personal", "--json"],
                )

            self.assertEqual(0, forced_code)
            self.assertEqual("ok", forced["status"])
            self.assertEqual("updated", forced["personal_marketplace"]["status"])
            self.assertFalse(divergent_marker.exists())
            self.assertEqual(hash_tree(Path(forced["plugin_dir"])), hash_tree(personal_plugin))
            self.assertEqual("", forced_stderr)

    def test_legacy_wrapper_staged_mode_uses_common_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = copy_fixture("single-skill-repo", root / "staged")
            output = root / "legacy-marketplace"
            code, payload, stderr = invoke_json(
                legacy_main,
                [
                    "--command",
                    "npx skills add owner/repo --skill example",
                    "--staged-skills-dir",
                    str(staged),
                    "--output-root",
                    str(output),
                    "--no-register-personal",
                    "--json",
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual("ok", payload["status"])
            self.assertEqual("example", payload["skills"][0]["name"])
            self.assertTrue(Path(payload["zip_path"]).is_file())
            self.assertEqual("", stderr)

    def test_legacy_wrapper_preserves_exit_two_and_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, payload, stderr = invoke_json(
                legacy_main,
                [
                    "--command",
                    "npx skills add owner/repo && whoami",
                    "--output-root",
                    str(Path(temporary) / "out"),
                    "--json",
                ],
            )
        self.assertEqual(2, code)
        self.assertEqual("error", payload["status"])
        self.assertEqual("security_rejected", payload["error_code"])
        self.assertEqual("", stderr)


if __name__ == "__main__":
    unittest.main()
