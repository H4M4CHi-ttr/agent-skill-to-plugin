"""Build, validate, and transactionally publish skills-only plugin artifacts."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import tempfile
from typing import Any, Iterable, Sequence
import uuid
import zipfile

import yaml

from .errors import SkillToPluginError
from .limits import (
    DEFAULT_PLUGIN_VERSION,
    MAX_COMBINED_IDENTITY,
    MAX_COMPRESSED_BYTES,
    MAX_DEFAULT_PROMPT,
    MAX_FILES,
    MAX_MEMBER_BYTES,
    MAX_PLUGIN_DESCRIPTION,
    MAX_PLUGIN_DISPLAY_NAME,
    MAX_PLUGIN_NAME,
    MAX_PLUGIN_SHORT_DESCRIPTION,
    MAX_SKILL_SHORT_DESCRIPTION,
    MAX_TOTAL_BYTES,
    MIN_SKILL_SHORT_DESCRIPTION,
    SCHEMA_VERSION,
    TOOL_VERSION,
)
from .models import ConversionResult, Diagnostic, Provenance, ResolutionState, SelectedSkill, SkillCandidate
from .reporting import write_reports
from .utils import (
    atomic_write_text,
    collapse_ws,
    ensure_within,
    file_hashes,
    hash_tree,
    iter_regular_files,
    normalized_path_key,
    sanitize_text,
    sha256_file,
    slugify,
    utc_now,
    validate_path_segment,
)


OUTPUT_LOCK_NAME = ".agent-skill-to-plugin.lock"
LOCK_OWNER_FILE = "owner.json"
MAX_LOCK_METADATA_BYTES = 16 * 1024


SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _read_lock_record(lock_dir: Path) -> tuple[dict[str, Any] | None, str]:
    """Read bounded owner metadata without following a supplied link."""

    owner_file = lock_dir / LOCK_OWNER_FILE
    try:
        metadata = owner_file.lstat()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if stat.S_ISLNK(metadata.st_mode) or is_reparse:
        return None, "linked"
    if not stat.S_ISREG(metadata.st_mode):
        return None, "not_regular"
    if metadata.st_size > MAX_LOCK_METADATA_BYTES:
        return None, "oversized"
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(owner_file, flags)
        opened = os.fstat(descriptor)
        # Detect replacement between lstat and open even where O_NOFOLLOW is
        # unavailable (notably Windows). Do not read a raced link target.
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            return None, "changed_during_read"
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_LOCK_METADATA_BYTES:
            return None, "invalid"
        chunks: list[bytes] = []
        remaining = MAX_LOCK_METADATA_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_LOCK_METADATA_BYTES:
            return None, "oversized"
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        return None, "invalid"
    return value, "valid"


def _public_lock_owner(record: dict[str, Any] | None) -> dict[str, Any] | None:
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


def _existing_lock_error(lock_dir: Path) -> SkillToPluginError:
    details: dict[str, Any] = {
        "lock_path": str(lock_dir),
        "lock_status": "active_or_crashed",
        "recovery": (
            "Confirm that no Agent Skill to Plugin conversion is using this output root, "
            "then manually remove the lock directory before retrying. Locks are never "
            "automatically broken because PID reuse and remote filesystems make stale-lock "
            "detection unsafe."
        ),
    }
    try:
        metadata = lock_dir.lstat()
    except FileNotFoundError:
        details["lock_kind"] = "disappeared_during_diagnosis"
        details["owner_metadata_status"] = "unavailable"
    except OSError:
        details["lock_kind"] = "unreadable"
        details["owner_metadata_status"] = "unavailable"
    else:
        is_reparse = bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        if stat.S_ISLNK(metadata.st_mode) or is_reparse:
            details["lock_kind"] = "link_or_reparse"
            details["owner_metadata_status"] = "not_read"
        elif not stat.S_ISDIR(metadata.st_mode):
            details["lock_kind"] = "non_directory"
            details["owner_metadata_status"] = "not_read"
        else:
            details["lock_kind"] = "directory"
            record, status = _read_lock_record(lock_dir)
            details["owner_metadata_status"] = status
            owner = _public_lock_owner(record)
            if owner:
                details["owner"] = owner
    return SkillToPluginError(
        "This output root is locked by another conversion or by a conversion that "
        "terminated before releasing its lock.",
        code="output_conflict",
        details=details,
    )


class _OutputRootLock:
    """Non-waiting cross-process lock backed by an atomic directory creation."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root.resolve()
        self.lock_dir = self.output_root / OUTPUT_LOCK_NAME
        self.owner_file = self.lock_dir / LOCK_OWNER_FILE
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "_OutputRootLock":
        try:
            self.lock_dir.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError as exc:
            raise _existing_lock_error(self.lock_dir) from exc
        except OSError as exc:
            raise SkillToPluginError(
                "Could not atomically acquire the output-root packaging lock.",
                code="output_conflict",
                details={"lock_path": str(self.lock_dir), "reason": sanitize_text(str(exc))},
            ) from exc

        try:
            try:
                hostname = socket.gethostname()
            except OSError:
                hostname = "unknown"
            owner = {
                "schema_version": 1,
                "token": self.token,
                "pid": os.getpid(),
                "hostname": hostname,
                "started_at": utc_now(),
                "tool_version": TOOL_VERSION,
            }
            atomic_write_text(
                self.owner_file,
                json.dumps(owner, ensure_ascii=False, indent=2) + "\n",
            )
        except Exception as exc:
            # We created this directory and have not exposed an acquired lock to
            # the caller. Remove only the known empty/owner-only directory.
            self.owner_file.unlink(missing_ok=True)
            try:
                self.lock_dir.rmdir()
            except OSError:
                pass
            if isinstance(exc, SkillToPluginError):
                raise
            raise SkillToPluginError(
                "The output-root lock was created but its owner metadata could not be written.",
                code="output_conflict",
                details={"lock_path": str(self.lock_dir), "reason": sanitize_text(str(exc))},
            ) from exc
        self.acquired = True
        return self

    def _release(self) -> None:
        if not self.acquired:
            return
        record, status = _read_lock_record(self.lock_dir)
        if status != "valid" or record is None or record.get("token") != self.token:
            self.acquired = False
            raise SkillToPluginError(
                "The output-root lock ownership changed while packaging; the lock was retained.",
                code="output_conflict",
                details={
                    "lock_path": str(self.lock_dir),
                    "owner_metadata_status": status,
                    "recovery": "Inspect the lock before manually removing it; artifacts may already have been committed.",
                },
            )
        try:
            self.owner_file.unlink()
            self.lock_dir.rmdir()
        except OSError as exc:
            self.acquired = False
            raise SkillToPluginError(
                "Packaging finished but the output-root lock could not be released safely.",
                code="output_conflict",
                details={
                    "lock_path": str(self.lock_dir),
                    "reason": sanitize_text(str(exc)),
                    "recovery": "Inspect the lock before manually removing it; artifacts may already have been committed.",
                },
            ) from exc
        self.acquired = False

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> bool:
        try:
            self._release()
        except SkillToPluginError as release_error:
            if exc is None:
                raise
            if isinstance(exc, SkillToPluginError):
                exc.details.setdefault("lock_release_error", release_error.to_dict())
            elif hasattr(exc, "add_note"):
                exc.add_note(f"Output lock release failed: {release_error.message}")
        return False


def _truncate(value: str, limit: int) -> str:
    value = collapse_ws(value)
    return value if len(value) <= limit else value[: max(1, limit - 1)].rstrip() + "…"


def _humanize(slug: str) -> str:
    return " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in slug.split("-"))


def _source_label(state: ResolutionState) -> str:
    source = state.resolved_source.original_plugin_name or state.resolved_source.normalized_source
    source = source.rstrip("/\\")
    label = source.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].removesuffix(".git")
    return label or "imported-skills"


def _candidate_skill_dir(snapshot: Path, candidate: SkillCandidate) -> Path:
    candidate_path = ensure_within(snapshot / Path(candidate.path), snapshot, code="resolution_integrity_failed")
    if candidate_path.is_file():
        candidate_path = candidate_path.parent
    if not (candidate_path / "SKILL.md").is_file():
        raise SkillToPluginError(
            "A selected candidate no longer contains `SKILL.md`.",
            code="resolution_integrity_failed",
            details={"candidate_id": candidate.id, "path": candidate.path},
        )
    return candidate_path


def _artifact_conflicts(output_root: Path, plugin_name: str) -> bool:
    paths = (
        output_root / "plugins" / plugin_name,
        output_root / "packages" / f"{plugin_name}.zip",
        output_root / "reports" / f"{plugin_name}.json",
        output_root / "reports" / f"{plugin_name}.md",
    )
    if any(path.exists() for path in paths):
        return True
    marketplace_file = output_root / ".agents" / "plugins" / "marketplace.json"
    if marketplace_file.is_file():
        try:
            data = json.loads(marketplace_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillToPluginError(
                "Existing marketplace is unreadable or invalid JSON.",
                code="package_validation_failed",
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
            raise SkillToPluginError(
                "Existing marketplace has an unsupported structure.",
                code="package_validation_failed",
            )
        return any(isinstance(item, dict) and item.get("name") == plugin_name for item in data.get("plugins", []))
    return False


def choose_plugin_name(
    state: ResolutionState,
    candidates: Sequence[SkillCandidate],
    output_root: Path,
    requested_name: str | None,
    *,
    force: bool,
) -> tuple[str, bool]:
    names = [candidate.name or "skill" for candidate in candidates]
    max_name_length = min(MAX_PLUGIN_NAME, MAX_COMBINED_IDENTITY - 1 - max(map(len, names)))
    if max_name_length < 1:
        raise SkillToPluginError("Skill name leaves no room for a valid plugin namespace.", code="package_validation_failed")
    if requested_name:
        base = slugify(requested_name, "skill-plugin")
    elif state.resolved_source.original_plugin_name:
        base = slugify(state.resolved_source.original_plugin_name, "skill-plugin")
    elif len(candidates) == 1:
        base = slugify(f"{names[0]}-plugin", "skill-plugin")
    else:
        base = slugify(f"{_source_label(state)}-skills", "skill-pack")
    base = base[:max_name_length].rstrip("-") or "p"
    base_conflict = _artifact_conflicts(output_root, base)
    if force or not base_conflict:
        return base, base_conflict
    for index in range(2, 10_001):
        suffix = f"-{index}"
        candidate_name = base[: max_name_length - len(suffix)].rstrip("-") + suffix
        if not _artifact_conflicts(output_root, candidate_name):
            return candidate_name, True
    raise SkillToPluginError("Could not find a collision-free plugin name.", code="output_conflict")


def build_manifest(
    plugin_name: str,
    candidates: Sequence[SkillCandidate],
    *,
    display_name: str | None,
    author_name: str,
    version: str,
) -> dict[str, Any]:
    if not SEMVER_RE.fullmatch(version):
        raise SkillToPluginError("Plugin version must use semantic versioning.", code="package_validation_failed")
    if len(candidates) == 1:
        description = candidates[0].description or f"Imported Agent Skill {candidates[0].name}."
    else:
        description = f"Bundle of {len(candidates)} Agent Skills imported into an OpenAI skills-only plugin."
    description = _truncate(description, MAX_PLUGIN_DESCRIPTION)
    display = _truncate(display_name or _humanize(plugin_name), MAX_PLUGIN_DISPLAY_NAME)
    short = _truncate(
        "Imported Agent Skill" if len(candidates) == 1 else f"{len(candidates)} imported Agent Skills",
        MAX_PLUGIN_SHORT_DESCRIPTION,
    )
    prompt = (
        f"Use the {candidates[0].name} skill for this task."
        if len(candidates) == 1 else "Choose the appropriate bundled skill for this task."
    )
    return {
        "name": plugin_name,
        "version": version,
        "description": description,
        "author": {"name": _truncate(author_name or "Local conversion", 80)},
        "skills": "./skills/",
        "interface": {
            "displayName": display,
            "shortDescription": short,
            "longDescription": description,
            "developerName": _truncate(author_name or "Local conversion", 80),
            "category": "Productivity",
            "capabilities": ["Use bundled Agent Skills"],
            "defaultPrompt": [_truncate(prompt, 128)],
        },
    }


_DISABLE_INVOCATION_LINE = re.compile(
    r"^(?P<key>(?:disable-model-invocation|disable_model_invocation|"
    r"['\"]disable-model-invocation['\"]|['\"]disable_model_invocation['\"]))"
    r"(?P<separator>\s*:\s*)(?P<value>[^#\r\n]*?)(?P<comment>\s*(?:#.*)?)$",
    re.IGNORECASE,
)


def _leaf_field_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    """Return value-free report keys for a nested metadata comparison.

    Values are retained only in memory for equality checks and are never
    emitted to reports, where they could contain source-controlled secrets.
    """

    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    fields: dict[str, Any] = {}
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict):
            nested = _leaf_field_paths(child, path)
            if nested:
                fields.update(nested)
            else:
                fields[path] = {}
        else:
            fields[path] = child
    return fields


def _generated_agent_payload(candidate: SkillCandidate, source: dict[str, Any] | None, *, explicit_only: bool) -> dict[str, Any]:
    source = source or {}
    source_interface = source.get("interface") if isinstance(source.get("interface"), dict) else {}
    short_description = collapse_ws(
        source_interface.get("short_description")
        if isinstance(source_interface.get("short_description"), str) and source_interface.get("short_description").strip()
        else str(candidate.description)
    )
    if len(short_description) < MIN_SKILL_SHORT_DESCRIPTION:
        short_description = collapse_ws(f"{short_description.rstrip('.')} for imported Agent Skill use")
    short_description = _truncate(short_description, MAX_SKILL_SHORT_DESCRIPTION)
    interface: dict[str, Any] = {
        "display_name": (
            source_interface.get("display_name")
            if isinstance(source_interface.get("display_name"), str) and source_interface.get("display_name").strip()
            else _humanize(str(candidate.name))
        ),
        "short_description": short_description,
    }
    for key in ("icon_small", "icon_large"):
        value = source_interface.get(key)
        if isinstance(value, str) and value.strip():
            interface[key] = value
    brand_color = source_interface.get("brand_color")
    if isinstance(brand_color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", brand_color):
        interface["brand_color"] = brand_color
    default_prompt = source_interface.get("default_prompt")
    skill_token = f"${candidate.name}"
    if isinstance(default_prompt, str) and default_prompt.strip():
        normalized_prompt = collapse_ws(default_prompt)
        if skill_token not in normalized_prompt:
            normalized_prompt = f"Use {skill_token} for this task. {normalized_prompt}"
        interface["default_prompt"] = _truncate(normalized_prompt, MAX_DEFAULT_PROMPT)
    elif explicit_only:
        interface["default_prompt"] = _truncate(
            f"Use ${candidate.name} for this task.",
            MAX_DEFAULT_PROMPT,
        )

    payload: dict[str, Any] = {"interface": interface}
    source_policy = source.get("policy") if isinstance(source.get("policy"), dict) else {}
    existing_allow = source_policy.get("allow_implicit_invocation")
    if explicit_only:
        payload["policy"] = {"allow_implicit_invocation": False}
    elif isinstance(existing_allow, bool):
        payload["policy"] = {"allow_implicit_invocation": existing_allow}
    source_dependencies = source.get("dependencies")
    if isinstance(source_dependencies, dict) and "tools" in source_dependencies:
        payload["dependencies"] = {"tools": source_dependencies["tools"]}
    return payload


def _normalize_openai_skill_metadata(
    destination: Path,
    candidate: SkillCandidate,
) -> list[dict[str, Any]]:
    """Apply only format-required OpenAI metadata normalization.

    Source snapshots and their hashes remain unchanged. Any generated-copy
    change is returned for the conversion report.
    """

    adaptations: list[dict[str, Any]] = []
    skill_md = destination / "SKILL.md"
    disable_value = candidate.manifest.get("disable-model-invocation")
    if disable_value is None:
        disable_value = candidate.manifest.get("disable_model_invocation")
    explicit_only = disable_value is True
    original_hash = sha256_file(skill_md)
    with skill_md.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    lines = text.splitlines(keepends=True)
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() in {"---", "..."}),
        None,
    )
    skill_changes: list[str] = []
    skill_reasons: list[str] = []
    if closing is not None and lines[closing].strip() == "...":
        ending = "\r\n" if lines[closing].endswith("\r\n") else "\n" if lines[closing].endswith("\n") else ""
        lines[closing] = "---" + ending
        skill_changes.append("normalized the front-matter closing delimiter from ... to ---")
        skill_reasons.append(
            "the OpenAI Plugin validator bundled with the tested Codex environment accepts the --- closing delimiter"
        )
    if explicit_only:
        changed_invocation = False
        if closing is not None:
            for index in range(1, closing):
                ending = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
                content = lines[index][:-len(ending)] if ending else lines[index]
                match = _DISABLE_INVOCATION_LINE.fullmatch(content)
                if match:
                    lines[index] = (
                        match.group("key")
                        + match.group("separator")
                        + "false"
                        + match.group("comment")
                        + ending
                    )
                    changed_invocation = True
                    break
        if not changed_invocation:
            raise SkillToPluginError(
                "The bundled OpenAI Plugin validator rejects `disable-model-invocation: true`, but the source form could not be normalized with the tool's narrow scalar-line adaptation.",
                code="package_validation_failed",
                details={"skill": candidate.name, "path": candidate.path},
            )
        skill_changes.append("set disable-model-invocation to false")
        skill_reasons.append(
            "the OpenAI Plugin validator bundled with the tested Codex environment rejected true on 2026-08-29"
        )
    if skill_changes:
        atomic_write_text(skill_md, "".join(lines))
        adaptations.append({
            "skill": candidate.name,
            "path": f"skills/{candidate.name}/SKILL.md",
            "change": "; ".join(skill_changes) + " in generated copy",
            "reason": "; ".join(skill_reasons),
            "source_sha256": original_hash,
            "generated_sha256": sha256_file(skill_md),
        })

    agent_yaml = destination / "agents" / "openai.yaml"
    source_payload: dict[str, Any] | None = None
    source_hash: str | None = None
    if agent_yaml.is_file():
        source_hash = sha256_file(agent_yaml)
        try:
            loaded = yaml.safe_load(agent_yaml.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SkillToPluginError(
                "Existing `agents/openai.yaml` is invalid and cannot be normalized safely.",
                code="package_validation_failed",
                details={"skill": candidate.name},
            ) from exc
        if not isinstance(loaded, dict):
            raise SkillToPluginError(
                "Existing `agents/openai.yaml` must contain a mapping.",
                code="package_validation_failed",
                details={"skill": candidate.name},
            )
        source_payload = loaded

    if source_payload is not None or explicit_only:
        normalized = _generated_agent_payload(candidate, source_payload, explicit_only=explicit_only)
        if source_payload != normalized:
            source_fields = _leaf_field_paths(source_payload or {})
            generated_fields = _leaf_field_paths(normalized)
            agent_yaml.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                agent_yaml,
                yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True),
            )
            adaptations.append({
                "skill": candidate.name,
                "path": f"skills/{candidate.name}/agents/openai.yaml",
                "change": "generated agent metadata from the tool's OpenAI compatibility allowlist"
                if source_payload is None else "normalized agent metadata to the tool's OpenAI compatibility allowlist",
                "reason": "represent the source's explicit-invocation intent with the tool's conservative OpenAI metadata allowlist"
                if explicit_only else "apply the tool's conservative OpenAI metadata allowlist",
                "source_sha256": source_hash,
                "generated_sha256": sha256_file(agent_yaml),
                "added_fields": sorted(set(generated_fields) - set(source_fields)),
                "removed_fields": sorted(set(source_fields) - set(generated_fields)),
                "changed_fields": sorted(
                    path for path in set(source_fields) & set(generated_fields)
                    if source_fields[path] != generated_fields[path]
                ),
            })
    return adaptations


def _validate_openai_skill_metadata(skill_root: Path) -> None:
    skill_md = skill_root / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n(?:---|\.\.\.)", text, re.DOTALL)
    if not match:
        raise SkillToPluginError("Generated Skill front matter is missing.", code="package_validation_failed")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillToPluginError("Generated Skill front matter is invalid YAML.", code="package_validation_failed") from exc
    if not isinstance(frontmatter, dict):
        raise SkillToPluginError("Generated Skill front matter must be a mapping.", code="package_validation_failed")
    skill_name = frontmatter.get("name")
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise SkillToPluginError("Generated Skill front matter name is missing.", code="package_validation_failed")
    disable_value = frontmatter.get("disable-model-invocation", frontmatter.get("disable_model_invocation"))
    if disable_value not in (None, False):
        raise SkillToPluginError(
            "Generated Skill has an OpenAI-incompatible invocation flag.",
            code="package_validation_failed",
        )
    agent_yaml = skill_root / "agents" / "openai.yaml"
    if not agent_yaml.is_file():
        return
    try:
        payload = yaml.safe_load(agent_yaml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SkillToPluginError("Generated OpenAI agent metadata is invalid.", code="package_validation_failed") from exc
    if not isinstance(payload, dict) or set(payload) - {"interface", "policy", "dependencies"}:
        raise SkillToPluginError("Generated OpenAI agent metadata has unsupported fields.", code="package_validation_failed")
    interface = payload.get("interface")
    allowed_interface = {"display_name", "short_description", "icon_small", "icon_large", "brand_color", "default_prompt"}
    if not isinstance(interface, dict) or set(interface) - allowed_interface:
        raise SkillToPluginError("Generated OpenAI agent interface is invalid.", code="package_validation_failed")
    if not all(isinstance(interface.get(key), str) and interface[key].strip() for key in ("display_name", "short_description")):
        raise SkillToPluginError("Generated OpenAI agent interface is incomplete.", code="package_validation_failed")
    short_description = interface["short_description"]
    if not MIN_SKILL_SHORT_DESCRIPTION <= len(short_description) <= MAX_SKILL_SHORT_DESCRIPTION:
        raise SkillToPluginError("Generated OpenAI agent short description is outside the 25-64 character guidance.", code="package_validation_failed")
    default_prompt = interface.get("default_prompt")
    if default_prompt is not None and (
        not isinstance(default_prompt, str)
        or not default_prompt.strip()
        or f"${skill_name}" not in default_prompt
    ):
        raise SkillToPluginError("Generated OpenAI agent default prompt must mention the Skill token.", code="package_validation_failed")
    plugin_root = skill_root.parent.parent.resolve()
    for key in ("icon_small", "icon_large"):
        raw_path = interface.get(key)
        if raw_path is None:
            continue
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise SkillToPluginError("Generated OpenAI agent icon path is empty.", code="package_validation_failed")
        relative = PurePosixPath(raw_path.replace("\\", "/"))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise SkillToPluginError("Generated OpenAI agent icon path escapes the Plugin.", code="package_validation_failed")
        icon_path = (skill_root / relative.as_posix()).resolve()
        try:
            icon_path.relative_to(plugin_root)
        except ValueError as exc:
            raise SkillToPluginError("Generated OpenAI agent icon path escapes the Plugin.", code="package_validation_failed") from exc
        if not icon_path.is_file():
            raise SkillToPluginError("Generated OpenAI agent icon path is missing.", code="package_validation_failed")
    policy = payload.get("policy")
    if policy is not None and (
        not isinstance(policy, dict)
        or set(policy) - {"allow_implicit_invocation"}
        or not isinstance(policy.get("allow_implicit_invocation"), bool)
    ):
        raise SkillToPluginError("Generated OpenAI agent policy is invalid.", code="package_validation_failed")


def _validate_plugin_tree(plugin_dir: Path, plugin_name: str, expected_skills: set[str]) -> dict[str, int]:
    manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillToPluginError(f"Generated plugin manifest is invalid: {exc}", code="package_validation_failed") from exc
    if manifest.get("name") != plugin_name or manifest.get("skills") != "./skills/":
        raise SkillToPluginError("Generated plugin manifest does not match its directory.", code="package_validation_failed")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", plugin_name) or len(plugin_name) > MAX_PLUGIN_NAME:
        raise SkillToPluginError("Generated plugin name is invalid.", code="package_validation_failed")
    actual_skills = {
        child.name for child in (plugin_dir / "skills").iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    }
    if actual_skills != expected_skills:
        raise SkillToPluginError(
            "Generated plugin skill set differs from the selected candidates.",
            code="package_validation_failed",
            details={"expected": sorted(expected_skills), "actual": sorted(actual_skills)},
        )
    for skill_name in sorted(actual_skills):
        _validate_openai_skill_metadata(plugin_dir / "skills" / skill_name)
    normalized: dict[str, tuple[str, bool]] = {}
    file_count = 0
    total_bytes = 0
    for file_path in iter_regular_files(plugin_dir):
        relative = (Path(plugin_name) / file_path.relative_to(plugin_dir)).as_posix()
        for part in Path(relative).parts:
            validate_path_segment(part, rendered_path=relative)
        key = normalized_path_key(relative)
        if key in normalized and normalized[key][0] != relative:
            raise SkillToPluginError("Generated paths collide after case/Unicode normalization.", code="package_validation_failed")
        normalized[key] = (relative, True)
        size = file_path.stat().st_size
        if size > MAX_MEMBER_BYTES:
            raise SkillToPluginError("Generated plugin has an oversized file.", code="package_validation_failed")
        file_count += 1
        total_bytes += size
    if file_count > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
        raise SkillToPluginError("Generated plugin exceeds packaging limits.", code="package_validation_failed")
    return {"file_count": file_count, "total_bytes": total_bytes, "skill_count": len(actual_skills)}


def write_deterministic_zip(plugin_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in sorted(iter_regular_files(plugin_dir), key=lambda item: item.relative_to(plugin_dir).as_posix()):
            arcname = f"{plugin_dir.name}/{file_path.relative_to(plugin_dir).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            info.flag_bits |= 0x800
            with file_path.open("rb") as handle:
                archive.writestr(info, handle.read())
    if zip_path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise SkillToPluginError("Generated ZIP exceeds the compressed-size limit.", code="package_validation_failed")
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if not names or len(names) != len(set(names)) or any(not name.startswith(plugin_dir.name + "/") for name in names):
            raise SkillToPluginError("Generated ZIP does not contain exactly one plugin namespace.", code="package_validation_failed")
        if any(info.file_size > MAX_MEMBER_BYTES for info in archive.infolist()):
            raise SkillToPluginError("Generated ZIP contains an oversized member.", code="package_validation_failed")


def _read_marketplace(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"name": "converted-skills", "interface": {"displayName": "Converted Skills"}, "plugins": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillToPluginError(f"Existing marketplace is invalid: {exc}", code="package_validation_failed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise SkillToPluginError("Existing marketplace has an unsupported structure.", code="package_validation_failed")
    return data


def _marketplace_with_entry(path: Path, plugin_name: str, *, force: bool) -> dict[str, Any]:
    data = _read_marketplace(path)
    entry = {
        "name": plugin_name,
        "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    matches = [index for index, item in enumerate(data["plugins"]) if isinstance(item, dict) and item.get("name") == plugin_name]
    if len(matches) > 1:
        raise SkillToPluginError("Marketplace contains duplicate plugin entries.", code="package_validation_failed")
    if matches:
        if not force:
            raise SkillToPluginError("Marketplace already contains the plugin name.", code="output_conflict")
        data["plugins"][matches[0]] = entry
    else:
        data["plugins"].append(entry)
    return data


def _marketplace_add_command(output_root: Path) -> str:
    return f'codex plugin marketplace add "{str(output_root).replace(chr(34), chr(92) + chr(34))}"'


def _copy_license_files(snapshot: Path, plugin_dir: Path, findings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    target_root = plugin_dir / "THIRD_PARTY_LICENSES"
    used: set[str] = set()
    for finding in findings:
        source_path = finding.get("path")
        if not source_path:
            continue
        source = ensure_within(snapshot / Path(str(source_path)), snapshot)
        if not source.is_file():
            continue
        target_name = slugify(Path(str(source_path)).as_posix().replace("/", "-"), "license")
        suffix = source.suffix if source.suffix else ".txt"
        if not target_name.casefold().endswith(suffix.casefold()):
            target_name += suffix
        if len(target_name) > 64:
            stem_limit = 64 - len(suffix) - 13
            target_name = (
                target_name[:stem_limit].rstrip("-.")
                + "-"
                + sha256_file(source)[:12]
                + suffix
            )
        base = target_name
        counter = 2
        while target_name.casefold() in used:
            target_name = f"{Path(base).stem}-{counter}{Path(base).suffix}"
            counter += 1
        used.add(target_name.casefold())
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_root / target_name)
        item = dict(finding)
        item["bundled_as"] = f"THIRD_PARTY_LICENSES/{target_name}"
        copied.append(item)
    copied.extend(dict(item) for item in findings if not item.get("path"))
    return copied


def _copy_tree_checked(source: Path, destination: Path) -> None:
    """Copy a previously validated tree while rechecking link races."""
    if destination.exists():
        raise SkillToPluginError(f"Copy destination already exists: `{destination}`.", code="package_validation_failed")
    destination.mkdir(parents=True)
    for file_path in iter_regular_files(source):
        relative = file_path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)


def _copy_external_references(
    snapshot: Path,
    plugin_dir: Path,
    plans: dict[str, Sequence[Any]],
) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    destination_hashes: dict[str, str] = {}
    for candidate_id in sorted(plans):
        for reference in plans[candidate_id]:
            source = ensure_within(snapshot / Path(reference.source_path), snapshot)
            destination = ensure_within(plugin_dir / Path(reference.destination_path), plugin_dir)
            key = normalized_path_key(destination.relative_to(plugin_dir).as_posix())
            if destination.exists():
                actual = hash_tree(destination) if destination.is_dir() else sha256_file(destination)
                if actual != reference.sha256:
                    raise SkillToPluginError(
                        "External reference collides with different generated content.",
                        code="package_validation_failed",
                        details={"destination": reference.destination_path},
                    )
            elif reference.is_directory:
                _copy_tree_checked(source, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            destination_hashes[key] = reference.sha256 or ""
            copied.append({
                "candidate_id": candidate_id,
                "referenced_from": reference.referenced_from,
                "raw_reference": reference.raw_reference,
                "source_path": reference.source_path,
                "destination_path": reference.destination_path,
                "sha256": reference.sha256,
            })
    return copied


def _commit_artifacts(
    staging: Path,
    output_root: Path,
    plugin_name: str,
    *,
    force: bool,
    marketplace_data: dict[str, Any],
) -> None:
    targets = [
        (staging / "plugin", output_root / "plugins" / plugin_name),
        (staging / "package.zip", output_root / "packages" / f"{plugin_name}.zip"),
        (staging / "report.json", output_root / "reports" / f"{plugin_name}.json"),
        (staging / "report.md", output_root / "reports" / f"{plugin_name}.md"),
    ]
    backup_root = staging / "backups"
    moved_new: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for _source, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not force:
                    raise SkillToPluginError(f"Refusing to overwrite `{target}`.", code="output_conflict")
                backup = backup_root / str(len(backups))
                backup.parent.mkdir(parents=True, exist_ok=True)
                target.replace(backup)
                backups.append((backup, target))
        for source, target in targets:
            source.replace(target)
            moved_new.append(target)
        marketplace_file = output_root / ".agents" / "plugins" / "marketplace.json"
        atomic_write_text(marketplace_file, json.dumps(marketplace_data, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        for target in reversed(moved_new):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        for backup, target in reversed(backups):
            if backup.exists():
                backup.replace(target)
        raise


def _package_selected_locked(
    state: ResolutionState,
    candidates: Sequence[SkillCandidate],
    *,
    output_root: Path,
    provenance: Provenance,
    diagnostics: Sequence[Diagnostic] = (),
    license_findings: Sequence[dict[str, Any]] = (),
    external_references: dict[str, Sequence[Any]] | None = None,
    requested_plugin_name: str | None = None,
    display_name: str | None = None,
    author_name: str = "Local conversion",
    version: str = DEFAULT_PLUGIN_VERSION,
    force: bool = False,
) -> ConversionResult:
    if not candidates or any(not candidate.valid or not candidate.name or not candidate.description for candidate in candidates):
        raise SkillToPluginError("Only valid Skill candidates can be packaged.", code="package_validation_failed")
    names = [candidate.name for candidate in candidates]
    if len(set(names)) != len(names):
        raise SkillToPluginError("Selected Skills contain duplicate names.", code="package_validation_failed", details={"names": names})

    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    snapshot = ensure_within(Path(state.resolved_source.snapshot_path), output_root / "resolutions", code="resolution_integrity_failed")
    plugin_name, had_collision = choose_plugin_name(state, candidates, output_root, requested_plugin_name, force=force)
    for name in names:
        if len(f"{plugin_name}:{name}") > MAX_COMBINED_IDENTITY:
            raise SkillToPluginError("Plugin and Skill identity exceeds 64 characters.", code="package_validation_failed")

    # Keep transient paths short enough for Windows hosts whose long-path
    # policy is disabled. The random namespace and output lock still prevent
    # collisions; reports retain the full immutable hashes.
    staging = output_root / f".asp-{uuid.uuid4().hex[:12]}"
    if staging.exists():
        raise SkillToPluginError("Unexpected staging collision.", code="output_conflict")
    staging.mkdir()
    plugin_stage = staging / "plugin"
    plugin_stage.mkdir()
    (plugin_stage / ".codex-plugin").mkdir()
    (plugin_stage / "skills").mkdir()
    selected: list[SelectedSkill] = []
    compatibility_adaptations: list[dict[str, Any]] = []
    warnings = [diagnostic.message for diagnostic in diagnostics if diagnostic.severity in {"warning", "error"}]
    if had_collision and not force:
        warnings.append(f"An existing artifact name collided; generated `{plugin_name}` without overwriting it.")
    try:
        for candidate in candidates:
            skill_dir = _candidate_skill_dir(snapshot, candidate)
            destination = plugin_stage / "skills" / str(candidate.name)
            _copy_tree_checked(skill_dir, destination)
            compatibility_adaptations.extend(
                _normalize_openai_skill_metadata(destination, candidate)
            )
            hashes = file_hashes(skill_dir)
            selected.append(
                SelectedSkill(
                    candidate_id=candidate.id,
                    name=str(candidate.name),
                    description=str(candidate.description),
                    path=candidate.path,
                    tree_sha256=hash_tree(skill_dir),
                    file_hashes=hashes,
                )
            )
            if skill_dir.name != candidate.name:
                warnings.append(f"`{skill_dir.name}` was copied as `{candidate.name}` to match SKILL.md front matter.")

        if compatibility_adaptations:
            adapted_skills = ", ".join(
                sorted({str(item["skill"]) for item in compatibility_adaptations})
            )
            warnings.append(
                "OpenAI compatibility metadata was normalized in the generated copy "
                f"for: {adapted_skills}. Source snapshots were not changed; see the conversion report."
            )

        copied_external_references = _copy_external_references(
            snapshot, plugin_stage, external_references or {},
        )
        bundled_licenses = _copy_license_files(snapshot, plugin_stage, license_findings)
        manifest = build_manifest(
            plugin_name, candidates, display_name=display_name, author_name=author_name, version=version,
        )
        atomic_write_text(
            plugin_stage / ".codex-plugin" / "plugin.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        checks = _validate_plugin_tree(plugin_stage, plugin_name, set(names))
        # Staging directory is intentionally named "plugin"; rename before ZIP so
        # the archive namespace and final folder name are identical.
        named_plugin_stage = staging / plugin_name
        plugin_stage.replace(named_plugin_stage)
        plugin_stage = named_plugin_stage
        plugin_tree_sha256 = hash_tree(plugin_stage)
        zip_stage = staging / "package.zip"
        write_deterministic_zip(plugin_stage, zip_stage)
        zip_sha256 = sha256_file(zip_stage)

        final_plugin = output_root / "plugins" / plugin_name
        final_zip = output_root / "packages" / f"{plugin_name}.zip"
        final_report_json = output_root / "reports" / f"{plugin_name}.json"
        final_report_md = output_root / "reports" / f"{plugin_name}.md"
        marketplace_file = output_root / ".agents" / "plugins" / "marketplace.json"
        marketplace_data = _marketplace_with_entry(marketplace_file, plugin_name, force=force)
        provenance_data = asdict(provenance)
        provenance_data["license_findings"] = bundled_licenses
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "created_at": utc_now(),
            "plugin_name": plugin_name,
            "plugin_dir": str(final_plugin),
            "zip_path": str(final_zip),
            "zip_sha256": zip_sha256,
            "plugin_tree_sha256": plugin_tree_sha256,
            "marketplace_root": str(output_root),
            "marketplace_file": str(marketplace_file),
            "marketplace_add_command": _marketplace_add_command(output_root),
            "report_json": str(final_report_json),
            "report_markdown": str(final_report_md),
            "resolution_id": state.resolution_id,
            "resolution_file": state.resolution_file,
            "checks": checks,
            "skills": [asdict(item) for item in selected],
            "provenance": provenance_data,
            "external_references": copied_external_references,
            "compatibility_adaptations": compatibility_adaptations,
            "diagnostics": [asdict(item) for item in diagnostics],
            "warnings": list(dict.fromkeys(warnings)),
        }
        write_reports(staging / "report.json", staging / "report.md", report)
        plugin_stage.replace(staging / "plugin")
        _commit_artifacts(
            staging, output_root, plugin_name, force=force, marketplace_data=marketplace_data,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    final_provenance = Provenance(**{**asdict(provenance), "license_findings": tuple(bundled_licenses)})
    return ConversionResult(
        plugin_name=plugin_name,
        plugin_dir=str(final_plugin),
        zip_path=str(final_zip),
        zip_sha256=zip_sha256,
        marketplace_root=str(output_root),
        marketplace_file=str(marketplace_file),
        marketplace_add_command=_marketplace_add_command(output_root),
        report_json=str(final_report_json),
        report_markdown=str(final_report_md),
        skills=tuple(selected),
        provenance=final_provenance,
        plugin_tree_sha256=plugin_tree_sha256,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def package_selected(
    state: ResolutionState,
    candidates: Sequence[SkillCandidate],
    *,
    output_root: Path,
    provenance: Provenance,
    diagnostics: Sequence[Diagnostic] = (),
    license_findings: Sequence[dict[str, Any]] = (),
    external_references: dict[str, Sequence[Any]] | None = None,
    requested_plugin_name: str | None = None,
    display_name: str | None = None,
    author_name: str = "Local conversion",
    version: str = DEFAULT_PLUGIN_VERSION,
    force: bool = False,
) -> ConversionResult:
    """Package under one output-root lock from conflict checks through commit."""

    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    with _OutputRootLock(output_root):
        return _package_selected_locked(
            state,
            candidates,
            output_root=output_root,
            provenance=provenance,
            diagnostics=diagnostics,
            license_findings=license_findings,
            external_references=external_references,
            requested_plugin_name=requested_plugin_name,
            display_name=display_name,
            author_name=author_name,
            version=version,
            force=force,
        )
