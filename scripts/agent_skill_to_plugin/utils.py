"""Small deterministic helpers with no network or subprocess side effects."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, Iterable
import unicodedata
import urllib.parse

from .errors import SkillToPluginError
from .limits import MAX_REPORT_TEXT


SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "auth", "authorization", "api_key", "apikey",
    "key", "signature", "sig", "password", "passwd",
}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,}|npm_[A-Za-z0-9]{20,})\b"
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def sanitize_text(value: str, limit: int = MAX_REPORT_TEXT) -> str:
    value = re.sub(r"(?i)\b(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1***:***@", value)
    value = re.sub(
        r"(?i)([?&](?:token|access_token|auth|authorization|api_key|apikey|key|signature|sig|password)=)[^&\s]+",
        r"\1***",
        value,
    )
    value = TOKEN_PATTERN.sub("***REDACTED_TOKEN***", value)
    return value if len(value) <= limit else value[:limit] + "\n…[truncated]"


def validate_url_credentials(value: str, *, allow_username: bool = False) -> None:
    if TOKEN_PATTERN.search(value):
        raise SkillToPluginError(
            "The source appears to contain an access token. Use an existing credential helper or SSH configuration instead.",
            code="security_rejected",
        )
    parsed = urllib.parse.urlsplit(value)
    if parsed.password is not None or (parsed.username is not None and not allow_username):
        raise SkillToPluginError("URLs with embedded credentials are not allowed.", code="security_rejected")
    sensitive = sorted(
        key for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() in SENSITIVE_QUERY_KEYS
    )
    if sensitive:
        raise SkillToPluginError(
            "Credential-like URL query parameters are not allowed.",
            code="security_rejected",
            details={"parameters": sensitive},
        )


def slugify(value: str, fallback: str = "skill") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or fallback


def validate_path_segment(segment: str, *, rendered_path: str) -> None:
    if not segment or segment in {".", ".."}:
        raise SkillToPluginError(f"Unsafe path segment in `{rendered_path}`.", code="security_rejected")
    if segment != segment.strip() or segment.endswith((".", " ")):
        raise SkillToPluginError(f"Path has Windows-invalid outer whitespace or trailing dot: `{rendered_path}`.", code="security_rejected")
    if "\\" in segment or any(ord(char) < 32 or ord(char) == 127 for char in segment):
        raise SkillToPluginError(f"Path contains a backslash or control character: `{rendered_path}`.", code="security_rejected")
    stem = segment.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED:
        raise SkillToPluginError(f"Path uses a Windows reserved name: `{rendered_path}`.", code="security_rejected")


def safe_posix_relative(value: str) -> PurePosixPath:
    if "\x00" in value:
        raise SkillToPluginError("NUL bytes are not allowed in paths.", code="security_rejected")
    value = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", value) or value.startswith(("/", "//")):
        raise SkillToPluginError(f"Absolute archive path is not allowed: `{sanitize_text(value)}`.", code="security_rejected")
    path = PurePosixPath(value)
    if not path.parts:
        raise SkillToPluginError("Empty archive path is not allowed.", code="security_rejected")
    rendered = path.as_posix()
    for part in path.parts:
        validate_path_segment(part, rendered_path=rendered)
    return path


def normalized_path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def ensure_within(path: Path, root: Path, *, code: str = "security_rejected") -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SkillToPluginError(
            f"Path escapes the allowed root: `{sanitize_text(str(path))}`.",
            code=code,
            details={"root": str(resolved_root)},
        ) from exc
    return resolved


def iter_regular_files(root: Path) -> Iterable[Path]:
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            child = current_path / dirname
            if child.is_symlink():
                raise SkillToPluginError(f"Symbolic links are not allowed: `{child}`.", code="security_rejected")
        for filename in filenames:
            child = current_path / filename
            if child.is_symlink():
                raise SkillToPluginError(f"Symbolic links are not allowed: `{child}`.", code="security_rejected")
            mode = child.stat().st_mode
            if not stat.S_ISREG(mode):
                raise SkillToPluginError(f"Special files are not allowed: `{child}`.", code="security_rejected")
            yield child


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(iter_regular_files(root), key=lambda item: item.relative_to(root).as_posix())
    }


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, file_hash in file_hashes(root).items():
        size = (root / PurePosixPath(relative)).stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n")
