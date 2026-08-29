from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_skill_to_plugin import packaging
from agent_skill_to_plugin.discovery import discover_skills
from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.models import ParsedInput, ResolutionState
from agent_skill_to_plugin.provenance import build_provenance, detect_licenses
from agent_skill_to_plugin.selection import select_candidates, validate_selected_references

from tests.helpers import copy_fixture, resolved_source


def prepared_conversion(output_root: Path, label: str):
    resolutions = output_root / "resolutions"
    resolutions.mkdir(parents=True, exist_ok=True)
    snapshot = copy_fixture("single-skill-repo", resolutions / f"{label}.snapshot")
    resolved = resolved_source(snapshot)
    candidates = discover_skills(resolved)
    decision = validate_selected_references(snapshot, select_candidates(candidates))
    parsed = ParsedInput(
        kind="local",
        raw_input=f"fixture:{label}",
        normalized_input=f"fixture:{label}",
        source=f"fixture:{label}",
    )
    state = ResolutionState(
        resolution_id=f"resolution-{label}",
        status="resolved",
        created_at="2026-08-29T00:00:00+00:00",
        input=parsed,
        resolved_source=resolved,
        candidates=candidates,
        selection_policy="structural",
        resolution_file=str((resolutions / f"{label}.json").resolve()),
        output_root=str(output_root.resolve()),
    )
    licenses = detect_licenses(snapshot, candidates=decision.selected, resolved_source=resolved)
    provenance = build_provenance(
        parsed,
        resolved,
        candidates=decision.selected,
        acquired_at=state.created_at,
        license_findings=licenses,
    )
    return state, decision, provenance, licenses


def run_prepared(prepared, output_root: Path, plugin_name: str):
    state, decision, provenance, licenses = prepared
    return packaging.package_selected(
        state,
        decision.selected,
        output_root=output_root,
        provenance=provenance,
        license_findings=licenses,
        external_references=decision.external_references,
        requested_plugin_name=plugin_name,
    )


class PackagingLockTests(unittest.TestCase):
    def test_marketplace_read_and_artifact_commit_share_one_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "market"
            prepared = prepared_conversion(output_root, "scope")
            observed: list[str] = []
            real_read = packaging._read_marketplace
            real_commit = packaging._commit_artifacts

            def checked_read(path: Path):
                self.assertTrue((output_root / packaging.OUTPUT_LOCK_NAME).is_dir())
                observed.append("marketplace-read")
                return real_read(path)

            def checked_commit(*args, **kwargs):
                self.assertTrue((output_root / packaging.OUTPUT_LOCK_NAME).is_dir())
                observed.append("artifact-commit")
                return real_commit(*args, **kwargs)

            with mock.patch.object(packaging, "_read_marketplace", side_effect=checked_read), mock.patch.object(
                packaging,
                "_commit_artifacts",
                side_effect=checked_commit,
            ):
                result = run_prepared(prepared, output_root, "scope-plugin")

            self.assertEqual(["marketplace-read", "artifact-commit"], observed)
            self.assertTrue(Path(result.plugin_dir).is_dir())
            self.assertFalse((output_root / packaging.OUTPUT_LOCK_NAME).exists())

    def test_existing_crash_lock_stops_before_marketplace_or_artifact_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "market"
            prepared = prepared_conversion(output_root, "blocked")
            marketplace = output_root / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            original = b'{"name":"existing","plugins":[]}\n'
            marketplace.write_bytes(original)
            lock_dir = output_root / packaging.OUTPUT_LOCK_NAME
            lock_dir.mkdir()
            (lock_dir / packaging.LOCK_OWNER_FILE).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": 4242,
                        "hostname": "crashed-host",
                        "started_at": "2026-08-29T00:00:00+00:00",
                        "tool_version": "fixture",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SkillToPluginError) as raised:
                run_prepared(prepared, output_root, "blocked-plugin")

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual("active_or_crashed", raised.exception.details["lock_status"])
            self.assertEqual(4242, raised.exception.details["owner"]["pid"])
            self.assertEqual("crashed-host", raised.exception.details["owner"]["hostname"])
            self.assertEqual(original, marketplace.read_bytes())
            self.assertTrue(lock_dir.is_dir())
            self.assertFalse((output_root / "plugins").exists())
            self.assertFalse((output_root / "packages").exists())
            self.assertFalse((output_root / "reports").exists())

    def test_failure_releases_owned_lock_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "market"
            prepared = prepared_conversion(output_root, "failure")
            with mock.patch.object(
                packaging,
                "write_deterministic_zip",
                side_effect=SkillToPluginError("fixture failure", code="package_validation_failed"),
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    run_prepared(prepared, output_root, "failure-plugin")

            self.assertEqual("package_validation_failed", raised.exception.code)
            self.assertFalse((output_root / packaging.OUTPUT_LOCK_NAME).exists())
            self.assertEqual([], list(output_root.glob(".asp-*")))

    def test_concurrent_same_name_conversion_fails_closed_without_lost_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "market"
            first = prepared_conversion(output_root, "first")
            second = prepared_conversion(output_root, "second")
            entered_zip = threading.Event()
            continue_zip = threading.Event()
            results: list[object] = []
            failures: list[BaseException] = []
            real_write_zip = packaging.write_deterministic_zip

            def blocking_write_zip(plugin_dir: Path, zip_path: Path) -> None:
                entered_zip.set()
                if not continue_zip.wait(timeout=10):
                    raise AssertionError("test did not release the packaging barrier")
                real_write_zip(plugin_dir, zip_path)

            def worker() -> None:
                try:
                    results.append(run_prepared(first, output_root, "shared-plugin"))
                except BaseException as exc:  # captured for assertion in the test thread
                    failures.append(exc)

            with mock.patch.object(packaging, "write_deterministic_zip", side_effect=blocking_write_zip):
                thread = threading.Thread(target=worker, name="first-packager")
                thread.start()
                try:
                    self.assertTrue(entered_zip.wait(timeout=10))
                    lock_dir = output_root / packaging.OUTPUT_LOCK_NAME
                    owner = json.loads(
                        (lock_dir / packaging.LOCK_OWNER_FILE).read_text(encoding="utf-8")
                    )
                    self.assertIsInstance(owner.get("token"), str)
                    self.assertTrue(owner["token"])
                    with self.assertRaises(SkillToPluginError) as raised:
                        run_prepared(second, output_root, "shared-plugin")
                    self.assertEqual("output_conflict", raised.exception.code)
                    self.assertEqual(
                        "active_or_crashed",
                        raised.exception.details["lock_status"],
                    )
                finally:
                    continue_zip.set()
                    thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertEqual([], failures)
            self.assertEqual(1, len(results))
            self.assertFalse((output_root / packaging.OUTPUT_LOCK_NAME).exists())
            marketplace = json.loads(
                (output_root / ".agents" / "plugins" / "marketplace.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                ["shared-plugin"],
                [item["name"] for item in marketplace["plugins"]],
            )
            self.assertTrue((output_root / "plugins" / "shared-plugin").is_dir())
            self.assertTrue((output_root / "packages" / "shared-plugin.zip").is_file())
            self.assertEqual([], list((output_root / "plugins").glob("shared-plugin-*")))


if __name__ == "__main__":
    unittest.main()
