"""Fetch npm package contents without invoking npm or lifecycle scripts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import tempfile
from typing import Any
import urllib.parse

from ..errors import SkillToPluginError
from ..limits import MAX_HTTP_BYTES
from ..utils import sanitize_text, validate_url_credentials
from .archive import ArchiveExtractionResult, extract_archive
from .http import HttpFetcher


_MAX_METADATA_BYTES = min(MAX_HTTP_BYTES, 8 * 1024 * 1024)
_UNSCOPED_NAME = r"[a-z0-9](?:[a-z0-9._~-]{0,212}[a-z0-9])?"
_SCOPED_NAME = rf"@{_UNSCOPED_NAME}/{_UNSCOPED_NAME}"
_PACKAGE_RE = re.compile(rf"^(?:{_SCOPED_NAME}|{_UNSCOPED_NAME})$")
_SELECTOR_RE = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]{0,127})$")


@dataclass(frozen=True)
class NpmFetchResult:
    """Resolved npm version and safely extracted package snapshot."""

    package: str
    version: str
    tarball_url: str
    destination: Path
    package_root: Path
    archive_sha256: str
    tree_sha256: str
    file_count: int
    total_bytes: int
    integrity: str | None = None


def _parse_package_spec(package_spec: str) -> tuple[str, str | None]:
    value = package_spec.strip()
    if not value or value != package_spec or any(character.isspace() for character in value):
        raise SkillToPluginError("The npm package source is malformed.", code="unsupported_source")
    if value.startswith("@"):
        slash = value.find("/")
        if slash <= 1:
            raise SkillToPluginError("The scoped npm package name is malformed.", code="unsupported_source")
        separator = value.rfind("@")
        if separator > slash:
            name, selector = value[:separator], value[separator + 1 :]
        else:
            name, selector = value, None
    else:
        if "@" in value:
            name, selector = value.rsplit("@", 1)
        else:
            name, selector = value, None
    if not _PACKAGE_RE.fullmatch(name):
        raise SkillToPluginError(
            "Only canonical npm registry package names are supported.",
            code="unsupported_source",
            details={"package": sanitize_text(name)},
        )
    if len(name) > 214:
        raise SkillToPluginError("The npm package name exceeds the registry limit.", code="unsupported_source")
    if selector is not None and not _SELECTOR_RE.fullmatch(selector):
        raise SkillToPluginError(
            "Only an exact npm version or a simple dist-tag is supported.",
            code="unsupported_source",
            details={"selector": sanitize_text(selector)},
        )
    return name, selector


def _require_https_url(value: str, *, description: str) -> None:
    validate_url_credentials(value)
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise SkillToPluginError(f"The npm {description} URL is malformed.", code="invalid_manifest") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise SkillToPluginError(
            f"The npm {description} must use HTTPS.",
            code="security_rejected",
            details={"url": sanitize_text(value)},
        )


def _mapping(value: Any, *, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillToPluginError(f"The npm registry {description} is invalid.", code="invalid_manifest")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _resolve_version(metadata: dict[str, Any], selector: str | None) -> tuple[str, dict[str, Any]]:
    versions = _mapping(metadata.get("versions"), description="versions map")
    dist_tags = _mapping(metadata.get("dist-tags", {}), description="dist-tags map")
    selected = selector or dist_tags.get("latest")
    if selector is not None and selector in dist_tags:
        selected = dist_tags[selector]
    if not isinstance(selected, str) or not selected:
        raise SkillToPluginError(
            "The npm package has no resolvable version for the requested selector.",
            code="unknown_source",
            details={"selector": selector or "latest"},
        )
    manifest = versions.get(selected)
    if not isinstance(manifest, dict):
        raise SkillToPluginError(
            "The requested npm version is not present in registry metadata.",
            code="unknown_source",
            details={"version": selected},
        )
    manifest_version = manifest.get("version")
    if manifest_version is not None and manifest_version != selected:
        raise SkillToPluginError(
            "The npm version manifest does not match its registry key.",
            code="invalid_manifest",
            details={"key": selected, "manifest_version": manifest_version},
        )
    return selected, manifest


def _verify_integrity(path: Path, dist: dict[str, Any]) -> str | None:
    integrity = dist.get("integrity")
    if isinstance(integrity, str) and integrity.strip():
        # Subresource Integrity can contain several whitespace-separated
        # alternatives.  Verify the strongest supported digest available.
        parsed: list[tuple[int, str, bytes, str]] = []
        strengths = {"sha512": 3, "sha384": 2, "sha256": 1}
        for token in integrity.split():
            if "-" not in token:
                continue
            algorithm, encoded = token.split("-", 1)
            if algorithm not in strengths:
                continue
            try:
                expected = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            parsed.append((strengths[algorithm], algorithm, expected, token))
        if not parsed:
            raise SkillToPluginError(
                "The npm package declares no supported valid integrity digest.",
                code="invalid_manifest",
            )
        _strength, algorithm, expected, token = max(parsed, key=lambda item: item[0])
        digest = hashlib.new(algorithm)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if not hmac.compare_digest(digest.digest(), expected):
            raise SkillToPluginError(
                "The npm tarball failed its registry integrity check.",
                code="security_rejected",
                details={"algorithm": algorithm},
            )
        return token

    shasum = dist.get("shasum")
    if isinstance(shasum, str) and re.fullmatch(r"[0-9a-fA-F]{40}", shasum):
        digest = hashlib.sha1()  # Registry compatibility for older packages; TLS and SHA-256 are also recorded.
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest().casefold(), shasum.casefold()):
            raise SkillToPluginError(
                "The npm tarball failed its registry shasum check.",
                code="security_rejected",
            )
        return f"sha1-{shasum.casefold()}"
    return None


class NpmFetcher:
    """Resolve registry metadata and download the published tarball directly.

    No npm executable or subprocess is used, so package lifecycle scripts have
    no opportunity to execute during acquisition.
    """

    def __init__(
        self,
        http_fetcher: HttpFetcher | None = None,
        *,
        registry_url: str = "https://registry.npmjs.org",
    ) -> None:
        self.http_fetcher = http_fetcher or HttpFetcher()
        self.registry_url = registry_url.rstrip("/")
        if not self.registry_url:
            raise ValueError("registry_url cannot be empty")
        _require_https_url(self.registry_url, description="registry")

    def fetch(self, package_spec: str, destination: Path) -> NpmFetchResult:
        package, selector = _parse_package_spec(package_spec)
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise SkillToPluginError(
                "npm extraction requires a new destination path.",
                code="output_conflict",
                details={"destination": str(destination)},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata_url = f"{self.registry_url}/{urllib.parse.quote(package, safe='')}"

        with tempfile.TemporaryDirectory(prefix=".npm-fetch-", dir=destination.parent) as temporary_name:
            temporary = Path(temporary_name)
            metadata_path = temporary / "metadata.json"
            self.http_fetcher.fetch(metadata_url, metadata_path, max_bytes=_MAX_METADATA_BYTES)
            try:
                metadata_value = json.loads(
                    metadata_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise SkillToPluginError("The npm registry metadata is not valid UTF-8 JSON.", code="invalid_manifest") from exc
            metadata = _mapping(metadata_value, description="metadata document")
            metadata_name = metadata.get("name")
            if metadata_name is not None and metadata_name != package:
                raise SkillToPluginError(
                    "The npm registry metadata name does not match the requested package.",
                    code="invalid_manifest",
                    details={"requested": package, "metadata_name": metadata_name},
                )
            version, manifest = _resolve_version(metadata, selector)
            dist = _mapping(manifest.get("dist"), description="distribution record")
            tarball_url = dist.get("tarball")
            if not isinstance(tarball_url, str) or not tarball_url:
                raise SkillToPluginError("The npm distribution has no tarball URL.", code="invalid_manifest")
            _require_https_url(tarball_url, description="tarball")

            tarball_path = temporary / "package.tgz"
            http_result = self.http_fetcher.fetch(tarball_url, tarball_path)
            verified_integrity = _verify_integrity(tarball_path, dist)
            extraction: ArchiveExtractionResult = extract_archive(tarball_path, destination)

        children = list(destination.iterdir())
        conventional_root = destination / "package"
        package_root = conventional_root if conventional_root.is_dir() and len(children) == 1 else destination
        return NpmFetchResult(
            package=package,
            version=version,
            tarball_url=http_result.url,
            destination=destination,
            package_root=package_root,
            archive_sha256=extraction.archive_sha256,
            tree_sha256=extraction.tree_sha256,
            file_count=extraction.file_count,
            total_bytes=extraction.total_bytes,
            integrity=verified_integrity,
        )


__all__ = ["NpmFetchResult", "NpmFetcher"]
