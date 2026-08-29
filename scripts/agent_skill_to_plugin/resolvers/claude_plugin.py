"""Resolve a Claude Plugin install request to one immutable plugin boundary.

Marketplace and plugin descriptions are untrusted data.  This module only
interprets structured JSON fields and filesystem boundaries; it never follows
instructions from README files and never executes ``command`` plugin sources.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Sequence
import urllib.parse

from ..errors import NeedsInputError, SkillToPluginError
from ..fetchers.git import GitFetcher
from ..fetchers.github_api import is_github_shorthand
from ..fetchers.http import HttpFetcher
from ..fetchers.npm import NpmFetcher
from ..limits import DEFAULT_TIMEOUT_SECONDS
from ..models import MarketplaceInfo, ParsedInput, PluginSource, ResolvedSource
from ..utils import ensure_within, hash_tree, safe_posix_relative, sanitize_text, sha256_file
from ..validation import detect_external_references, parse_skill_manifest, validate_tree
from .archive import ARCHIVE_SUFFIXES, ArchiveResolver
from .git import GitResolver
from .github import GitHubResolver
from .local import LocalResolver


KNOWN_MARKETPLACES: dict[str, str] = {
    "claude-plugins-official": "anthropics/claude-plugins-official",
}
MAX_MARKETPLACE_JSON_BYTES = 10 * 1024 * 1024
MAX_CLAUDE_JSON_BYTES = 4 * 1024 * 1024
MAX_SEARCH_RESULTS = 5

MarketplaceSearcher = Callable[[str, str, Path, int], Sequence[str]]


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads_json(raw: str, *, description: str, error_code: str = "invalid_manifest") -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateKey) as exc:
        raise SkillToPluginError(
            f"The {description} is not valid unambiguous JSON: {sanitize_text(str(exc))}",
            code=error_code,
        ) from exc


def _read_json(path: Path, *, description: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise SkillToPluginError(f"The {description} is missing or is a symbolic link.", code="invalid_manifest")
        if path.stat().st_size > MAX_MARKETPLACE_JSON_BYTES:
            raise SkillToPluginError(f"The {description} exceeds the JSON size limit.", code="invalid_manifest")
        value = _loads_json(path.read_text(encoding="utf-8"), description=description)
    except SkillToPluginError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillToPluginError(f"Could not read the {description}.", code="invalid_manifest") from exc
    if not isinstance(value, dict):
        raise SkillToPluginError(f"The {description} must be a JSON object.", code="invalid_manifest")
    return value


def _relative_path(value: str | None, *, allow_root: bool = True) -> PurePosixPath | None:
    if value is None:
        return None
    normalized = urllib.parse.unquote(value.strip()).replace("\\", "/")
    if allow_root and normalized in {"", ".", "./"}:
        return None
    normalized = normalized.removeprefix("./")
    return safe_posix_relative(normalized)


def _is_archive_source(value: str) -> bool:
    path = urllib.parse.urlsplit(value).path.casefold()
    return path.endswith(ARCHIVE_SUFFIXES)


def _source_from_registration(entry: dict[str, Any]) -> str | None:
    """Extract only the documented source, never an internal install/cache path."""

    source = entry.get("source")
    if isinstance(source, str):
        if source.casefold() not in {"github", "git", "url", "directory", "local"}:
            return source
    if isinstance(source, dict):
        source_type = source.get("source") or source.get("type") or source.get("kind")
        if isinstance(source_type, str) and source_type.casefold() == "command":
            return None
        for key in ("repo", "repository", "url", "path"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(source_type, str) and (
            is_github_shorthand(source_type)
            or re.match(r"^(?:https?|ssh|git)://", source_type, flags=re.IGNORECASE)
        ):
            return source_type
    for key in ("repo", "repository", "url", "sourcePath"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _registered_entries(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        yield from (item for item in value if isinstance(item, dict))
        return
    if not isinstance(value, dict):
        return
    for container_key in ("marketplaces", "items", "results"):
        container = value.get(container_key)
        if isinstance(container, (list, dict)):
            yield from _registered_entries(container)
            return
    if isinstance(value.get("name"), str):
        yield value
        return
    # Some CLI versions render the registry as {name: registration}.
    for name, registration in value.items():
        if not isinstance(registration, dict):
            continue
        item = dict(registration)
        item.setdefault("name", name)
        yield item


def _registered_source(value: Any, marketplace_name: str) -> tuple[str | None, list[str]]:
    sources: list[str] = []
    for entry in _registered_entries(value):
        if entry.get("name") != marketplace_name:
            continue
        source = _source_from_registration(entry)
        if source and source not in sources:
            sources.append(source)
    return (sources[0] if len(sources) == 1 else None), sources


def _plugin_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("plugins")
    if isinstance(raw, list):
        if not all(isinstance(item, dict) for item in raw):
            raise SkillToPluginError("Marketplace plugins must be JSON objects.", code="invalid_manifest")
        return list(raw)
    if isinstance(raw, dict):
        entries: list[dict[str, Any]] = []
        for name, value in raw.items():
            if not isinstance(value, dict):
                raise SkillToPluginError("Marketplace plugin map values must be JSON objects.", code="invalid_manifest")
            entry = dict(value)
            entry.setdefault("name", name)
            entries.append(entry)
        return entries
    raise SkillToPluginError("Marketplace manifest has no valid `plugins` collection.", code="invalid_manifest")


def _exact_plugin_entry(manifest: dict[str, Any], plugin_name: str) -> dict[str, Any]:
    entries = _plugin_entries(manifest)
    matches = [entry for entry in entries if entry.get("name") == plugin_name]
    if len(matches) > 1:
        raise SkillToPluginError(
            "Marketplace manifest contains duplicate exact plugin names.",
            code="invalid_manifest",
            details={"plugin": plugin_name},
        )
    if not matches:
        available = sorted(
            entry["name"] for entry in entries
            if isinstance(entry.get("name"), str)
        )
        raise SkillToPluginError(
            f"Plugin `{sanitize_text(plugin_name)}` was not found in the resolved Marketplace.",
            code="unknown_plugin",
            details={"available_plugins": available[:100]},
        )
    return matches[0]


def _candidate_manifests(snapshot: Path) -> list[Path]:
    direct = snapshot / ".claude-plugin" / "marketplace.json"
    candidates = [direct] if direct.is_file() else []
    candidates.extend(
        path for path in snapshot.rglob("marketplace.json")
        if path.parent.name == ".claude-plugin" and path not in candidates
    )
    return sorted(candidates, key=lambda path: path.relative_to(snapshot).as_posix())


def _select_marketplace_manifest(
    snapshot: Path,
    marketplace_name: str,
    plugin_name: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    candidates = _candidate_manifests(snapshot)
    if not candidates:
        raise SkillToPluginError("No `.claude-plugin/marketplace.json` was found.", code="unknown_marketplace")
    matching_marketplaces: list[tuple[Path, dict[str, Any]]] = []
    invalid: list[dict[str, str]] = []
    valid_manifest_count = 0
    for path in candidates:
        try:
            manifest = _read_json(path, description="Claude Marketplace manifest")
        except SkillToPluginError as exc:
            invalid.append({"path": path.relative_to(snapshot).as_posix(), "error": exc.message})
            continue
        valid_manifest_count += 1
        if manifest.get("name") == marketplace_name:
            matching_marketplaces.append((path, manifest))
    if not matching_marketplaces:
        if valid_manifest_count == 0 and invalid:
            raise SkillToPluginError(
                "Every discovered Claude Marketplace manifest is invalid.",
                code="invalid_manifest",
                details={"invalid_manifests": invalid},
            )
        names: list[str] = []
        for path in candidates:
            try:
                manifest = _read_json(path, description="Claude Marketplace manifest")
            except SkillToPluginError:
                continue
            if isinstance(manifest.get("name"), str):
                names.append(manifest["name"])
        raise SkillToPluginError(
            f"Marketplace `{sanitize_text(marketplace_name)}` was not found at the resolved source.",
            code="unknown_marketplace",
            details={"available_marketplaces": sorted(set(names)), "invalid_manifests": invalid},
        )

    with_plugin: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    plugin_errors: list[SkillToPluginError] = []
    for path, manifest in matching_marketplaces:
        try:
            entry = _exact_plugin_entry(manifest, plugin_name)
            with_plugin.append((path, manifest, entry))
        except SkillToPluginError as exc:
            plugin_errors.append(exc)
    if len(with_plugin) > 1:
        raise SkillToPluginError(
            "Several Marketplace manifests match the same Marketplace and Plugin.",
            code="invalid_manifest",
            details={"paths": [path.relative_to(snapshot).as_posix() for path, _, _ in with_plugin]},
        )
    if with_plugin:
        return with_plugin[0]
    if plugin_errors:
        raise plugin_errors[0]
    raise SkillToPluginError(f"Plugin `{sanitize_text(plugin_name)}` was not found.", code="unknown_plugin")


def _optional_git_commit_sha(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise SkillToPluginError(
            "Claude Plugin Git source `sha` must be a full 40-character commit SHA.",
            code="invalid_manifest",
        )
    return value.casefold()


def _parse_plugin_source(
    entry: dict[str, Any],
    marketplace_manifest: dict[str, Any] | None = None,
) -> PluginSource:
    if entry.get("headersHelper") is not None:
        raise SkillToPluginError(
            "Claude archive headersHelper commands are not executed by Agent Skill to Plugin.",
            code="security_rejected",
        )
    raw = entry.get("source")
    if raw is None:
        raise SkillToPluginError("Claude Plugin entry has no `source`.", code="invalid_manifest")
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            raise SkillToPluginError("Claude Plugin source is empty.", code="invalid_manifest")
        lowered = value.casefold()
        if lowered == "command" or lowered.startswith("command:"):
            raise SkillToPluginError(
                "Claude command sources are arbitrary code and are never executed.",
                code="security_rejected",
            )
        if value in {".", "./"} or value.startswith(("./", "../")):
            return PluginSource(kind="relative", value=value)
        if is_github_shorthand(value) or "github.com/" in lowered:
            return PluginSource(kind="github", value=value)
        if lowered.startswith(("ssh://", "git://", "git+https://")) or re.match(r"^[^/@\s]+@[^:\s]+:.+$", value):
            return PluginSource(kind="git", value=value.removeprefix("git+"))
        if lowered.startswith("https://"):
            return PluginSource(kind="archive" if _is_archive_source(value) else "git", value=value)
        metadata = marketplace_manifest.get("metadata") if isinstance(marketplace_manifest, dict) else None
        plugin_root = metadata.get("pluginRoot") if isinstance(metadata, dict) else None
        if re.fullmatch(r"[A-Za-z0-9._-]+", value) and isinstance(plugin_root, str):
            root_path = _relative_path(plugin_root)
            if root_path is None:
                return PluginSource(kind="relative", value=f"./{value}")
            combined = PurePosixPath(*root_path.parts, value).as_posix()
            return PluginSource(kind="relative", value=f"./{combined}")
        raise SkillToPluginError("Unsupported Claude Plugin source string.", code="unsupported_source")
    if not isinstance(raw, dict):
        raise SkillToPluginError("Claude Plugin source must be a string or object.", code="invalid_manifest")
    if "command" in raw:
        raise SkillToPluginError(
            "Claude command sources are arbitrary code and are never executed.",
            code="security_rejected",
        )
    source_type = raw.get("source") or raw.get("type") or raw.get("kind")
    if not isinstance(source_type, str) or not source_type.strip():
        raise SkillToPluginError("Claude Plugin source object has no source type.", code="invalid_manifest")
    kind = source_type.casefold().replace("_", "-")
    if kind == "command":
        raise SkillToPluginError(
            "Claude command sources are arbitrary code and are never executed.",
            code="security_rejected",
        )

    # A few producers wrap the ordinary string source in an object without a
    # type discriminator, e.g. {"source": "./plugins/demo"}.
    if (
        source_type in {".", "./"}
        or source_type.startswith(("./", "../"))
        or is_github_shorthand(source_type)
        or re.match(r"^(?:https?|ssh|git)://", source_type, flags=re.IGNORECASE)
    ):
        nested = dict(entry)
        nested["source"] = source_type
        parsed = _parse_plugin_source(nested, marketplace_manifest)
        nested_ref = raw.get("ref")
        nested_sha = _optional_git_commit_sha(raw.get("sha") or raw.get("commit"))
        nested_subdir = raw.get("subdir") or raw.get("path")
        return replace(
            parsed,
            ref=nested_ref if isinstance(nested_ref, str) and nested_ref else parsed.ref,
            sha=nested_sha or parsed.sha,
            subdir=nested_subdir if isinstance(nested_subdir, str) and nested_subdir else parsed.subdir,
        )

    ref_value = raw.get("ref")
    sha_value = raw.get("sha") or raw.get("commit")
    ref = ref_value if isinstance(ref_value, str) and ref_value else None
    sha = _optional_git_commit_sha(sha_value)
    subdir_value = raw.get("subdir") if raw.get("subdir") is not None else raw.get("path")
    subdir = subdir_value if isinstance(subdir_value, str) and subdir_value.strip() else None
    if subdir is not None:
        _relative_path(subdir)

    if kind in {"relative", "local", "directory", "path"}:
        path = raw.get("path") or raw.get("value")
        if not isinstance(path, str) or not path.strip():
            raise SkillToPluginError("Relative Claude Plugin source has no path.", code="invalid_manifest")
        _relative_path(path)
        return PluginSource(kind="relative", value=path, subdir=None)
    if kind in {"github", "git", "git-subdir"}:
        value = raw.get("repo") or raw.get("repository") or raw.get("url") or raw.get("value")
        if not isinstance(value, str) or not value.strip():
            raise SkillToPluginError("Git-backed Claude Plugin source has no repository.", code="invalid_manifest")
        actual_kind = "github" if kind == "github" or is_github_shorthand(value) or "github.com/" in value.casefold() else "git"
        return PluginSource(kind=actual_kind, value=value.removeprefix("git+"), ref=ref or sha, sha=sha, subdir=subdir)
    if kind in {"url", "archive", "https-archive"}:
        value = raw.get("url") or raw.get("value")
        if not isinstance(value, str) or not value.strip():
            raise SkillToPluginError("URL-backed Claude Plugin source has no URL.", code="invalid_manifest")
        if not value.casefold().startswith("https://"):
            raise SkillToPluginError("Claude URL sources must use HTTPS.", code="security_rejected")
        if kind in {"archive", "https-archive"}:
            resolved_kind = "archive"
        elif "github.com/" in value.casefold():
            resolved_kind = "github"
        else:
            # Per the Claude Marketplace schema, structured ``source: url``
            # is a Git URL. Downloadable artifacts use ``source: archive``.
            resolved_kind = "git"
        metadata: dict[str, Any] = {}
        if resolved_kind == "archive":
            expected_sha256 = raw.get("sha256")
            if expected_sha256 is not None:
                if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
                    raise SkillToPluginError("Archive source sha256 must be 64 hexadecimal characters.", code="invalid_manifest")
                metadata["expected_sha256"] = expected_sha256.casefold()
        return PluginSource(
            kind=resolved_kind,
            value=value,
            ref=ref or sha,
            sha=sha,
            subdir=subdir,
            metadata=metadata,
        )
    if kind in {"npm", "registry"}:
        package = raw.get("package") or raw.get("name") or raw.get("value")
        if not isinstance(package, str) or not package.strip():
            raise SkillToPluginError("npm Claude Plugin source has no package name.", code="invalid_manifest")
        version = raw.get("version") or raw.get("tag")
        if version is not None and not isinstance(version, str):
            raise SkillToPluginError("npm source version must be a string.", code="invalid_manifest")
        if raw.get("registry") is not None:
            raise SkillToPluginError(
                "Custom npm registries are not supported; the tool only performs bounded public-registry tarball acquisition.",
                code="unsupported_source",
            )
        spec = package if not version else f"{package}@{version}"
        return PluginSource(kind="npm", value=spec, ref=version, subdir=subdir)
    raise SkillToPluginError(
        f"Unsupported Claude Plugin source type `{sanitize_text(source_type)}`.",
        code="unsupported_source",
    )


def _recognizable_plugin_root(root: Path) -> bool:
    if (root / ".claude-plugin" / "plugin.json").is_file():
        return True
    return any(
        (root / name).exists()
        for name in (
            "SKILL.md", "skills", "commands", "agents", "hooks", ".mcp.json",
            "settings.json", "settings", "lsp", "monitors",
        )
    )


def _validate_plugin_root(snapshot: Path, plugin_root: str | None, plugin_name: str) -> str:
    relative = _relative_path(plugin_root)
    root = snapshot if relative is None else ensure_within(snapshot.joinpath(*relative.parts), snapshot)
    if root.is_symlink() or not root.is_dir():
        raise SkillToPluginError(
            "Resolved Claude Plugin root is missing or is not a directory.",
            code="invalid_manifest",
            details={"plugin_root": plugin_root or "."},
        )
    if relative is None and not _recognizable_plugin_root(root):
        children = [child for child in root.iterdir() if child.is_dir() and not child.is_symlink()]
        files = [child for child in root.iterdir() if child.is_file()]
        if len(children) == 1 and not files and _recognizable_plugin_root(children[0]):
            root = children[0]
            relative = PurePosixPath(root.relative_to(snapshot).as_posix())
    plugin_manifest = root / ".claude-plugin" / "plugin.json"
    if plugin_manifest.is_file():
        manifest = _read_json(plugin_manifest, description="Claude Plugin manifest")
        declared_name = manifest.get("name")
        if declared_name is not None and declared_name != plugin_name:
            raise SkillToPluginError(
                "Claude Plugin manifest name does not match the Marketplace entry.",
                code="invalid_manifest",
                details={"marketplace_name": plugin_name, "manifest_name": declared_name},
            )
    else:
        if not _recognizable_plugin_root(root):
            raise SkillToPluginError(
                "Resolved source does not contain a recognizable Claude Plugin root.",
                code="invalid_manifest",
                details={"plugin_root": plugin_root or "."},
            )
    return "." if relative is None else relative.as_posix()


class ClaudePluginResolver:
    """Resolve one parsed Claude Plugin install without installing the plugin."""

    def __init__(
        self,
        *,
        claude_executable: str | None = None,
        marketplace_searcher: MarketplaceSearcher | None = None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.claude_executable = claude_executable
        self.marketplace_searcher = marketplace_searcher
        self.subprocess_runner = subprocess_runner

    @staticmethod
    def supports(parsed: ParsedInput) -> bool:
        return parsed.kind == "claude_plugin"

    def _claude_registered_source(self, marketplace_name: str, timeout: int) -> tuple[str | None, list[str]]:
        executable = self.claude_executable
        if executable is None:
            executable = shutil.which("claude.exe") or shutil.which("claude")
        if not executable or executable.casefold().endswith((".cmd", ".bat")):
            return None, []
        try:
            completed = self.subprocess_runner(
                [executable, "plugin", "marketplace", "list", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env={**os.environ, "NO_COLOR": "1", "CI": "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, []
        if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > MAX_CLAUDE_JSON_BYTES:
            return None, []
        try:
            value = _loads_json(completed.stdout, description="Claude Marketplace list", error_code="invalid_manifest")
        except SkillToPluginError:
            return None, []
        return _registered_source(value, marketplace_name)

    @staticmethod
    def _default_search(
        marketplace_name: str,
        plugin_name: str,
        work_dir: Path,
        timeout: int,
    ) -> Sequence[str]:
        query = f'path:.claude-plugin filename:marketplace.json "{marketplace_name}" "{plugin_name}"'
        url = "https://api.github.com/search/code?q=" + urllib.parse.quote(query)
        path = work_dir / "github-marketplace-search.json"
        try:
            HttpFetcher(timeout_seconds=timeout).fetch(url, path, max_bytes=4 * 1024 * 1024)
            value = _loads_json(path.read_text(encoding="utf-8"), description="GitHub search response")
        except (OSError, UnicodeDecodeError, SkillToPluginError):
            return []
        items = value.get("items", []) if isinstance(value, dict) else []
        sources: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if not str(item.get("path", "")).endswith(".claude-plugin/marketplace.json"):
                continue
            repository = item.get("repository")
            if not isinstance(repository, dict):
                continue
            source = repository.get("html_url")
            if not isinstance(source, str):
                full_name = repository.get("full_name")
                source = f"https://github.com/{full_name}" if isinstance(full_name, str) else None
            if isinstance(source, str) and source not in sources:
                sources.append(source)
            if len(sources) >= MAX_SEARCH_RESULTS:
                break
        return sources

    @staticmethod
    def _acquire_marketplace(
        source: str,
        destination: Path,
        *,
        source_base: Path,
        timeout: int,
    ) -> ResolvedSource:
        source = source.strip()
        lowered = source.casefold()

        # owner/repo is an explicit remote identity. Resolve it before probing
        # source_base so a same-named local tree cannot shadow a known mapping.
        # A caller that intends a local relative path can use ./owner/repo or
        # ../owner/repo; those explicit forms are not GitHub shorthand.
        if is_github_shorthand(source):
            parsed = ParsedInput(
                kind="github_repository",
                raw_input=source,
                normalized_input=source,
                source=source,
            )
            return GitHubResolver(GitFetcher(timeout=timeout)).resolve(parsed, destination)

        local = Path(os.path.expanduser(source))
        if not local.is_absolute():
            local = source_base / local
        if local.exists():
            local = local.resolve()
            if local.is_file() and local.name.casefold() == "marketplace.json":
                destination.mkdir(parents=True, exist_ok=False)
                target = destination / ".claude-plugin" / "marketplace.json"
                target.parent.mkdir(parents=True)
                shutil.copy2(local, target)
                return ResolvedSource(
                    kind="local_claude_marketplace_manifest",
                    normalized_source=str(local),
                    snapshot_path=str(destination.resolve()),
                    snapshot_sha256=hash_tree(destination),
                    requested_path=".claude-plugin/marketplace.json",
                    resolution_method="filesystem-copy",
                )
            if local.is_file() and _is_archive_source(local.name):
                parsed = ParsedInput(kind="local_archive", raw_input=source, normalized_input=source, source=str(local))
                return ArchiveResolver(HttpFetcher(timeout_seconds=timeout)).resolve(parsed, destination, source_base=source_base)
            parsed = ParsedInput(kind="local", raw_input=source, normalized_input=source, source=str(local))
            return LocalResolver().resolve(parsed, destination, source_base=source_base)

        if lowered.startswith("https://") and urllib.parse.urlsplit(source).path.casefold().endswith("marketplace.json"):
            destination.mkdir(parents=True, exist_ok=False)
            target = destination / ".claude-plugin" / "marketplace.json"
            target.parent.mkdir(parents=True)
            result = HttpFetcher(timeout_seconds=timeout).fetch(source, target, max_bytes=MAX_MARKETPLACE_JSON_BYTES)
            return ResolvedSource(
                kind="https_claude_marketplace_manifest",
                normalized_source=result.url,
                snapshot_path=str(destination.resolve()),
                snapshot_sha256=hash_tree(destination),
                requested_path=".claude-plugin/marketplace.json",
                resolution_method="bounded-https-download",
                metadata={"http_sha256": result.sha256, "redirects": list(result.redirects)},
            )
        if _is_archive_source(source):
            parsed = ParsedInput(kind="archive_url", raw_input=source, normalized_input=source, source=source)
            return ArchiveResolver(HttpFetcher(timeout_seconds=timeout)).resolve(parsed, destination, source_base=source_base)
        if "github.com/" in lowered:
            parsed = ParsedInput(kind="github_repository", raw_input=source, normalized_input=source, source=source)
            return GitHubResolver(GitFetcher(timeout=timeout)).resolve(parsed, destination)
        if lowered.startswith(("https://", "ssh://", "git://", "git+https://")) or re.match(r"^[^/@\s]+@[^:\s]+:.+$", source):
            normalized = source.removeprefix("git+")
            parsed = ParsedInput(kind="git_url", raw_input=source, normalized_input=normalized, source=normalized)
            return GitResolver(GitFetcher(timeout=timeout)).resolve(parsed, destination)
        raise SkillToPluginError("Could not resolve the Claude Marketplace source.", code="unknown_marketplace")

    @staticmethod
    def _materialize_relative_plugin(
        plugin_path: Path,
        destination: Path,
        *,
        marketplace_snapshot: Path,
    ) -> tuple[ResolvedSource, str]:
        """Persist only the selected relative Plugin plus required evidence.

        The Marketplace repository is acquired into a short-lived, validated
        snapshot. Copying that entire repository again is unnecessary and can
        exceed Windows path limits because unrelated Plugins may contain deep
        trees. The selected Plugin keeps its repository-relative placement so
        `SKILL.md` references remain stable; concrete external references and
        root license evidence are copied explicitly.
        """

        plugin_relative = plugin_path.relative_to(marketplace_snapshot).as_posix() or "."
        if plugin_relative == ".":
            # A root Plugin has no narrower structural boundary to preserve.
            LocalResolver._copy(plugin_path, destination)
        else:
            LocalResolver._copy(plugin_path, destination / Path(plugin_relative))

        copied_external: list[str] = []
        for skill_md in sorted(plugin_path.rglob("SKILL.md"), key=lambda item: item.as_posix()):
            manifest = parse_skill_manifest(skill_md)
            if not manifest.valid:
                continue
            for reference in detect_external_references(skill_md.parent, marketplace_snapshot):
                source = ensure_within(marketplace_snapshot / Path(reference.source_path), marketplace_snapshot)
                try:
                    source.relative_to(plugin_path)
                    continue  # already included with the Plugin subtree
                except ValueError:
                    pass
                try:
                    plugin_path.relative_to(source)
                except ValueError:
                    pass
                else:
                    raise SkillToPluginError(
                        "A Skill references an ancestor directory broad enough to include the selected Plugin; refusing an implicit Marketplace-wide copy.",
                        code="package_validation_failed",
                        details={"reference": reference.raw_reference, "source_path": reference.source_path},
                    )
                target = destination / Path(reference.source_path)
                if target.exists():
                    expected = hash_tree(source) if source.is_dir() else sha256_file(source)
                    actual = hash_tree(target) if target.is_dir() else sha256_file(target)
                    if actual != expected:
                        raise SkillToPluginError(
                            "External Plugin reference collides with different snapshot content.",
                            code="package_validation_failed",
                            details={"source_path": reference.source_path},
                        )
                elif source.is_dir():
                    LocalResolver._copy(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                copied_external.append(reference.source_path)

        # Preserve repository-root legal evidence without copying unrelated
        # Marketplace Plugins. The complete Marketplace snapshot hash remains
        # in provenance as well.
        for source in marketplace_snapshot.iterdir():
            folded = source.name.casefold()
            if not source.is_file() or source.is_symlink() or not (
                folded in {"license", "copying", "notice"}
                or folded.startswith(("license.", "copying.", "notice."))
            ):
                continue
            target = destination / source.name
            if target.exists() and sha256_file(target) != sha256_file(source):
                raise SkillToPluginError(
                    "Repository license evidence collides with Plugin content.",
                    code="package_validation_failed",
                    details={"path": source.name},
                )
            if not target.exists():
                shutil.copy2(source, target)

        tree = validate_tree(destination)
        return (
            ResolvedSource(
                kind="local_claude_plugin",
                normalized_source=str(plugin_path),
                snapshot_path=str(destination.resolve()),
                snapshot_sha256=tree.tree_sha256,
                requested_path=None if plugin_relative == "." else plugin_relative,
                resolution_method="selected-relative-plugin-copy",
                metadata={
                    "source_root": str(plugin_path),
                    "copied_external_reference_paths": sorted(set(copied_external)),
                },
            ),
            plugin_relative,
        )

    @staticmethod
    def _acquire_plugin(
        plugin_source: PluginSource,
        destination: Path,
        *,
        marketplace_snapshot: Path,
        marketplace_manifest: Path,
        source_base: Path,
        timeout: int,
    ) -> tuple[ResolvedSource, str]:
        if plugin_source.kind == "relative":
            marketplace_root = marketplace_manifest.parent.parent
            relative = _relative_path(plugin_source.value)
            plugin_path = marketplace_root if relative is None else ensure_within(
                marketplace_root.joinpath(*relative.parts), marketplace_snapshot
            )
            if plugin_path.is_symlink() or not plugin_path.is_dir():
                raise SkillToPluginError(
                    "Relative Claude Plugin source is missing or not a directory.",
                    code="invalid_manifest",
                    details={"source": plugin_source.value},
                )
            plugin_relative = plugin_path.relative_to(marketplace_snapshot).as_posix() or "."
            return ClaudePluginResolver._materialize_relative_plugin(
                plugin_path,
                destination,
                marketplace_snapshot=marketplace_snapshot,
            )

        requested_ref = plugin_source.ref or plugin_source.sha
        requested_path = plugin_source.subdir
        if plugin_source.kind == "github":
            parsed = ParsedInput(
                kind="github_repository",
                raw_input=plugin_source.value,
                normalized_input=plugin_source.value,
                source=plugin_source.value,
                requested_ref=requested_ref,
                requested_path=requested_path,
            )
            resolved = GitHubResolver(GitFetcher(timeout=timeout)).resolve(parsed, destination)
            ClaudePluginResolver._verify_declared_git_sha(plugin_source, resolved, destination)
            return resolved, requested_path or "."
        if plugin_source.kind == "git":
            parsed = ParsedInput(
                kind="git_url",
                raw_input=plugin_source.value,
                normalized_input=plugin_source.value,
                source=plugin_source.value,
                requested_ref=requested_ref,
                requested_path=requested_path,
            )
            resolved = GitResolver(GitFetcher(timeout=timeout)).resolve(parsed, destination)
            ClaudePluginResolver._verify_declared_git_sha(plugin_source, resolved, destination)
            return resolved, requested_path or "."
        if plugin_source.kind == "archive":
            parsed = ParsedInput(
                kind="archive_url",
                raw_input=plugin_source.value,
                normalized_input=plugin_source.value,
                source=plugin_source.value,
                requested_path=requested_path,
            )
            resolved = ArchiveResolver(HttpFetcher(timeout_seconds=timeout)).resolve(
                parsed, destination, source_base=source_base
            )
            expected_sha256 = plugin_source.metadata.get("expected_sha256")
            actual_sha256 = resolved.metadata.get("archive_sha256")
            if expected_sha256 and expected_sha256 != actual_sha256:
                shutil.rmtree(destination, ignore_errors=True)
                raise SkillToPluginError(
                    "The Claude archive source failed its declared SHA-256 check.",
                    code="security_rejected",
                    details={"expected": expected_sha256, "actual": actual_sha256},
                )
            return resolved, requested_path or "."
        if plugin_source.kind == "npm":
            npm_result = NpmFetcher(HttpFetcher(timeout_seconds=timeout)).fetch(plugin_source.value, destination)
            base_root = npm_result.package_root.relative_to(destination).as_posix()
            if base_root == ".":
                base_root = ""
            if requested_path:
                subdir = _relative_path(requested_path)
                assert subdir is not None
                root = "/".join(part for part in (base_root, subdir.as_posix()) if part)
            else:
                root = base_root or "."
            resolved = ResolvedSource(
                kind="npm_package",
                normalized_source=f"npm:{npm_result.package}@{npm_result.version}",
                snapshot_path=str(destination.resolve()),
                snapshot_sha256=hash_tree(destination),
                requested_ref=npm_result.version,
                requested_path=None if root == "." else root,
                resolution_method="npm-registry-metadata-and-tarball",
                metadata={
                    "archive_sha256": npm_result.archive_sha256,
                    "integrity": npm_result.integrity,
                    "tarball_url": npm_result.tarball_url,
                },
            )
            return resolved, root
        if plugin_source.kind == "command":
            raise SkillToPluginError("Claude command sources are never executed.", code="security_rejected")
        raise SkillToPluginError("Unsupported resolved Claude Plugin source.", code="unsupported_source")

    @staticmethod
    def _verify_declared_git_sha(
        plugin_source: PluginSource,
        resolved: ResolvedSource,
        destination: Path,
    ) -> None:
        """Bind a Marketplace-declared named ref to its immutable commit."""

        if plugin_source.sha is None:
            return
        expected = plugin_source.sha.casefold()
        actual = (resolved.resolved_commit or "").casefold()
        if actual == expected:
            return
        shutil.rmtree(destination, ignore_errors=True)
        raise SkillToPluginError(
            "The resolved Claude Plugin Git commit does not match its declared `sha`.",
            code="resolution_integrity_failed",
            details={
                "ref": plugin_source.ref,
                "expected": expected,
                "actual": actual or None,
            },
        )

    def _search_unique_source(
        self,
        marketplace_name: str,
        plugin_name: str,
        *,
        source_base: Path,
        work_dir: Path,
        timeout: int,
    ) -> str | None:
        searcher = self.marketplace_searcher or self._default_search
        try:
            raw_sources = searcher(marketplace_name, plugin_name, work_dir, timeout)
        except Exception:
            # Search is deliberately best-effort.  It must not hide the final,
            # actionable request for a Marketplace source.
            return None
        sources = list(dict.fromkeys(source for source in raw_sources if isinstance(source, str) and source.strip()))[:MAX_SEARCH_RESULTS]
        matches: list[str] = []
        for index, source in enumerate(sources):
            candidate = work_dir / f"search-candidate-{index + 1}"
            try:
                self._acquire_marketplace(source, candidate, source_base=source_base, timeout=timeout)
                _select_marketplace_manifest(candidate, marketplace_name, plugin_name)
                matches.append(source)
            except SkillToPluginError:
                continue
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise NeedsInputError(
                "Several public repositories contain the requested Marketplace and Plugin; select the source.",
                prompt_kind="marketplace_source",
                choices=[{"id": f"marketplace-{index + 1}", "label": source, "source": source} for index, source in enumerate(matches)],
            )
        return None

    def resolve(
        self,
        parsed: ParsedInput,
        snapshot_dir: Path,
        source_base: Path,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ResolvedSource:
        if not self.supports(parsed):
            raise SkillToPluginError("ClaudePluginResolver received an incompatible input.", code="unknown_input_format")
        if timeout <= 0:
            raise SkillToPluginError("Resolver timeout must be positive.", code="unknown_input_format")
        if not parsed.marketplace_name or not parsed.plugin_name:
            raise SkillToPluginError("Claude Plugin input lacks Marketplace or Plugin name.", code="unknown_input_format")
        snapshot_dir = Path(snapshot_dir)
        source_base = Path(source_base).resolve()
        if snapshot_dir.exists() or snapshot_dir.is_symlink():
            raise SkillToPluginError("Snapshot destination already exists.", code="output_conflict")
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)

        marketplace_source = parsed.marketplace_source
        resolution_method = "inline-marketplace-add" if marketplace_source else ""
        # Keep the transient Marketplace checkout in the platform temp area.
        # Persisting only the selected Plugin below prevents unrelated deep
        # Marketplace paths from exceeding Windows path limits.
        with tempfile.TemporaryDirectory(prefix="agent-skill-to-plugin-claude-") as temporary_name:
            temporary = Path(temporary_name)
            if marketplace_source is None:
                registered, registered_candidates = self._claude_registered_source(parsed.marketplace_name, timeout)
                if registered is not None:
                    marketplace_source = registered
                    resolution_method = "claude-cli-marketplace-list"
                elif len(registered_candidates) > 1:
                    raise NeedsInputError(
                        "Several registered Marketplace sources share this name; select one.",
                        prompt_kind="marketplace_source",
                        choices=[{"id": f"marketplace-{index + 1}", "label": source, "source": source} for index, source in enumerate(registered_candidates)],
                    )
            if marketplace_source is None and parsed.marketplace_name in KNOWN_MARKETPLACES:
                marketplace_source = KNOWN_MARKETPLACES[parsed.marketplace_name]
                resolution_method = "known-marketplace-map"
            if marketplace_source is None:
                marketplace_source = self._search_unique_source(
                    parsed.marketplace_name,
                    parsed.plugin_name,
                    source_base=source_base,
                    work_dir=temporary,
                    timeout=timeout,
                )
                if marketplace_source:
                    resolution_method = "github-public-search"
            if marketplace_source is None:
                raise NeedsInputError(
                    f"Marketplace `{sanitize_text(parsed.marketplace_name)}` could not be resolved uniquely. Provide its repository or URL.",
                    prompt_kind="marketplace_source",
                    choices=[],
                )

            marketplace_snapshot = temporary / "marketplace"
            marketplace_resolved = self._acquire_marketplace(
                marketplace_source,
                marketplace_snapshot,
                source_base=source_base,
                timeout=timeout,
            )
            manifest_path, marketplace_manifest, plugin_entry = _select_marketplace_manifest(
                marketplace_snapshot,
                parsed.marketplace_name,
                parsed.plugin_name,
            )
            plugin_source = _parse_plugin_source(plugin_entry, marketplace_manifest)
            base_resolved, plugin_root = self._acquire_plugin(
                plugin_source,
                snapshot_dir,
                marketplace_snapshot=marketplace_snapshot,
                marketplace_manifest=manifest_path,
                source_base=source_base,
                timeout=timeout,
            )

            plugin_root = _validate_plugin_root(snapshot_dir, plugin_root, parsed.plugin_name)
            manifest_relative = manifest_path.relative_to(marketplace_snapshot).as_posix()
            license_value = marketplace_manifest.get("license")
            marketplace = MarketplaceInfo(
                name=parsed.marketplace_name,
                source=marketplace_source,
                manifest_path=manifest_relative,
                resolution_method=resolution_method,
                license=license_value if isinstance(license_value, str) else None,
            )
            metadata = dict(base_resolved.metadata)
            if plugin_source.kind == "relative":
                for key in (
                    "github_license", "github_license_name", "github_license_url",
                    "github_license_status", "skipped_symbolic_links",
                ):
                    if key in marketplace_resolved.metadata:
                        metadata[key] = marketplace_resolved.metadata[key]
            metadata.update(
                {
                    "plugin_root": plugin_root,
                    "marketplace_manifest": manifest_relative,
                    "marketplace_snapshot_sha256": marketplace_resolved.snapshot_sha256,
                    "marketplace_resolved_commit": marketplace_resolved.resolved_commit,
                    "marketplace_repository_url": marketplace_resolved.repository_url,
                    "marketplace_resolution_method": resolution_method,
                    "excluded_marketplace_components": sorted(
                        key for key in (
                            "commands", "agents", "hooks", "mcpServers", "settings",
                            "userConfig", "lspServers", "monitors", "dependencies",
                        )
                        if key in plugin_entry
                    ),
                }
            )
            relative_plugin_source = plugin_source.kind == "relative"
            normalized_source = (
                marketplace_resolved.normalized_source
                if relative_plugin_source
                else base_resolved.normalized_source
            )
            repository_url = (
                marketplace_resolved.repository_url
                if relative_plugin_source
                else base_resolved.repository_url
            )
            requested_ref = (
                marketplace_resolved.requested_ref
                if relative_plugin_source
                else base_resolved.requested_ref
            )
            resolved_commit = (
                marketplace_resolved.resolved_commit
                if relative_plugin_source
                else base_resolved.resolved_commit
            )
            return ResolvedSource(
                kind="claude_plugin",
                normalized_source=normalized_source,
                snapshot_path=str(snapshot_dir.resolve()),
                snapshot_sha256=hash_tree(snapshot_dir),
                repository_url=repository_url,
                requested_ref=requested_ref,
                resolved_commit=resolved_commit,
                requested_path=None if plugin_root == "." else plugin_root,
                marketplace=marketplace,
                plugin_source=plugin_source,
                original_plugin_name=parsed.plugin_name,
                resolution_method=f"claude-marketplace:{base_resolved.resolution_method}",
                metadata=metadata,
            )


def resolve(
    parsed: ParsedInput,
    snapshot_dir: Path,
    source_base: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> ResolvedSource:
    """Module-level resolver entry point used by the CLI dispatcher."""

    return ClaudePluginResolver().resolve(parsed, snapshot_dir, source_base, timeout)


__all__ = ["ClaudePluginResolver", "KNOWN_MARKETPLACES", "resolve"]
