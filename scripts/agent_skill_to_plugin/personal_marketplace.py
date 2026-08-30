"""Safely register a generated plugin in Codex's personal Marketplace."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
from typing import Any
import urllib.parse
import uuid

from .errors import SkillToPluginError
from .limits import TOOL_VERSION
from .models import PersonalMarketplaceRegistration
from .utils import atomic_write_text, hash_tree, sanitize_text, utc_now, validate_path_segment


PERSONAL_MARKETPLACE_RELATIVE = Path(".agents") / "plugins" / "marketplace.json"
PERSONAL_PLUGIN_DIRECTORY = "plugins"
PERSONAL_MARKETPLACE_LOCK = ".agent-skill-to-plugin.lock"
LOCK_OWNER_FILE = "owner.json"
LOCK_JOURNAL_FILE = "transaction.json"
MAX_LOCK_RECORD_BYTES = 32 * 1024
MAX_MARKETPLACE_BYTES = 4 * 1024 * 1024
MARKETPLACE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _metadata(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillToPluginError(
            "Could not inspect a personal Marketplace path.",
            code="personal_marketplace_failed",
            details={"path": str(path), "reason": sanitize_text(str(exc))},
        ) from exc


def _require_safe_directory(path: Path, *, create: bool = False) -> None:
    metadata = _metadata(path)
    if metadata is None and create:
        try:
            path.mkdir()
        except OSError as exc:
            raise SkillToPluginError(
                "Could not create a personal Marketplace directory.",
                code="personal_marketplace_failed",
                details={"path": str(path), "reason": sanitize_text(str(exc))},
            ) from exc
        metadata = _metadata(path)
    if metadata is None:
        raise SkillToPluginError(
            "A required personal Marketplace directory does not exist.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )
    if _is_link_or_reparse(metadata):
        raise SkillToPluginError(
            "Personal Marketplace paths may not be symbolic links or reparse points.",
            code="security_rejected",
            details={"path": str(path)},
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise SkillToPluginError(
            "A personal Marketplace directory path has an unsupported type.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )


def _prepare_personal_directories(home: Path) -> tuple[Path, Path]:
    _require_safe_directory(home)
    plugin_root = home / PERSONAL_PLUGIN_DIRECTORY
    agents_root = home / ".agents"
    marketplace_root = agents_root / "plugins"
    _require_safe_directory(plugin_root, create=True)
    _require_safe_directory(agents_root, create=True)
    _require_safe_directory(marketplace_root, create=True)
    return plugin_root, marketplace_root


def _validate_tree(root: Path, *, label: str) -> None:
    root_metadata = _metadata(root)
    if root_metadata is not None and _is_link_or_reparse(root_metadata):
        raise SkillToPluginError(
            f"{label} may not be a symbolic link or reparse point.",
            code="security_rejected",
            details={"path": str(root)},
        )
    if root_metadata is None or not stat.S_ISDIR(root_metadata.st_mode):
        raise SkillToPluginError(
            f"{label} must be a directory.",
            code="personal_marketplace_failed",
            details={"path": str(root)},
        )

    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise SkillToPluginError(
                f"Could not inspect {label.lower()}.",
                code="personal_marketplace_failed",
                details={"path": str(current), "reason": sanitize_text(str(exc))},
            ) from exc
        for entry in entries:
            child = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillToPluginError(
                    f"Could not inspect {label.lower()}.",
                    code="personal_marketplace_failed",
                    details={"path": str(child), "reason": sanitize_text(str(exc))},
                ) from exc
            if entry.is_symlink() or _is_link_or_reparse(metadata):
                raise SkillToPluginError(
                    f"{label} may not contain symbolic links or reparse points.",
                    code="security_rejected",
                    details={"path": str(child)},
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(child)
            elif not stat.S_ISREG(metadata.st_mode):
                raise SkillToPluginError(
                    f"{label} may not contain special files.",
                    code="security_rejected",
                    details={"path": str(child)},
                )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_bounded_record(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read one lock record without trusting links, replacements, or size."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if _is_link_or_reparse(metadata):
        return None, "linked"
    if not stat.S_ISREG(metadata.st_mode):
        return None, "not_regular"
    if metadata.st_size > MAX_LOCK_RECORD_BYTES:
        return None, "oversized"

    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            return None, "changed_during_read"
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_LOCK_RECORD_BYTES:
            return None, "invalid"
        remaining = MAX_LOCK_RECORD_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_LOCK_RECORD_BYTES:
            return None, "oversized"
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "invalid"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        return None, "invalid"
    return value, "valid"


def _public_owner(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    owner: dict[str, Any] = {}
    pid = record.get("pid")
    if isinstance(pid, int) and pid >= 0:
        owner["pid"] = pid
    for key in ("hostname", "started_at", "tool_version"):
        value = record.get(key)
        if isinstance(value, str) and value:
            owner[key] = sanitize_text(value, 256)
    return owner or None


def _public_transaction(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    transaction: dict[str, Any] = {}
    for key in (
        "state",
        "plugin_name",
        "plugin_target",
        "backup_path",
        "stage_path",
        "marketplace_file",
        "updated_at",
    ):
        value = record.get(key)
        if isinstance(value, str) and value:
            transaction[key] = sanitize_text(value, 1024)
    force = record.get("force")
    if isinstance(force, bool):
        transaction["force"] = force
    return transaction or None


def _existing_lock_error(lock_path: Path) -> SkillToPluginError:
    details: dict[str, Any] = {
        "lock_path": str(lock_path),
        "lock_status": "active_or_crashed",
        "recovery": (
            "Confirm that no personal Marketplace registration is active. Inspect the "
            "reported transaction paths and compare the plugin target, backup, and "
            "marketplace entry. Restore or remove only those exact paths as appropriate, "
            "then manually remove this lock directory before retrying. The tool never "
            "breaks an existing lock automatically."
        ),
    }
    try:
        metadata = lock_path.lstat()
    except FileNotFoundError:
        details["lock_kind"] = "disappeared_during_diagnosis"
        return SkillToPluginError(
            "The personal Marketplace lock changed during diagnosis.",
            code="output_conflict",
            details=details,
        )
    except OSError:
        details["lock_kind"] = "unreadable"
        return SkillToPluginError(
            "The personal Marketplace lock cannot be safely inspected.",
            code="output_conflict",
            details=details,
        )
    if _is_link_or_reparse(metadata):
        details["lock_kind"] = "link_or_reparse"
        return SkillToPluginError(
            "The personal Marketplace lock path is a link or reparse point.",
            code="security_rejected",
            details=details,
        )
    if not stat.S_ISDIR(metadata.st_mode):
        details["lock_kind"] = "non_directory"
        return SkillToPluginError(
            "The personal Marketplace lock path has an unsupported type.",
            code="output_conflict",
            details=details,
        )

    details["lock_kind"] = "directory"
    owner, owner_status = _read_bounded_record(lock_path / LOCK_OWNER_FILE)
    journal, journal_status = _read_bounded_record(lock_path / LOCK_JOURNAL_FILE)
    details["owner_metadata_status"] = owner_status
    details["transaction_metadata_status"] = journal_status
    public_owner = _public_owner(owner)
    if public_owner:
        details["owner"] = public_owner
    transaction = _public_transaction(journal)
    if transaction:
        details["transaction"] = transaction
    if owner_status == "linked" or journal_status == "linked":
        return SkillToPluginError(
            "Personal Marketplace lock metadata is a link or reparse point.",
            code="security_rejected",
            details=details,
        )
    return SkillToPluginError(
        "The personal Marketplace is locked by another registration or an incomplete transaction.",
        code="output_conflict",
        details=details,
    )


def _read_marketplace(path: Path) -> tuple[dict[str, Any], bytes | None]:
    metadata = _metadata(path)
    if metadata is None:
        return {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}, None
    if _is_link_or_reparse(metadata):
        raise SkillToPluginError(
            "The personal Marketplace manifest may not be a symbolic link or reparse point.",
            code="security_rejected",
            details={"path": str(path)},
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise SkillToPluginError(
            "The personal Marketplace manifest must be a regular file.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )
    if metadata.st_size > MAX_MARKETPLACE_BYTES:
        raise SkillToPluginError(
            "The personal Marketplace manifest exceeds the supported size limit.",
            code="personal_marketplace_failed",
            details={"path": str(path), "max_bytes": MAX_MARKETPLACE_BYTES},
        )
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SkillToPluginError(
            "The existing personal Marketplace manifest is invalid.",
            code="personal_marketplace_failed",
            details={"path": str(path), "reason": sanitize_text(str(exc))},
        ) from exc
    if not isinstance(data, dict):
        raise SkillToPluginError(
            "The personal Marketplace manifest must contain a JSON object.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )
    name = data.get("name")
    if not isinstance(name, str) or not MARKETPLACE_NAME_RE.fullmatch(name):
        raise SkillToPluginError(
            "The personal Marketplace has an invalid name.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )
    interface = data.get("interface")
    if interface is not None and not isinstance(interface, dict):
        raise SkillToPluginError(
            "The personal Marketplace interface must be an object when present.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        raise SkillToPluginError(
            "The personal Marketplace plugins field must be an array.",
            code="personal_marketplace_failed",
            details={"path": str(path)},
        )
    names: set[str] = set()
    for item in plugins:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise SkillToPluginError(
                "The personal Marketplace contains an invalid plugin entry.",
                code="personal_marketplace_failed",
                details={"path": str(path)},
            )
        if item["name"] in names:
            raise SkillToPluginError(
                "The personal Marketplace contains duplicate plugin names.",
                code="personal_marketplace_failed",
                details={"path": str(path), "plugin_name": item["name"]},
            )
        names.add(item["name"])
    return data, raw


def _validate_source_plugin(source: Path) -> tuple[str, str]:
    _validate_tree(source, label="Generated plugin")
    plugin_name = source.name
    validate_path_segment(plugin_name, rendered_path=plugin_name)
    if not PLUGIN_NAME_RE.fullmatch(plugin_name):
        raise SkillToPluginError(
            "The generated plugin directory name is invalid.",
            code="personal_marketplace_failed",
            details={"plugin_name": plugin_name},
        )
    manifest_path = source / ".codex-plugin" / "plugin.json"
    metadata = _metadata(manifest_path)
    if metadata is not None and _is_link_or_reparse(metadata):
        raise SkillToPluginError(
            "The generated plugin manifest may not be a symbolic link or reparse point.",
            code="security_rejected",
            details={"path": str(manifest_path)},
        )
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise SkillToPluginError(
            "The generated plugin is missing a safe .codex-plugin/plugin.json manifest.",
            code="personal_marketplace_failed",
            details={"path": str(manifest_path)},
        )
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SkillToPluginError(
            "The generated plugin manifest is invalid.",
            code="personal_marketplace_failed",
            details={"path": str(manifest_path), "reason": sanitize_text(str(exc))},
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("name") != plugin_name:
        raise SkillToPluginError(
            "The generated plugin manifest name must match its directory name.",
            code="personal_marketplace_failed",
            details={"path": str(manifest_path), "plugin_name": plugin_name},
        )
    return plugin_name, hash_tree(source)


def _expected_entry(plugin_name: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }


def _marketplace_stamp(path: Path, raw: bytes | None) -> tuple[Any, ...]:
    metadata = _metadata(path)
    if raw is None:
        return ("missing",) if metadata is None else ("changed",)
    if metadata is None or _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        return ("changed",)
    try:
        current = path.read_bytes()
    except OSError:
        return ("changed",)
    return (
        "file",
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        hashlib.sha256(current).digest(),
    )


def _original_marketplace_stamp(path: Path, raw: bytes | None) -> tuple[Any, ...]:
    if raw is None:
        return ("missing",)
    metadata = _metadata(path)
    if metadata is None:
        return ("changed",)
    return (
        "file",
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        hashlib.sha256(raw).digest(),
    )


def _copy_tree_checked(source: Path, destination: Path, expected_hash: str) -> None:
    if _metadata(destination) is not None:
        raise SkillToPluginError(
            "A personal plugin staging path unexpectedly exists.",
            code="output_conflict",
            details={"path": str(destination)},
        )
    try:
        destination.mkdir()
        for current, dirnames, filenames in os.walk(source, followlinks=False):
            current_path = Path(current)
            relative_root = current_path.relative_to(source)
            for dirname in dirnames:
                source_dir = current_path / dirname
                metadata = source_dir.lstat()
                if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                    raise SkillToPluginError(
                        "The generated plugin changed while it was being registered.",
                        code="security_rejected",
                        details={"path": str(source_dir)},
                    )
                (destination / relative_root / dirname).mkdir()
            for filename in filenames:
                source_file = current_path / filename
                metadata = source_file.lstat()
                if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                    raise SkillToPluginError(
                        "The generated plugin changed while it was being registered.",
                        code="security_rejected",
                        details={"path": str(source_file)},
                    )
                shutil.copy2(source_file, destination / relative_root / filename)
        if hash_tree(destination) != expected_hash or hash_tree(source) != expected_hash:
            raise SkillToPluginError(
                "The generated plugin changed while it was being registered.",
                code="personal_marketplace_failed",
                details={"path": str(source)},
            )
    except Exception:
        if _metadata(destination) is not None:
            try:
                _validate_tree(destination, label="Plugin staging tree")
                shutil.rmtree(destination)
            except Exception:
                pass
        raise


def _verify_durable_registration(
    *,
    target: Path,
    expected_hash: str,
    marketplace_file: Path,
    expected_entry: dict[str, Any],
) -> str:
    """Read back both durable surfaces before reporting registration success."""

    _validate_tree(target, label="Registered personal plugin")
    actual_hash = hash_tree(target)
    if actual_hash != expected_hash:
        raise SkillToPluginError(
            "The registered personal plugin failed final hash verification.",
            code="personal_marketplace_failed",
            details={"path": str(target), "expected_sha256": expected_hash, "actual_sha256": actual_hash},
        )
    marketplace, _raw = _read_marketplace(marketplace_file)
    matches = [item for item in marketplace["plugins"] if item.get("name") == expected_entry["name"]]
    if len(matches) != 1 or matches[0] != expected_entry:
        raise SkillToPluginError(
            "The personal Marketplace entry failed final verification.",
            code="personal_marketplace_failed",
            details={"path": str(marketplace_file), "plugin_name": expected_entry["name"]},
        )
    return marketplace["name"]


class _PersonalMarketplaceLock:
    def __init__(self, marketplace_root: Path) -> None:
        self.path = marketplace_root / PERSONAL_MARKETPLACE_LOCK
        self.owner_file = self.path / LOCK_OWNER_FILE
        self.journal_file = self.path / LOCK_JOURNAL_FILE
        self.token = uuid.uuid4().hex
        self.owner_record: dict[str, Any] | None = None
        self.acquired = False
        self.retained = False

    def __enter__(self) -> "_PersonalMarketplaceLock":
        existing = _metadata(self.path)
        if existing is not None:
            raise _existing_lock_error(self.path)
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            raise _existing_lock_error(self.path) from exc
        except OSError as exc:
            raise SkillToPluginError(
                "Could not acquire the personal Marketplace registration lock.",
                code="personal_marketplace_failed",
                details={"lock_path": str(self.path), "reason": sanitize_text(str(exc))},
            ) from exc
        try:
            try:
                hostname = socket.gethostname()
            except OSError:
                hostname = "unknown"
            self.owner_record = {
                "schema_version": 1,
                "token": self.token,
                "pid": os.getpid(),
                "hostname": hostname,
                "started_at": utc_now(),
                "tool_version": TOOL_VERSION,
            }
            self._write_record(self.owner_file, self.owner_record)
            self.acquired = True
        except Exception:
            try:
                _validate_tree(self.path, label="Personal Marketplace lock")
                shutil.rmtree(self.path)
            except Exception:
                pass
            raise
        return self

    def _write_record(self, path: Path, record: dict[str, Any]) -> None:
        rendered = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        if len(rendered.encode("utf-8")) > MAX_LOCK_RECORD_BYTES:
            raise SkillToPluginError(
                "Personal Marketplace transaction metadata exceeds its safety limit.",
                code="personal_marketplace_failed",
                details={"path": str(path), "max_bytes": MAX_LOCK_RECORD_BYTES},
            )
        atomic_write_text(path, rendered)

    def _assert_ownership(self) -> None:
        metadata = _metadata(self.path)
        if metadata is None or _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            self.retained = True
            raise SkillToPluginError(
                "The personal Marketplace lock changed while registration was active.",
                code="security_rejected",
                details={"lock_path": str(self.path)},
            )
        owner, status = _read_bounded_record(self.owner_file)
        if status != "valid" or owner is None or owner.get("token") != self.token:
            self.retained = True
            raise SkillToPluginError(
                "The personal Marketplace lock ownership record changed while registration was active.",
                code="security_rejected" if status == "linked" else "personal_marketplace_failed",
                details={"lock_path": str(self.path), "owner_metadata_status": status},
            )

    def write_journal(self, *, state: str, transaction: dict[str, Any]) -> None:
        self._assert_ownership()
        existing, status = _read_bounded_record(self.journal_file)
        if status not in {"missing", "valid"}:
            self.retained = True
            raise SkillToPluginError(
                "Personal Marketplace transaction metadata cannot be safely updated.",
                code="security_rejected" if status == "linked" else "personal_marketplace_failed",
                details={"path": str(self.journal_file), "transaction_metadata_status": status},
            )
        if existing is not None and existing.get("token") != self.token:
            self.retained = True
            raise SkillToPluginError(
                "Personal Marketplace transaction metadata has different ownership.",
                code="security_rejected",
                details={"path": str(self.journal_file)},
            )
        record = {
            "schema_version": 1,
            "token": self.token,
            **transaction,
            "state": state,
            "updated_at": utc_now(),
        }
        self._write_record(self.journal_file, record)
        verified, verified_status = _read_bounded_record(self.journal_file)
        if (
            verified_status != "valid"
            or verified is None
            or verified.get("token") != self.token
            or verified.get("state") != state
        ):
            self.retained = True
            raise SkillToPluginError(
                "Personal Marketplace transaction metadata could not be verified after writing.",
                code="personal_marketplace_failed",
                details={"path": str(self.journal_file), "transaction_metadata_status": verified_status},
            )

    def clear_journal(self) -> None:
        self._assert_ownership()
        record, status = _read_bounded_record(self.journal_file)
        if status == "missing":
            return
        if status != "valid" or record is None or record.get("token") != self.token:
            self.retained = True
            raise SkillToPluginError(
                "Personal Marketplace transaction metadata cannot be safely cleared.",
                code="security_rejected" if status == "linked" else "personal_marketplace_failed",
                details={"path": str(self.journal_file), "transaction_metadata_status": status},
            )
        try:
            self.journal_file.unlink()
        except OSError as exc:
            self.retained = True
            raise SkillToPluginError(
                "Personal Marketplace transaction metadata could not be cleared.",
                code="personal_marketplace_failed",
                details={"path": str(self.journal_file), "reason": sanitize_text(str(exc))},
            ) from exc

    def retain(self) -> None:
        self.retained = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.acquired:
            return
        if self.retained:
            if isinstance(exc, SkillToPluginError):
                exc.details.setdefault("lock_retained", True)
                exc.details.setdefault("lock_path", str(self.path))
            return
        _record, journal_status = _read_bounded_record(self.journal_file)
        if journal_status != "missing":
            self.retained = True
            if isinstance(exc, SkillToPluginError):
                exc.details.setdefault("lock_retained", True)
                exc.details.setdefault("lock_path", str(self.path))
                exc.details.setdefault("transaction_metadata_status", journal_status)
                return
            raise SkillToPluginError(
                "Registration ended with transaction metadata still present; the lock was retained for recovery.",
                code="personal_marketplace_failed",
                details={
                    "registration_status": "succeeded_lock_retained",
                    "commit_durable": True,
                    "installation_performed": False,
                    "lock_retained": True,
                    "lock_path": str(self.path),
                    "transaction_metadata_status": journal_status,
                },
            )
        try:
            self._assert_ownership()
            self.owner_file.unlink()
            self.path.rmdir()
            self.acquired = False
        except SkillToPluginError as cleanup_error:
            self.retained = True
            if isinstance(exc, SkillToPluginError):
                exc.details.setdefault("lock_retained", True)
                exc.details.setdefault("lock_path", str(self.path))
                exc.details.setdefault("lock_release_error", sanitize_text(str(cleanup_error)))
                return
            cleanup_error.details.setdefault("registration_status", "succeeded_lock_retained")
            cleanup_error.details.setdefault("commit_durable", True)
            cleanup_error.details.setdefault("installation_performed", False)
            cleanup_error.details.setdefault("lock_retained", True)
            cleanup_error.details.setdefault("lock_path", str(self.path))
            raise
        except OSError as cleanup_error:
            self.retained = True
            # If removing the final directory failed after owner.json was
            # unlinked, restore the same bounded ownership record when safe so
            # the next diagnostic remains attributable.
            if self.owner_record is not None and _metadata(self.owner_file) is None:
                try:
                    metadata = _metadata(self.path)
                    if metadata is not None and stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(metadata):
                        self._write_record(self.owner_file, self.owner_record)
                except Exception:
                    pass
            if exc is None:
                raise SkillToPluginError(
                    "Registration completed, but its personal Marketplace lock could not be released.",
                    code="personal_marketplace_failed",
                    details={
                        "registration_status": "succeeded_lock_retained",
                        "commit_durable": True,
                        "installation_performed": False,
                        "lock_retained": True,
                        "lock_path": str(self.path),
                        "reason": sanitize_text(str(cleanup_error)),
                    },
                ) from cleanup_error
            if isinstance(exc, SkillToPluginError):
                exc.details.setdefault("lock_retained", True)
                exc.details.setdefault("lock_path", str(self.path))


def _registration_result(
    *,
    status: str,
    marketplace_name: str,
    marketplace_file: Path,
    plugin_dir: Path,
    reinstall_required: bool,
) -> PersonalMarketplaceRegistration:
    encoded_name = urllib.parse.quote(plugin_dir.name, safe="")
    encoded_marketplace = urllib.parse.quote(str(marketplace_file), safe="")
    view_url = f"codex://plugins/{encoded_name}?marketplacePath={encoded_marketplace}"
    return PersonalMarketplaceRegistration(
        status=status,
        marketplace_name=marketplace_name,
        marketplace_file=str(marketplace_file),
        plugin_dir=str(plugin_dir),
        policy_installation="AVAILABLE",
        policy_authentication="ON_INSTALL",
        category="Productivity",
        reinstall_required=reinstall_required,
        installation_performed=False,
        view_url=view_url,
        share_url=f"{view_url}&mode=share",
    )


def _register_personal_plugin(
    source_plugin: Path,
    *,
    force: bool = False,
    home: Path | None = None,
) -> PersonalMarketplaceRegistration:
    """Copy ``source_plugin`` into ``~/plugins`` and register it safely.

    Registration is idempotent when both the installed tree and Marketplace
    entry already match. Partial state is repaired without ``force``; replacing
    different content or a different same-name entry requires ``force``.
    """

    source = Path(os.path.abspath(Path(source_plugin)))
    home_path = Path(os.path.abspath(Path.home() if home is None else Path(home)))
    plugin_name, source_hash = _validate_source_plugin(source)
    plugin_root, marketplace_root = _prepare_personal_directories(home_path)
    marketplace_file = marketplace_root / "marketplace.json"
    target = plugin_root / plugin_name
    expected_entry = _expected_entry(plugin_name)

    with _PersonalMarketplaceLock(marketplace_root) as registration_lock:
        marketplace, original_raw = _read_marketplace(marketplace_file)
        original_stamp = _original_marketplace_stamp(marketplace_file, original_raw)
        matches = [
            (index, item)
            for index, item in enumerate(marketplace["plugins"])
            if item.get("name") == plugin_name
        ]
        entry_index = matches[0][0] if matches else None
        entry_matches = bool(matches and matches[0][1] == expected_entry)

        target_metadata = _metadata(target)
        target_matches = False
        target_exists = target_metadata is not None
        target_hash: str | None = None
        if target_exists:
            if _is_link_or_reparse(target_metadata):
                raise SkillToPluginError(
                    "The personal plugin target may not be a link or reparse point.",
                    code="security_rejected",
                    details={"path": str(target)},
                )
            if not stat.S_ISDIR(target_metadata.st_mode):
                raise SkillToPluginError(
                    "The personal plugin target exists and is not a directory.",
                    code="output_conflict",
                    details={"path": str(target)},
                )
            _validate_tree(target, label="Existing personal plugin")
            target_hash = hash_tree(target)
            target_matches = target_hash == source_hash

        entry_conflict = entry_index is not None and not entry_matches
        target_conflict = target_exists and not target_matches
        if (entry_conflict or target_conflict) and not force:
            raise SkillToPluginError(
                "A different same-name personal plugin registration already exists; use --force-personal to replace it.",
                code="output_conflict",
                details={
                    "plugin_name": plugin_name,
                    "plugin_path_conflict": target_conflict,
                    "marketplace_entry_conflict": entry_conflict,
                },
            )
        if target_matches and entry_matches:
            return _registration_result(
                status="already_registered",
                marketplace_name=marketplace["name"],
                marketplace_file=marketplace_file,
                plugin_dir=target,
                reinstall_required=False,
            )

        plugin_needs_change = not target_exists or target_conflict
        entry_needs_change = entry_index is None or entry_conflict
        stage = plugin_root / f".{plugin_name}.agent-skill-to-plugin.{uuid.uuid4().hex}.tmp"
        backup = plugin_root / f".{plugin_name}.agent-skill-to-plugin.{uuid.uuid4().hex}.bak"
        transaction = {
            "plugin_name": plugin_name,
            "plugin_target": str(target),
            "backup_path": str(backup),
            "stage_path": str(stage),
            "marketplace_file": str(marketplace_file),
            "force": force,
            "source_tree_sha256": source_hash,
            "previous_target_tree_sha256": target_hash,
            "marketplace_original_sha256": hashlib.sha256(original_raw).hexdigest() if original_raw is not None else None,
        }
        installed_new = False
        moved_backup = False
        journal_started = False
        transaction_committed = False
        commit_phase = "pre_commit"
        commit_verified = False
        verified_marketplace_name = marketplace["name"]
        try:
            if plugin_needs_change:
                _copy_tree_checked(source, stage, source_hash)

            # The prepared record is durable before any target replacement or
            # Marketplace write. A crash can therefore be diagnosed without
            # guessing which generated hidden paths belong to this operation.
            registration_lock.write_journal(state="prepared", transaction=transaction)
            journal_started = True

            if plugin_needs_change:
                if target_exists:
                    # Recheck the observed target before replacing it so an
                    # out-of-band update cannot be silently overwritten.
                    _validate_tree(target, label="Existing personal plugin")
                    if hash_tree(target) != target_hash:
                        raise SkillToPluginError(
                            "The personal plugin target changed during registration.",
                            code="output_conflict",
                            details={"path": str(target)},
                        )
                    if not force:
                        raise SkillToPluginError(
                            "Replacing a different personal plugin requires --force-personal.",
                            code="output_conflict",
                            details={"path": str(target)},
                        )
                    target.replace(backup)
                    moved_backup = True
                    registration_lock.write_journal(state="backup_moved", transaction=transaction)
                elif _metadata(target) is not None:
                    raise SkillToPluginError(
                        "The personal plugin target appeared during registration.",
                        code="output_conflict",
                        details={"path": str(target)},
                    )
                stage.replace(target)
                installed_new = True
                registration_lock.write_journal(state="plugin_installed", transaction=transaction)

            if entry_needs_change:
                if _marketplace_stamp(marketplace_file, original_raw) != original_stamp:
                    raise SkillToPluginError(
                        "The personal Marketplace changed during registration.",
                        code="output_conflict",
                        details={"path": str(marketplace_file)},
                    )
                if entry_index is None:
                    marketplace["plugins"].append(expected_entry)
                else:
                    marketplace["plugins"][entry_index] = expected_entry
                atomic_write_text(
                    marketplace_file,
                    json.dumps(marketplace, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
                )
                transaction_committed = True
                commit_phase = "journal_update"
                registration_lock.write_journal(state="marketplace_committed", transaction=transaction)
            else:
                # The entry was already exact, so installing the missing plugin
                # tree itself is the durable commit point.
                transaction_committed = True
                commit_phase = "journal_update"
                registration_lock.write_journal(state="plugin_committed", transaction=transaction)

            # Verify the committed target and entry before deleting a force
            # backup, then read them back once more after all cleanup.
            commit_phase = "verification"
            verified_marketplace_name = _verify_durable_registration(
                target=target,
                expected_hash=source_hash,
                marketplace_file=marketplace_file,
                expected_entry=expected_entry,
            )
            commit_verified = True
            commit_phase = "cleanup"

            # Remove only trees whose hashes prove they are the temporary or
            # backup trees observed by this transaction. Any mismatch retains
            # the recovery journal and lock rather than deleting unknown data.
            if _metadata(stage) is not None:
                _validate_tree(stage, label="Plugin staging tree")
                if hash_tree(stage) != source_hash:
                    raise SkillToPluginError(
                        "The registration staging tree changed before cleanup.",
                        code="security_rejected",
                        details={"path": str(stage)},
                    )
                shutil.rmtree(stage)
            if _metadata(backup) is not None:
                _validate_tree(backup, label="Personal plugin backup")
                if target_hash is None or hash_tree(backup) != target_hash:
                    raise SkillToPluginError(
                        "The personal plugin backup changed before cleanup.",
                        code="security_rejected",
                        details={"path": str(backup)},
                    )
                shutil.rmtree(backup)
            commit_phase = "final_verification"
            verified_marketplace_name = _verify_durable_registration(
                target=target,
                expected_hash=source_hash,
                marketplace_file=marketplace_file,
                expected_entry=expected_entry,
            )
            commit_verified = True
            commit_phase = "journal_finalize"
            registration_lock.write_journal(state="completed", transaction=transaction)
            registration_lock.clear_journal()
        except Exception as exc:
            if transaction_committed:
                # The Plugin and Marketplace now agree. Never roll back only
                # the Plugin after this point; retain exact recovery metadata
                # for cleanup instead.
                try:
                    if journal_started:
                        registration_lock.write_journal(state=f"{commit_phase}_incomplete", transaction=transaction)
                except Exception:
                    pass
                registration_lock.retain()
                error = exc if isinstance(exc, SkillToPluginError) else SkillToPluginError(
                    "Personal Marketplace registration committed, but transaction cleanup failed.",
                    code="personal_marketplace_failed",
                    details={"reason": sanitize_text(str(exc))},
                )
                committed_status = (
                    "committed_verification_failed"
                    if commit_phase in {"verification", "final_verification"}
                    else "succeeded_cleanup_incomplete"
                    if commit_phase == "cleanup"
                    else "succeeded_journal_incomplete"
                )
                error.details.setdefault("registration_status", committed_status)
                error.details.setdefault("commit_durable", True)
                error.details.setdefault("commit_verified", commit_verified)
                error.details.setdefault("commit_phase", commit_phase)
                error.details.setdefault("installation_performed", False)
                error.details.setdefault("reinstall_required", bool(force and (target_conflict or entry_conflict)))
                error.details.setdefault("lock_retained", True)
                error.details.setdefault("lock_path", str(registration_lock.path))
                error.details.setdefault("backup_path", str(backup))
                raise error from exc if error is not exc else None

            rollback_errors: list[str] = []
            if journal_started:
                try:
                    registration_lock.write_journal(state="rollback_started", transaction=transaction)
                except Exception as journal_error:
                    rollback_errors.append(f"journal update: {sanitize_text(str(journal_error))}")

            if installed_new and _metadata(target) is not None:
                try:
                    _validate_tree(target, label="New personal plugin")
                    if hash_tree(target) != source_hash:
                        raise SkillToPluginError(
                            "The newly installed plugin changed before rollback.",
                            code="security_rejected",
                            details={"path": str(target)},
                        )
                    shutil.rmtree(target)
                except Exception as rollback_error:
                    rollback_errors.append(f"remove new plugin: {sanitize_text(str(rollback_error))}")

            if moved_backup:
                try:
                    backup_metadata = _metadata(backup)
                    if backup_metadata is None:
                        raise SkillToPluginError(
                            "The personal plugin backup is missing during rollback.",
                            code="personal_marketplace_failed",
                            details={"path": str(backup)},
                        )
                    _validate_tree(backup, label="Personal plugin backup")
                    if target_hash is None or hash_tree(backup) != target_hash:
                        raise SkillToPluginError(
                            "The personal plugin backup changed before rollback.",
                            code="security_rejected",
                            details={"path": str(backup)},
                        )
                    if _metadata(target) is not None:
                        raise SkillToPluginError(
                            "The personal plugin target is occupied during rollback.",
                            code="output_conflict",
                            details={"path": str(target)},
                        )
                    backup.replace(target)
                except Exception as rollback_error:
                    rollback_errors.append(f"restore backup: {sanitize_text(str(rollback_error))}")

            if _metadata(stage) is not None:
                try:
                    _validate_tree(stage, label="Plugin staging tree")
                    if hash_tree(stage) != source_hash:
                        raise SkillToPluginError(
                            "The plugin staging tree changed before rollback.",
                            code="security_rejected",
                            details={"path": str(stage)},
                        )
                    shutil.rmtree(stage)
                except Exception as rollback_error:
                    rollback_errors.append(f"remove staging tree: {sanitize_text(str(rollback_error))}")

            if not rollback_errors and journal_started:
                try:
                    registration_lock.write_journal(state="rolled_back", transaction=transaction)
                    registration_lock.clear_journal()
                except Exception as journal_error:
                    rollback_errors.append(f"clear journal: {sanitize_text(str(journal_error))}")

            if rollback_errors:
                try:
                    # Even if the initial prepared record could not be written,
                    # make one bounded best-effort recovery record before
                    # retaining the lock and any unknown temporary tree.
                    registration_lock.write_journal(state="rollback_incomplete", transaction=transaction)
                    journal_started = True
                except Exception:
                    pass
                registration_lock.retain()
                error = exc if isinstance(exc, SkillToPluginError) else SkillToPluginError(
                    "Personal Marketplace registration failed and rollback was incomplete.",
                    code="personal_marketplace_failed",
                    details={"reason": sanitize_text(str(exc))},
                )
                error.details["registration_status"] = "rollback_incomplete"
                error.details["rollback_errors"] = rollback_errors
                error.details["lock_retained"] = True
                error.details["lock_path"] = str(registration_lock.path)
                error.details["backup_path"] = str(backup)
                error.details["recovery"] = (
                    "Inspect the retained lock transaction journal, plugin target, backup, and Marketplace entry. "
                    "Restore or remove only paths whose ownership and recorded hashes are verified before retrying."
                )
                raise error from exc if error is not exc else None

            if isinstance(exc, SkillToPluginError):
                raise
            raise SkillToPluginError(
                "Personal Marketplace registration failed.",
                code="personal_marketplace_failed",
                details={"reason": sanitize_text(str(exc)), "registration_status": "rolled_back"},
            ) from exc

        return _registration_result(
            status="updated" if force and (target_conflict or entry_conflict) else "registered",
            marketplace_name=verified_marketplace_name,
            marketplace_file=marketplace_file,
            plugin_dir=target,
            reinstall_required=bool(force and (target_conflict or entry_conflict)),
        )


def register_personal_plugin(
    source_plugin: Path,
    *,
    force: bool = False,
    home: Path | None = None,
) -> PersonalMarketplaceRegistration:
    """Register a generated plugin and convert filesystem failures to a stable error."""

    try:
        return _register_personal_plugin(source_plugin, force=force, home=home)
    except SkillToPluginError:
        raise
    except OSError as exc:
        raise SkillToPluginError(
            "Personal Marketplace registration failed.",
            code="personal_marketplace_failed",
            details={"reason": sanitize_text(str(exc))},
        ) from exc
