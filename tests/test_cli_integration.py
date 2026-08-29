from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_skill_to_plugin.cli import main as cli_main
from agent_skill_to_plugin.errors import ExitCode
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


class CliIntegrationTests(unittest.TestCase):
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
