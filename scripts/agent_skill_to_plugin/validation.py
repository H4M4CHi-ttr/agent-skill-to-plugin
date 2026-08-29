"""Manifest, filesystem, resolution-state, and reference validation.

All repository and Skill content handled here is untrusted data.  This module
never executes imported files and never performs network access.  In
particular, resolution resumption is deliberately limited to re-validating the
snapshot already recorded in the resolution file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable
import urllib.parse

import yaml

from .errors import SkillToPluginError
from .limits import (
    MAX_DEPTH,
    MAX_FILES,
    MAX_MEMBER_BYTES,
    MAX_PATH_CHARS,
    MAX_SKILL_DESCRIPTION,
    MAX_SKILL_NAME,
    MAX_TOTAL_BYTES,
    SCHEMA_VERSION,
)
from .models import Diagnostic, ResolutionState, SkillCandidate
from .utils import ensure_within, normalized_path_key, sanitize_text, sha256_file, validate_path_segment


SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:(?:RSA|EC|OPENSSH|DSA|ENCRYPTED) PRIVATE KEY|PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
)
WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
MAX_SKILL_MANIFEST_BYTES = 1024 * 1024
MAX_YAML_DATA_NODES = 10_000
_FILE_SCAN_CHUNK_BYTES = 1024 * 1024
_PRIVATE_KEY_SCAN_OVERLAP_BYTES = 128
SECRET_LIKE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "auth.json",
        "client_secret.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "private-key.pem",
        "private_key.pem",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
SECRET_LIKE_RE = re.compile(
    r"(?:^|[._-])(?:client[_-]?secret|credentials?|private[_-]?key|secrets?)(?:[._-]|$)",
    re.IGNORECASE,
)
MARKDOWN_REFERENCE_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))",
    re.MULTILINE,
)
HTML_REFERENCE_RE = re.compile(
    r"\b(?:href|src)\s*=\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)')",
    re.IGNORECASE,
)
PLAIN_RELATIVE_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])"
    r"(?P<relative>\.{1,2}[\\/](?:%[0-9A-Fa-f]{2}|[^\s`'\"<>()\[\]{},;:!?])+)",
    re.MULTILINE,
)


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that diagnoses duplicate explicit mapping keys."""

    def construct_mapping(self, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=False)
            # YAML merge keys intentionally provide defaults that explicit
            # mapping keys may override; they are not duplicate declarations.
            if key == "<<":
                continue
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class ManifestValidation:
    """Parsed SKILL.md front matter and diagnostics.

    Invalid front matter is a discovery result, not an exception: callers use
    this object to retain the path as ``SkillCandidate(valid=False)``.
    """

    valid: bool
    name: str | None
    description: str | None
    manifest: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class TreeValidation:
    root: str
    file_count: int
    total_bytes: int
    file_hashes: dict[str, str]
    tree_sha256: str
    warnings: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class ExternalReference:
    """A safe copy plan for one reference outside the Skill directory."""

    referenced_from: str
    raw_reference: str
    source_path: str
    destination_path: str
    is_directory: bool
    sha256: str | None = None


def _diagnostic(
    code: str,
    message: str,
    *,
    path: str | None = None,
    severity: str = "error",
    **details: Any,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=sanitize_text(message),
        severity=severity,
        path=path,
        details={key: sanitize_text(value) if isinstance(value, str) else value for key, value in details.items()},
    )


def _json_safe(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> Any:
    """Convert safe-loaded YAML into bounded JSON-compatible data.

    PyYAML permits aliases which can form recursive Python containers.  The
    resolution file must not serialize such structures, so cycles and extreme
    nesting are rejected deterministically.
    """

    if _depth > 50:
        raise ValueError("YAML nesting exceeds 50 levels")
    budget = _budget if _budget is not None else [MAX_YAML_DATA_NODES]
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError(f"YAML data exceeds {MAX_YAML_DATA_NODES} values")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()

    seen = _seen if _seen is not None else set()
    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            raise ValueError("recursive YAML aliases are not supported")
        seen.add(marker)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("front matter keys must be strings")
                result[key] = _json_safe(item, _seen=seen, _depth=_depth + 1, _budget=budget)
            return result
        finally:
            seen.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            raise ValueError("recursive YAML aliases are not supported")
        seen.add(marker)
        try:
            return [_json_safe(item, _seen=seen, _depth=_depth + 1, _budget=budget) for item in value]
        finally:
            seen.remove(marker)
    raise ValueError(f"unsupported YAML value type: {type(value).__name__}")


def parse_skill_manifest(skill_md: Path, *, display_path: str | None = None) -> ManifestValidation:
    """Parse a SKILL.md with :func:`yaml.safe_load` and diagnostic failures."""

    rendered = display_path or str(skill_md)
    diagnostics: list[Diagnostic] = []
    try:
        if skill_md.stat().st_size > MAX_SKILL_MANIFEST_BYTES:
            diagnostics.append(
                _diagnostic(
                    "skill_manifest_too_large",
                    f"SKILL.md exceeds the {MAX_SKILL_MANIFEST_BYTES}-byte parser limit.",
                    path=rendered,
                )
            )
            return ManifestValidation(False, None, None, diagnostics=tuple(diagnostics))
        # UTF-8 BOM is permitted as a transport marker but is not part of the
        # front-matter delimiter.
        text = skill_md.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        diagnostics.append(
            _diagnostic(
                "skill_manifest_not_utf8",
                "SKILL.md is not valid UTF-8.",
                path=rendered,
                line=exc.start,
            )
        )
        return ManifestValidation(False, None, None, diagnostics=tuple(diagnostics))
    except OSError as exc:
        diagnostics.append(
            _diagnostic("skill_manifest_unreadable", f"Could not read SKILL.md: {exc}", path=rendered)
        )
        return ManifestValidation(False, None, None, diagnostics=tuple(diagnostics))

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        diagnostics.append(
            _diagnostic(
                "skill_manifest_missing_frontmatter",
                "SKILL.md must start with YAML front matter delimited by `---`.",
                path=rendered,
            )
        )
        return ManifestValidation(False, None, None, body=text, diagnostics=tuple(diagnostics))

    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        diagnostics.append(
            _diagnostic(
                "skill_manifest_unclosed_frontmatter",
                "SKILL.md has unclosed YAML front matter.",
                path=rendered,
            )
        )
        return ManifestValidation(False, None, None, body=text, diagnostics=tuple(diagnostics))

    frontmatter_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :]).strip()
    try:
        # This SafeLoader subclass is the trust boundary for untrusted YAML.
        # It preserves ordinary YAML support while surfacing ambiguous duplicate
        # explicit keys rather than silently accepting the last value.
        loaded = yaml.load(frontmatter_text, Loader=_UniqueSafeLoader)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("front matter must be a YAML mapping")
        manifest = _json_safe(loaded)
    except (yaml.YAMLError, ValueError) as exc:
        diagnostics.append(
            _diagnostic(
                "skill_manifest_yaml_invalid",
                f"Could not parse SKILL.md YAML front matter: {exc}",
                path=rendered,
            )
        )
        return ManifestValidation(False, None, None, body=body, diagnostics=tuple(diagnostics))

    name_value = manifest.get("name")
    description_value = manifest.get("description")
    name = name_value.strip() if isinstance(name_value, str) else None
    description = description_value.strip() if isinstance(description_value, str) else None

    if not name:
        diagnostics.append(
            _diagnostic("skill_manifest_name_missing", "Front matter requires a non-empty string `name`.", path=rendered)
        )
    elif len(name) > MAX_SKILL_NAME or not SKILL_NAME_RE.fullmatch(name):
        diagnostics.append(
            _diagnostic(
                "skill_manifest_name_invalid",
                "Skill name must use lowercase letters, digits, and single hyphens and fit the configured limit.",
                path=rendered,
                value=name,
                max_length=MAX_SKILL_NAME,
            )
        )

    if not description:
        diagnostics.append(
            _diagnostic(
                "skill_manifest_description_missing",
                "Front matter requires a non-empty string `description`.",
                path=rendered,
            )
        )
    elif len(description) > MAX_SKILL_DESCRIPTION:
        diagnostics.append(
            _diagnostic(
                "skill_manifest_description_too_long",
                "Skill description exceeds the configured limit.",
                path=rendered,
                length=len(description),
                max_length=MAX_SKILL_DESCRIPTION,
            )
        )

    if not body:
        diagnostics.append(
            _diagnostic("skill_manifest_body_missing", "SKILL.md has no instruction body.", path=rendered)
        )

    return ManifestValidation(
        valid=not diagnostics,
        name=name,
        description=description,
        manifest=manifest,
        body=body,
        diagnostics=tuple(diagnostics),
    )


def _is_windows_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _validate_relative_path(relative: PurePosixPath) -> None:
    rendered = relative.as_posix()
    if len(rendered) > MAX_PATH_CHARS:
        raise SkillToPluginError(
            f"Imported path exceeds the {MAX_PATH_CHARS}-character limit: `{sanitize_text(rendered)}`.",
            code="security_rejected",
        )
    if len(relative.parts) > MAX_DEPTH:
        raise SkillToPluginError(
            f"Imported path exceeds the {MAX_DEPTH}-level depth limit: `{sanitize_text(rendered)}`.",
            code="security_rejected",
        )
    for part in relative.parts:
        validate_path_segment(part, rendered_path=rendered)
        if any(character in WINDOWS_INVALID_CHARS for character in part):
            raise SkillToPluginError(
                f"Path contains a Windows-invalid character: `{sanitize_text(rendered)}`.",
                code="security_rejected",
            )


def _register_path(
    relative: PurePosixPath,
    kind: str,
    seen: dict[str, tuple[str, str]],
) -> None:
    rendered = relative.as_posix()
    key = normalized_path_key(rendered)
    previous = seen.get(key)
    if previous is not None:
        previous_path, previous_kind = previous
        if previous_path != rendered or previous_kind != kind:
            raise SkillToPluginError(
                "Imported paths collide after case-insensitive Unicode NFC normalization.",
                code="security_rejected",
                details={
                    "first": previous_path,
                    "first_kind": previous_kind,
                    "second": rendered,
                    "second_kind": kind,
                },
            )
        return
    seen[key] = (rendered, kind)


def _is_secret_like(relative: PurePosixPath) -> bool:
    name = relative.name.casefold()
    return (
        name in SECRET_LIKE_NAMES
        or name.startswith(".env.")
        or bool(SECRET_LIKE_RE.search(name))
    )


def _canonical_tree_hash(file_hashes: dict[str, str], sizes: dict[str, int]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(file_hashes):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(sizes[relative]).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hashes[relative].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _hash_and_scan_private_key(path: Path) -> tuple[str, bool]:
    """Hash a file while scanning every byte for private-key armor.

    Retaining a small overlap ensures a marker split across read boundaries is
    still detected without loading a potentially large imported file into
    memory.
    """

    digest = hashlib.sha256()
    overlap = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_FILE_SCAN_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            scan_window = overlap + chunk
            if PRIVATE_KEY_RE.search(scan_window):
                return digest.hexdigest(), True
            overlap = scan_window[-_PRIVATE_KEY_SCAN_OVERLAP_BYTES:]
    return digest.hexdigest(), False


def validate_tree(root: Path) -> TreeValidation:
    """Validate a materialized tree before it is copied or packaged."""

    supplied_root = Path(root).absolute()
    try:
        supplied_stat = supplied_root.lstat()
    except OSError as exc:
        raise SkillToPluginError(
            f"Imported tree does not exist: `{sanitize_text(str(supplied_root))}`.",
            code="security_rejected",
        ) from exc
    if stat.S_ISLNK(supplied_stat.st_mode) or _is_windows_reparse(supplied_stat):
        raise SkillToPluginError(
            f"Imported tree root may not be a link or reparse point: `{supplied_root}`.",
            code="security_rejected",
        )
    root = supplied_root.resolve()
    if not root.is_dir():
        raise SkillToPluginError(f"Imported tree does not exist: `{sanitize_text(str(root))}`.", code="security_rejected")
    root_stat = root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or _is_windows_reparse(root_stat):
        raise SkillToPluginError(f"Imported tree root may not be a link or reparse point: `{root}`.", code="security_rejected")

    seen: dict[str, tuple[str, str]] = {}
    file_hash_map: dict[str, str] = {}
    sizes: dict[str, int] = {}
    warnings: list[Diagnostic] = []
    total_bytes = 0
    stack = [root]

    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: unicodedata_sort_key(item.name))
        except OSError as exc:
            raise SkillToPluginError(
                f"Could not inspect imported directory `{sanitize_text(str(directory))}`: {exc}",
                code="security_rejected",
            ) from exc

        for entry in entries:
            path = Path(entry.path)
            relative = PurePosixPath(path.relative_to(root).as_posix())
            _validate_relative_path(relative)
            try:
                item_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillToPluginError(
                    f"Could not inspect imported path `{sanitize_text(relative.as_posix())}`: {exc}",
                    code="security_rejected",
                ) from exc

            if entry.is_symlink() or stat.S_ISLNK(item_stat.st_mode) or _is_windows_reparse(item_stat):
                raise SkillToPluginError(
                    f"Symbolic links and Windows reparse points are not allowed: `{relative.as_posix()}`.",
                    code="security_rejected",
                )
            if stat.S_ISDIR(item_stat.st_mode):
                _register_path(relative, "directory", seen)
                stack.append(path)
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                raise SkillToPluginError(
                    f"Special files are not allowed: `{relative.as_posix()}`.",
                    code="security_rejected",
                )

            _register_path(relative, "file", seen)
            if _is_secret_like(relative):
                raise SkillToPluginError(
                    f"Refusing to package a likely credential file: `{relative.as_posix()}`.",
                    code="security_rejected",
                    details={"remediation": "Remove credentials and load them from the runtime environment."},
                )
            if item_stat.st_size > MAX_MEMBER_BYTES:
                raise SkillToPluginError(
                    f"Imported file exceeds the {MAX_MEMBER_BYTES}-byte member limit: `{relative.as_posix()}`.",
                    code="security_rejected",
                )

            total_bytes += item_stat.st_size
            if total_bytes > MAX_TOTAL_BYTES:
                raise SkillToPluginError(
                    f"Imported content exceeds the {MAX_TOTAL_BYTES}-byte total limit.",
                    code="security_rejected",
                )
            if len(file_hash_map) + 1 > MAX_FILES:
                raise SkillToPluginError(
                    f"Imported content exceeds the {MAX_FILES}-file limit.",
                    code="security_rejected",
                )

            try:
                file_hash, contains_private_key = _hash_and_scan_private_key(path)
            except OSError as exc:
                raise SkillToPluginError(
                    f"Could not inspect imported file `{relative.as_posix()}`: {exc}",
                    code="security_rejected",
                ) from exc
            if contains_private_key:
                raise SkillToPluginError(
                    f"Refusing to package private-key material found in `{relative.as_posix()}`.",
                    code="security_rejected",
                )

            relative_text = relative.as_posix()
            file_hash_map[relative_text] = file_hash
            sizes[relative_text] = item_stat.st_size
            try:
                after_stat = path.lstat()
            except OSError as exc:
                raise SkillToPluginError(
                    f"Imported file changed while it was being validated: `{relative_text}`.",
                    code="security_rejected",
                ) from exc
            if (
                stat.S_ISLNK(after_stat.st_mode)
                or _is_windows_reparse(after_stat)
                or after_stat.st_size != item_stat.st_size
                or getattr(after_stat, "st_mtime_ns", None) != getattr(item_stat, "st_mtime_ns", None)
            ):
                raise SkillToPluginError(
                    f"Imported file changed while it was being validated: `{relative_text}`.",
                    code="security_rejected",
                )
            if item_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                warnings.append(
                    _diagnostic(
                        "executable_file_included",
                        "Imported content includes an executable file; it was not run.",
                        path=relative_text,
                        severity="warning",
                    )
                )

    return TreeValidation(
        root=str(root),
        file_count=len(file_hash_map),
        total_bytes=total_bytes,
        file_hashes=dict(sorted(file_hash_map.items())),
        tree_sha256=_canonical_tree_hash(file_hash_map, sizes),
        warnings=tuple(warnings),
    )


def unicodedata_sort_key(value: str) -> tuple[str, str]:
    """Stable sorting across case-sensitive and case-insensitive platforms."""

    normalized = normalized_path_key(value)
    return normalized, value


def candidate_id_for(snapshot_sha256: str, candidate_path: str, manifest_sha256: str) -> str:
    payload = f"{snapshot_sha256}\0{candidate_path}\0{manifest_sha256}".encode("utf-8")
    return "skill-" + hashlib.sha256(payload).hexdigest()[:24]


def build_skill_candidate(
    snapshot_root: Path,
    skill_md: Path,
    *,
    snapshot_sha256: str,
    selection_reason: str,
    priority: int,
    plugin: str | None = None,
) -> SkillCandidate:
    """Build a selectable or invalid candidate without dropping parse errors."""

    root = snapshot_root.resolve()
    manifest_path = ensure_within(skill_md, root)
    relative_manifest = manifest_path.relative_to(root).as_posix()
    candidate_path = manifest_path.parent.relative_to(root).as_posix() or "."
    parsed = parse_skill_manifest(manifest_path, display_path=relative_manifest)
    diagnostics = list(parsed.diagnostics)

    if manifest_path.is_file():
        manifest_sha = sha256_file(manifest_path)
    else:
        manifest_sha = hashlib.sha256(b"missing-SKILL.md").hexdigest()

    # Nested boundaries are unusual but not categorically invalid in Agent
    # Skills. Preserve both discovery records and warn about the packaging
    # consequence instead of silently rejecting an otherwise valid manifest.
    if manifest_path.parent.is_dir():
        nested = sorted(
            (
                path.relative_to(root).as_posix()
                for path in manifest_path.parent.rglob("SKILL.md")
                if path.resolve() != manifest_path
            ),
            key=unicodedata_sort_key,
        )
        if nested:
            diagnostics.append(
                _diagnostic(
                    "nested_skill_manifest",
                    "This Skill directory contains nested SKILL.md files; selecting it copies those files as original resources.",
                    path=relative_manifest,
                    severity="warning",
                    nested=nested,
                )
            )

    valid = parsed.valid and not any(item.severity == "error" for item in diagnostics)
    return SkillCandidate(
        id=candidate_id_for(snapshot_sha256, candidate_path, manifest_sha),
        name=parsed.name,
        description=parsed.description,
        path=candidate_path,
        plugin=plugin,
        selection_reason=selection_reason,
        valid=valid,
        priority=priority,
        manifest=parsed.manifest,
        diagnostics=tuple(diagnostics),
    )


def _iter_reference_values(markdown: str) -> Iterable[str]:
    for match in MARKDOWN_REFERENCE_RE.finditer(markdown):
        value = match.group("angle") or match.group("plain")
        if value:
            yield value.strip()
    for match in HTML_REFERENCE_RE.finditer(markdown):
        value = match.group("double") or match.group("single")
        if value:
            yield value.strip()
    # Agent Skills commonly mention resources in inline code or command prose
    # rather than Markdown links (for example `python ../../scripts/check.py`).
    # Any path that can escape a Skill boundary necessarily starts with `../`;
    # capture those mechanically instead of emitting a knowingly broken copy.
    for match in PLAIN_RELATIVE_REFERENCE_RE.finditer(markdown):
        value = match.group("relative").rstrip(".,;:!?")
        if value:
            yield value


def _iter_manifest_reference_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield from _iter_reference_values(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_manifest_reference_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_manifest_reference_values(item)


def _plugin_destination(skill_name: str, reference: str) -> PurePosixPath:
    parts = ["skills", skill_name]
    for part in PurePosixPath(reference).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise SkillToPluginError("External reference escapes the generated plugin.", code="package_validation_failed")
            parts.pop()
            continue
        validate_path_segment(part, rendered_path=reference)
        if any(character in WINDOWS_INVALID_CHARS for character in part):
            raise SkillToPluginError(
                f"External reference contains a Windows-invalid path: `{sanitize_text(reference)}`.",
                code="package_validation_failed",
            )
        parts.append(part)
    if not parts:
        raise SkillToPluginError("External reference resolves outside the generated plugin.", code="package_validation_failed")
    return PurePosixPath(*parts)


def detect_external_references(
    skill_dir: Path,
    snapshot_root: Path,
    *,
    skill_name: str | None = None,
    diagnostics: list[Diagnostic] | None = None,
) -> tuple[ExternalReference, ...]:
    """Return safe copy plans for concrete relative references outside a Skill.

    Missing paths are reported because a reference may be a runtime output,
    template placeholder, or repository route rather than a bundled input.
    Links escaping the fixed source snapshot and paths that would escape the
    generated plugin still fail conversion. URLs, fragments, and variable
    expressions are compatibility concerns rather than local files and are
    intentionally ignored here.
    """

    root = snapshot_root.resolve()
    skill_root = ensure_within(skill_dir, root)
    skill_md = skill_root / "SKILL.md"
    parsed = parse_skill_manifest(skill_md, display_path=skill_md.relative_to(root).as_posix())
    if not parsed.valid or not parsed.name:
        raise SkillToPluginError(
            "External references cannot be resolved for an invalid Skill manifest.",
            code="invalid_manifest",
            details={"path": skill_md.relative_to(root).as_posix()},
        )
    resolved_name = skill_name or parsed.name
    references: dict[tuple[str, str], ExternalReference] = {}

    reference_values = list(_iter_reference_values(parsed.body))
    reference_values.extend(_iter_manifest_reference_values(parsed.manifest))
    for raw_reference in reference_values:
        if not raw_reference or raw_reference.startswith("#") or "${" in raw_reference:
            continue
        if "\\" in raw_reference:
            raise SkillToPluginError(
                f"Relative reference uses a non-portable backslash path: `{sanitize_text(raw_reference)}`.",
                code="package_validation_failed",
            )
        split = urllib.parse.urlsplit(raw_reference)
        if split.scheme or split.netloc:
            continue
        decoded_path = urllib.parse.unquote(split.path)
        if not decoded_path:
            continue
        if decoded_path.startswith("/") or re.match(r"^[A-Za-z]:", decoded_path):
            raise SkillToPluginError(
                f"Absolute local reference cannot be safely bundled: `{sanitize_text(raw_reference)}`.",
                code="package_validation_failed",
            )

        target = (skill_root / Path(*PurePosixPath(decoded_path).parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise SkillToPluginError(
                f"Relative reference escapes the fixed source snapshot: `{sanitize_text(raw_reference)}`.",
                code="package_validation_failed",
            ) from exc
        if not target.exists():
            if diagnostics is not None:
                diagnostics.append(
                    Diagnostic(
                        code="external_reference_missing",
                        message=f"Relative reference `{sanitize_text(raw_reference)}` did not exist in the fixed source snapshot and was not bundled; verify whether it is a runtime output, template placeholder, repository route, or missing dependency.",
                        severity="warning",
                        path=skill_md.relative_to(root).as_posix(),
                        details={"reference": sanitize_text(raw_reference)},
                    )
                )
            continue

        # References already inside the Skill are copied with the Skill itself.
        try:
            target.relative_to(skill_root)
            continue
        except ValueError:
            pass

        target_stat = target.lstat()
        if stat.S_ISLNK(target_stat.st_mode) or _is_windows_reparse(target_stat):
            raise SkillToPluginError(
                f"External reference resolves to a link or reparse point: `{sanitize_text(raw_reference)}`.",
                code="security_rejected",
            )
        if not (target.is_file() or target.is_dir()):
            raise SkillToPluginError(
                f"External reference is not a regular file or directory: `{sanitize_text(raw_reference)}`.",
                code="security_rejected",
            )

        destination = _plugin_destination(resolved_name, decoded_path)
        source_relative = target.relative_to(root).as_posix()
        reference = ExternalReference(
            referenced_from=skill_md.relative_to(root).as_posix(),
            raw_reference=raw_reference,
            source_path=source_relative,
            destination_path=destination.as_posix(),
            is_directory=target.is_dir(),
            sha256=sha256_file(target) if target.is_file() else validate_tree(target).tree_sha256,
        )
        references[(source_relative, destination.as_posix())] = reference

    return tuple(references[key] for key in sorted(references))


def _state_path(value: str, output_root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (output_root / path).resolve()


def validate_resolution_state(state: ResolutionState, resolution_file: Path) -> TreeValidation:
    """Validate a persisted resolution and its immutable snapshot offline."""

    if state.schema_version != SCHEMA_VERSION:
        raise SkillToPluginError(
            f"Unsupported resolution schema `{sanitize_text(state.schema_version)}`.",
            code="resolution_integrity_failed",
        )

    actual_resolution = resolution_file.resolve()
    output_root = Path(state.output_root)
    if not output_root.is_absolute():
        raise SkillToPluginError("Resolution output_root must be absolute.", code="resolution_integrity_failed")
    output_root = output_root.resolve()
    resolutions_root = output_root / "resolutions"
    ensure_within(actual_resolution, resolutions_root, code="resolution_integrity_failed")

    recorded_resolution = _state_path(state.resolution_file, output_root)
    if recorded_resolution != actual_resolution:
        raise SkillToPluginError(
            "Resolution file path does not match the path recorded in its state.",
            code="resolution_integrity_failed",
            details={"recorded": str(recorded_resolution), "actual": str(actual_resolution)},
        )

    snapshot = _state_path(state.resolved_source.snapshot_path, output_root)
    ensure_within(snapshot, resolutions_root, code="resolution_integrity_failed")
    if snapshot == output_root or not snapshot.is_dir():
        raise SkillToPluginError("Resolution snapshot is missing or invalid.", code="resolution_integrity_failed")

    tree = validate_tree(snapshot)
    if tree.tree_sha256 != state.resolved_source.snapshot_sha256:
        raise SkillToPluginError(
            "Resolution snapshot hash no longer matches the pinned snapshot.",
            code="resolution_integrity_failed",
            details={"expected": state.resolved_source.snapshot_sha256, "actual": tree.tree_sha256},
        )

    for candidate in state.candidates:
        candidate_dir = snapshot if candidate.path == "." else snapshot / Path(*PurePosixPath(candidate.path).parts)
        ensure_within(candidate_dir, snapshot, code="resolution_integrity_failed")
        manifest = candidate_dir / "SKILL.md"
        if not manifest.is_file():
            raise SkillToPluginError(
                "A candidate SKILL.md is missing from the pinned snapshot.",
                code="resolution_integrity_failed",
                details={"candidate_id": candidate.id, "path": candidate.path},
            )
        rebuilt = build_skill_candidate(
            snapshot,
            manifest,
            snapshot_sha256=tree.tree_sha256,
            selection_reason=candidate.selection_reason,
            priority=candidate.priority,
            plugin=candidate.plugin,
        )
        comparable_expected = (
            candidate.id,
            candidate.name,
            candidate.description,
            candidate.path,
            candidate.plugin,
            candidate.valid,
            candidate.manifest,
        )
        comparable_actual = (
            rebuilt.id,
            rebuilt.name,
            rebuilt.description,
            rebuilt.path,
            rebuilt.plugin,
            rebuilt.valid,
            rebuilt.manifest,
        )
        if comparable_expected != comparable_actual:
            raise SkillToPluginError(
                "A candidate no longer matches its persisted resolution record.",
                code="resolution_integrity_failed",
                details={"candidate_id": candidate.id, "path": candidate.path},
            )

    return tree


__all__ = [
    "ExternalReference",
    "ManifestValidation",
    "TreeValidation",
    "build_skill_candidate",
    "candidate_id_for",
    "detect_external_references",
    "parse_skill_manifest",
    "validate_resolution_state",
    "validate_tree",
]
