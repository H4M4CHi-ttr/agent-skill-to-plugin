from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from agent_skill_to_plugin import personal_marketplace
from agent_skill_to_plugin.errors import SkillToPluginError
from agent_skill_to_plugin.personal_marketplace import register_personal_plugin
from agent_skill_to_plugin.utils import atomic_write_text as real_atomic_write_text, hash_tree


def make_plugin(root: Path, name: str = "fixture-plugin", marker: str = "new") -> Path:
    plugin = root / name
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "fixture").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "skills": "./skills/"}),
        encoding="utf-8",
    )
    (plugin / "skills" / "fixture" / "SKILL.md").write_text(
        f"---\nname: fixture\ndescription: Fixture\n---\n\n{marker}\n",
        encoding="utf-8",
    )
    return plugin


def expected_entry(name: str) -> dict:
    return {
        "name": name,
        "source": {"source": "local", "path": f"./plugins/{name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }


def write_marketplace(home: Path, value: dict) -> Path:
    path = home / ".agents" / "plugins" / "marketplace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class PersonalMarketplaceTests(unittest.TestCase):
    def test_new_registration_uses_standard_personal_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")

            result = register_personal_plugin(source, home=home)

            target = home / "plugins" / source.name
            marketplace_file = home / ".agents" / "plugins" / "marketplace.json"
            marketplace = json.loads(marketplace_file.read_text(encoding="utf-8"))
            self.assertEqual("registered", result.status)
            self.assertEqual("personal", result.marketplace_name)
            self.assertEqual(str(target), result.plugin_dir)
            self.assertEqual(str(marketplace_file), result.marketplace_file)
            self.assertEqual("AVAILABLE", result.policy_installation)
            self.assertEqual("ON_INSTALL", result.policy_authentication)
            self.assertEqual("Productivity", result.category)
            self.assertFalse(result.reinstall_required)
            self.assertFalse(result.installation_performed)
            self.assertEqual(hash_tree(source), hash_tree(target))
            self.assertEqual(
                {
                    "name": "personal",
                    "interface": {"displayName": "Personal"},
                    "plugins": [expected_entry(source.name)],
                },
                marketplace,
            )
            self.assertTrue(result.view_url.startswith(f"codex://plugins/{source.name}?marketplacePath="))
            self.assertEqual(result.view_url + "&mode=share", result.share_url)
            self.assertFalse((home / ".agents" / "plugins" / ".agent-skill-to-plugin.lock").exists())

    def test_exact_registration_is_idempotent_without_rewriting_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            first = register_personal_plugin(source, home=home)
            marketplace_file = Path(first.marketplace_file)
            original = marketplace_file.read_bytes()
            marketplace_writes: list[Path] = []

            def track_marketplace_write(path: Path, value: str) -> None:
                if path == marketplace_file:
                    marketplace_writes.append(path)
                real_atomic_write_text(path, value)

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.atomic_write_text",
                side_effect=track_marketplace_write,
            ):
                second = register_personal_plugin(source, home=home)

            self.assertEqual("already_registered", second.status)
            self.assertEqual(original, marketplace_file.read_bytes())
            self.assertEqual([], marketplace_writes)

    def test_matching_plugin_without_entry_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            target = home / "plugins" / source.name
            target.parent.mkdir()
            make_plugin(target.parent, source.name)
            write_marketplace(
                home,
                {
                    "name": "my_personal",
                    "interface": {"displayName": "Mine", "extra": True},
                    "plugins": [{"name": "other", "source": "./plugins/other"}],
                    "custom": {"preserve": True},
                },
            )

            result = register_personal_plugin(source, home=home)

            marketplace = json.loads(Path(result.marketplace_file).read_text(encoding="utf-8"))
            self.assertEqual("registered", result.status)
            self.assertFalse(result.reinstall_required)
            self.assertFalse(result.installation_performed)
            self.assertEqual("my_personal", result.marketplace_name)
            self.assertEqual({"displayName": "Mine", "extra": True}, marketplace["interface"])
            self.assertEqual({"preserve": True}, marketplace["custom"])
            self.assertEqual(["other", source.name], [item["name"] for item in marketplace["plugins"]])

    def test_exact_entry_without_plugin_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            marketplace_file = write_marketplace(
                home,
                {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": [expected_entry(source.name)]},
            )
            original = marketplace_file.read_bytes()
            marketplace_writes: list[Path] = []

            def track_marketplace_write(path: Path, value: str) -> None:
                if path == marketplace_file:
                    marketplace_writes.append(path)
                real_atomic_write_text(path, value)

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.atomic_write_text",
                side_effect=track_marketplace_write,
            ):
                result = register_personal_plugin(source, home=home)

            self.assertEqual("registered", result.status)
            self.assertFalse(result.reinstall_required)
            self.assertFalse(result.installation_performed)
            self.assertEqual(hash_tree(source), hash_tree(Path(result.plugin_dir)))
            self.assertEqual(original, marketplace_file.read_bytes())
            self.assertEqual([], marketplace_writes)

    def test_different_same_name_state_requires_force_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source", marker="new")
            target = make_plugin(home / "plugins", source.name, marker="old")
            marketplace_file = write_marketplace(
                home,
                {
                    "name": "personal",
                    "plugins": [{"name": source.name, "source": "./somewhere-else"}],
                },
            )
            target_hash = hash_tree(target)
            original = marketplace_file.read_bytes()

            with self.assertRaises(SkillToPluginError) as raised:
                register_personal_plugin(source, home=home)

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual(target_hash, hash_tree(target))
            self.assertEqual(original, marketplace_file.read_bytes())

    def test_force_replaces_target_and_entry_while_preserving_order_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source", marker="new")
            make_plugin(home / "plugins", source.name, marker="old")
            marketplace_file = write_marketplace(
                home,
                {
                    "name": "mine",
                    "interface": {"displayName": "My Marketplace", "theme": "blue"},
                    "custom": [1, 2, 3],
                    "plugins": [
                        {"name": "before", "source": "./plugins/before"},
                        {"name": source.name, "source": "./old"},
                        {"name": "after", "source": "./plugins/after"},
                    ],
                },
            )

            result = register_personal_plugin(source, home=home, force=True)

            marketplace = json.loads(marketplace_file.read_text(encoding="utf-8"))
            self.assertEqual("updated", result.status)
            self.assertTrue(result.reinstall_required)
            self.assertFalse(result.installation_performed)
            self.assertEqual(hash_tree(source), hash_tree(Path(result.plugin_dir)))
            self.assertEqual(["before", source.name, "after"], [item["name"] for item in marketplace["plugins"]])
            self.assertEqual(expected_entry(source.name), marketplace["plugins"][1])
            self.assertEqual({"displayName": "My Marketplace", "theme": "blue"}, marketplace["interface"])
            self.assertEqual([1, 2, 3], marketplace["custom"])

    def test_duplicate_json_keys_are_rejected_before_plugin_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            marketplace_file = home / ".agents" / "plugins" / "marketplace.json"
            marketplace_file.parent.mkdir(parents=True)
            marketplace_file.write_text(
                '{"name":"personal","name":"personal","plugins":[]}',
                encoding="utf-8",
            )

            with self.assertRaises(SkillToPluginError) as raised:
                register_personal_plugin(source, home=home)

            self.assertEqual("personal_marketplace_failed", raised.exception.code)
            self.assertEqual(35, raised.exception.exit_code)
            self.assertFalse((home / "plugins" / source.name).exists())

    def test_atomic_manifest_failure_rolls_back_replaced_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source", marker="new")
            target = make_plugin(home / "plugins", source.name, marker="old")
            old_hash = hash_tree(target)
            marketplace_file = write_marketplace(
                home,
                {"name": "personal", "plugins": [{"name": source.name, "source": "./old"}]},
            )
            original = marketplace_file.read_bytes()

            def fail_marketplace_write(path: Path, value: str) -> None:
                if path == marketplace_file:
                    raise OSError("fixture write failure")
                real_atomic_write_text(path, value)

            with mock.patch(
                "agent_skill_to_plugin.personal_marketplace.atomic_write_text",
                side_effect=fail_marketplace_write,
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    register_personal_plugin(source, home=home, force=True)

            self.assertEqual("personal_marketplace_failed", raised.exception.code)
            self.assertEqual(old_hash, hash_tree(target))
            self.assertEqual(original, marketplace_file.read_bytes())
            self.assertEqual([], list((home / "plugins").glob(".*.agent-skill-to-plugin.*")))
            self.assertFalse((home / ".agents" / "plugins" / personal_marketplace.PERSONAL_MARKETPLACE_LOCK).exists())

    def test_existing_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            lock = home / ".agents" / "plugins" / ".agent-skill-to-plugin.lock"
            lock.mkdir(parents=True)

            with self.assertRaises(SkillToPluginError) as raised:
                register_personal_plugin(source, home=home)

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual("active_or_crashed", raised.exception.details["lock_status"])
            self.assertEqual("missing", raised.exception.details["owner_metadata_status"])
            self.assertEqual("missing", raised.exception.details["transaction_metadata_status"])
            self.assertIn("manually remove", raised.exception.details["recovery"])
            self.assertTrue(lock.is_dir())
            self.assertFalse((home / "plugins" / source.name).exists())

    def test_crash_journal_is_reported_with_actionable_paths_and_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            lock = home / ".agents" / "plugins" / personal_marketplace.PERSONAL_MARKETPLACE_LOCK
            lock.mkdir(parents=True)
            token = "fixture-owner-token"
            owner = {
                "schema_version": 1,
                "token": token,
                "pid": 4242,
                "hostname": "crashed-host",
                "started_at": "2026-08-30T00:00:00+00:00",
                "tool_version": "fixture",
            }
            backup = home / "plugins" / ".fixture-plugin.backup"
            journal = {
                "schema_version": 1,
                "token": token,
                "state": "plugin_installed",
                "plugin_name": source.name,
                "plugin_target": str(home / "plugins" / source.name),
                "backup_path": str(backup),
                "stage_path": str(home / "plugins" / ".fixture-plugin.stage"),
                "marketplace_file": str(home / ".agents" / "plugins" / "marketplace.json"),
                "force": True,
                "updated_at": "2026-08-30T00:00:01+00:00",
            }
            (lock / personal_marketplace.LOCK_OWNER_FILE).write_text(json.dumps(owner), encoding="utf-8")
            (lock / personal_marketplace.LOCK_JOURNAL_FILE).write_text(json.dumps(journal), encoding="utf-8")

            with self.assertRaises(SkillToPluginError) as raised:
                register_personal_plugin(source, home=home)

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual(4242, raised.exception.details["owner"]["pid"])
            self.assertEqual("crashed-host", raised.exception.details["owner"]["hostname"])
            self.assertEqual("plugin_installed", raised.exception.details["transaction"]["state"])
            self.assertEqual(str(backup), raised.exception.details["transaction"]["backup_path"])
            self.assertIn("plugin target, backup", raised.exception.details["recovery"])
            self.assertTrue((lock / personal_marketplace.LOCK_OWNER_FILE).is_file())
            self.assertTrue((lock / personal_marketplace.LOCK_JOURNAL_FILE).is_file())

    def test_incomplete_rollback_retains_backup_journal_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source", marker="new")
            target = make_plugin(home / "plugins", source.name, marker="old")
            old_hash = hash_tree(target)
            marketplace_file = write_marketplace(
                home,
                {"name": "personal", "plugins": [{"name": source.name, "source": "./old"}]},
            )
            original_marketplace = marketplace_file.read_bytes()
            real_rmtree = shutil.rmtree

            def fail_marketplace_write(path: Path, value: str) -> None:
                if path == marketplace_file:
                    raise OSError("fixture marketplace failure")
                real_atomic_write_text(path, value)

            def block_new_target_removal(path: Path, *args, **kwargs) -> None:
                if Path(path) == target:
                    raise OSError("fixture rollback failure")
                real_rmtree(path, *args, **kwargs)

            with mock.patch.object(personal_marketplace, "atomic_write_text", side_effect=fail_marketplace_write), mock.patch.object(
                personal_marketplace.shutil,
                "rmtree",
                side_effect=block_new_target_removal,
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    register_personal_plugin(source, home=home, force=True)

            self.assertEqual("rollback_incomplete", raised.exception.details["registration_status"])
            self.assertTrue(raised.exception.details["lock_retained"])
            lock = Path(raised.exception.details["lock_path"])
            journal_file = lock / personal_marketplace.LOCK_JOURNAL_FILE
            journal = json.loads(journal_file.read_text(encoding="utf-8"))
            backup = Path(journal["backup_path"])
            self.assertEqual("rollback_incomplete", journal["state"])
            self.assertTrue(backup.is_dir())
            self.assertEqual(old_hash, hash_tree(backup))
            self.assertEqual(hash_tree(source), hash_tree(target))
            self.assertEqual(original_marketplace, marketplace_file.read_bytes())

            with self.assertRaises(SkillToPluginError) as retry:
                register_personal_plugin(source, home=home, force=True)
            self.assertEqual("output_conflict", retry.exception.code)
            self.assertEqual("rollback_incomplete", retry.exception.details["transaction"]["state"])
            self.assertEqual(str(backup), retry.exception.details["transaction"]["backup_path"])

    def test_final_readback_mismatch_retains_force_backup_and_reports_committed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source", marker="new")
            target = make_plugin(home / "plugins", source.name, marker="old")
            old_hash = hash_tree(target)
            marketplace_file = write_marketplace(
                home,
                {"name": "personal", "plugins": [{"name": source.name, "source": "./old"}]},
            )

            def corrupt_marketplace_write(path: Path, value: str) -> None:
                if path == marketplace_file:
                    payload = json.loads(value)
                    payload["plugins"][0]["source"]["path"] = "./plugins/wrong"
                    real_atomic_write_text(path, json.dumps(payload) + "\n")
                    return
                real_atomic_write_text(path, value)

            with mock.patch.object(
                personal_marketplace,
                "atomic_write_text",
                side_effect=corrupt_marketplace_write,
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    register_personal_plugin(source, home=home, force=True)

            self.assertEqual("committed_verification_failed", raised.exception.details["registration_status"])
            self.assertTrue(raised.exception.details["commit_durable"])
            self.assertFalse(raised.exception.details["commit_verified"])
            self.assertTrue(raised.exception.details["reinstall_required"])
            self.assertFalse(raised.exception.details["installation_performed"])
            lock = Path(raised.exception.details["lock_path"])
            journal = json.loads((lock / personal_marketplace.LOCK_JOURNAL_FILE).read_text(encoding="utf-8"))
            backup = Path(journal["backup_path"])
            self.assertEqual("verification_incomplete", journal["state"])
            self.assertEqual(old_hash, hash_tree(backup))
            self.assertEqual(hash_tree(source), hash_tree(target))

    def test_nonregular_owner_metadata_is_reported_without_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            lock = home / ".agents" / "plugins" / personal_marketplace.PERSONAL_MARKETPLACE_LOCK
            owner = lock / personal_marketplace.LOCK_OWNER_FILE
            owner.mkdir(parents=True)

            with self.assertRaises(SkillToPluginError) as raised:
                register_personal_plugin(source, home=home)

            self.assertEqual("output_conflict", raised.exception.code)
            self.assertEqual("not_regular", raised.exception.details["owner_metadata_status"])
            self.assertTrue(owner.is_dir())

    def test_owner_token_change_prevents_lock_release_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            real_clear_journal = personal_marketplace._PersonalMarketplaceLock.clear_journal

            def tamper_then_clear(lock) -> None:
                owner = json.loads(lock.owner_file.read_text(encoding="utf-8"))
                owner["token"] = "different-owner"
                real_atomic_write_text(lock.owner_file, json.dumps(owner) + "\n")
                real_clear_journal(lock)

            with mock.patch.object(
                personal_marketplace._PersonalMarketplaceLock,
                "clear_journal",
                new=tamper_then_clear,
            ):
                with self.assertRaises(SkillToPluginError) as raised:
                    register_personal_plugin(source, home=home)

            self.assertEqual("succeeded_journal_incomplete", raised.exception.details["registration_status"])
            self.assertTrue(raised.exception.details["lock_retained"])
            lock = Path(raised.exception.details["lock_path"])
            self.assertTrue(lock.is_dir())
            self.assertEqual("different-owner", json.loads((lock / personal_marketplace.LOCK_OWNER_FILE).read_text(encoding="utf-8"))["token"])
            self.assertTrue((lock / personal_marketplace.LOCK_JOURNAL_FILE).is_file())
            self.assertEqual(hash_tree(source), hash_tree(home / "plugins" / source.name))

            with self.assertRaises(SkillToPluginError) as retry:
                register_personal_plugin(source, home=home)
            self.assertEqual("output_conflict", retry.exception.code)
            self.assertEqual("completed", retry.exception.details["transaction"]["state"])

    def test_symlink_target_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            source = make_plugin(root / "source")
            outside = make_plugin(root / "outside", source.name)
            target = home / "plugins" / source.name
            target.parent.mkdir()
            try:
                target.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            with self.assertRaises(SkillToPluginError) as raised:
                register_personal_plugin(source, home=home, force=True)

            self.assertEqual("security_rejected", raised.exception.code)
            self.assertTrue(target.is_symlink())


if __name__ == "__main__":
    unittest.main()
