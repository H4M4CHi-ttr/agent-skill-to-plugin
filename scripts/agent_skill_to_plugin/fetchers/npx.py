"""Safe ``npx skills add`` acquisition backend.

Only the canonical ``skills`` package is executed.  Imported Skill scripts are
never invoked, npm lifecycle scripts are disabled, npm writes are redirected to
an ephemeral cache, and every process call uses an argv list with ``shell=False``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence

from ..errors import SkillToPluginError
from ..input_parser import _validate_option_value, _validate_source_value
from ..limits import DEFAULT_TIMEOUT_SECONDS
from ..models import ParsedInput
from ..utils import sanitize_text


_SAFE_PACKAGE_RE = re.compile(
    r"skills(?:@(?:[A-Za-z][A-Za-z0-9._-]*|[~^]?\d+(?:\.\d+){0,2}"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?))?\Z"
)
_KNOWN_SKILL_ROOTS = (Path(".agents/skills"), Path(".codex/skills"), Path("skills"))
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


@dataclass(frozen=True)
class NpxFetchResult:
    snapshot_path: Path
    resolved_source: str
    installed_skill_dirs: tuple[Path, ...]
    argv: tuple[str, ...]
    stdout: str
    stderr: str


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_local_source(source: str) -> bool:
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", source)
        or source.startswith(("\\\\", "./", "../", ".\\", "..\\", "/", "~/", "~\\"))
    )


def _resolve_source(source: str, source_base: Path) -> str:
    _validate_source_value(source)
    if not _is_local_source(source):
        return source
    candidate = Path(os.path.expanduser(source))
    if not candidate.is_absolute():
        candidate = source_base / candidate
    try:
        if _is_link_or_reparse(candidate):
            raise SkillToPluginError(
                "The local npx source root may not be a symbolic link or Windows reparse point.",
                code="security_rejected",
            )
        candidate = candidate.resolve(strict=True)
    except SkillToPluginError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SkillToPluginError(
            "The local Skill source does not exist or could not be resolved.",
            code="unknown_source",
            details={"source": sanitize_text(source)},
        ) from exc
    return str(candidate)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SkillToPluginError(
            f"Could not inspect fetched path `{sanitize_text(str(path))}`.",
            code="security_rejected",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _validate_snapshot_links(root: Path) -> None:
    if _is_link_or_reparse(root):
        raise SkillToPluginError("The snapshot root may not be a symbolic link or junction.", code="security_rejected")
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirnames + filenames:
            child = current_path / name
            if _is_link_or_reparse(child):
                raise SkillToPluginError(
                    f"Fetched Skills may not contain symbolic links or junctions: `{sanitize_text(str(child.relative_to(root)))}`.",
                    code="security_rejected",
                )


def _resolve_npx_prefix(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    candidates = ("npx.cmd", "npx") if os.name == "nt" else ("npx", "npx.cmd")
    npx_path = next((path for candidate in candidates if (path := which(candidate))), None)
    if not npx_path:
        raise SkillToPluginError(
            "`npx` was not found. Install a compatible Node.js/npm runtime first.",
            code="dependency_missing",
        )

    if os.name != "nt" or not npx_path.casefold().endswith((".cmd", ".bat")):
        return [npx_path]

    node_path = which("node.exe") or which("node")
    if not node_path:
        raise SkillToPluginError("`node` was not found for the Windows npx launcher.", code="dependency_missing")
    launcher_dir = Path(npx_path).resolve().parent
    possible_cli_paths = (
        launcher_dir / "node_modules" / "npm" / "bin" / "npx-cli.js",
        launcher_dir.parent / "node_modules" / "npm" / "bin" / "npx-cli.js",
    )
    for cli_path in possible_cli_paths:
        if cli_path.is_file():
            # Running the JS entrypoint avoids implicit cmd.exe evaluation.
            # Resolve both paths so Windows 8.3 aliases cannot leave argv with
            # a mixture of short and long path spellings.
            return [str(Path(node_path).resolve()), str(cli_path.resolve())]
    raise SkillToPluginError(
        "Could not locate `npx-cli.js` for the Windows npx launcher.",
        code="dependency_missing",
        details={"npx_path": sanitize_text(npx_path)},
    )


def _build_cli_args(parsed: ParsedInput, resolved_source: str) -> list[str]:
    if parsed.kind != "npx_skills":
        raise SkillToPluginError("The npx fetcher received a non-npx input.", code="unknown_input_format")
    package_spec = str(parsed.metadata.get("package_spec", "skills"))
    if not _SAFE_PACKAGE_RE.fullmatch(package_spec):
        raise SkillToPluginError("Unsafe npx package specification.", code="security_rejected")
    _validate_source_value(resolved_source)

    args = ["--yes", package_spec, "add", resolved_source]
    if parsed.select_all:
        args.append("--skill=*")
    else:
        for skill in parsed.requested_skills:
            value = _validate_option_value(skill, option="--skill", allow_star=False)
            # Keep option and value in one argv element.  Even if a downstream
            # parser is permissive, the value cannot become a second option.
            args.append(f"--skill={value}")
    if bool(parsed.metadata.get("full_depth", False)):
        args.append("--full-depth")
    args.extend(["--agent", "codex", "--copy", "--yes"])
    return args


def _classify_process_failure(stdout: str, stderr: str) -> str:
    text = f"{stdout}\n{stderr}".casefold()
    if any(marker in text for marker in ("authentication", "permission denied", "unauthorized", "forbidden", "could not read username")):
        return "authentication_failed"
    if any(marker in text for marker in ("timed out", "network", "econn", "enotfound", "could not resolve", "unable to access")):
        return "network_failed"
    return "unknown_source"


def _locate_installed_skills(snapshot_dir: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for relative_root in _KNOWN_SKILL_ROOTS:
        root = snapshot_dir / relative_root
        if not root.exists():
            continue
        if _is_link_or_reparse(root) or not root.is_dir():
            raise SkillToPluginError(
                f"The Skills CLI produced an unsafe Skill root: `{relative_root.as_posix()}`.",
                code="security_rejected",
            )
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if _is_link_or_reparse(child):
                raise SkillToPluginError(
                    f"The Skills CLI produced a linked Skill root: `{child.relative_to(snapshot_dir).as_posix()}`.",
                    code="security_rejected",
                )
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.append(child)
    if not found:
        raise SkillToPluginError(
            "The Skills CLI completed but no direct child `SKILL.md` was found in a known project-scoped Skill root.",
            code="no_skill_candidates",
        )
    return tuple(found)


class NpxFetcher:
    """Fetch project-scoped Skills through the allow-listed Skills CLI."""

    def __init__(
        self,
        *,
        npx_prefix: Sequence[str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._npx_prefix = tuple(npx_prefix) if npx_prefix is not None else None
        self._runner = runner
        self._which = which

    def fetch(
        self,
        parsed: ParsedInput,
        destination: Path,
        *,
        source_base: Path | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        extra_env: Mapping[str, str] | None = None,
    ) -> NpxFetchResult:
        if timeout_seconds <= 0:
            raise SkillToPluginError("The npx timeout must be greater than zero.", code="unknown_input_format")
        if parsed.source is None:
            raise SkillToPluginError("The npx input has no source.", code="unknown_source")

        destination = destination.resolve()
        if destination.exists():
            if _is_link_or_reparse(destination) or not destination.is_dir():
                raise SkillToPluginError("The snapshot destination must be a normal directory.", code="security_rejected")
            if any(destination.iterdir()):
                raise SkillToPluginError(
                    "The npx snapshot destination must be newly created and empty.",
                    code="output_conflict",
                    details={"snapshot_path": str(destination)},
                )
        else:
            destination.mkdir(parents=True, exist_ok=False)

        resolved_source = _resolve_source(parsed.source, (source_base or Path.cwd()).resolve())
        prefix = list(self._npx_prefix) if self._npx_prefix is not None else _resolve_npx_prefix(self._which)
        argv = prefix + _build_cli_args(parsed, resolved_source)

        with tempfile.TemporaryDirectory(prefix="agent-skill-to-plugin-npm-") as temporary:
            npm_root = Path(temporary)
            npm_user_config = npm_root / "isolated-npmrc"
            npm_user_config.write_text("", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "CI": "1",
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "submodule.recurse",
                    "GIT_CONFIG_KEY_1": "core.hooksPath",
                    "GIT_CONFIG_VALUE_0": "false",
                    "GIT_CONFIG_VALUE_1": "NUL" if os.name == "nt" else "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                    "NO_COLOR": "1",
                    "FORCE_COLOR": "0",
                    "npm_config_audit": "false",
                    "npm_config_cache": str(npm_root / "cache"),
                    "npm_config_fund": "false",
                    "npm_config_ignore_scripts": "true",
                    "npm_config_package_lock": "false",
                    "npm_config_prefix": str(npm_root / "prefix"),
                    "npm_config_update_notifier": "false",
                    "npm_config_userconfig": str(npm_user_config),
                    "npm_config_yes": "true",
                }
            )
            if extra_env:
                # Callers may provide test-only or network configuration, but
                # may not weaken the lifecycle-script or cache isolation policy.
                protected = {
                    "git_config_count",
                    "git_config_key_0",
                    "git_config_key_1",
                    "git_config_value_0",
                    "git_config_value_1",
                    "git_terminal_prompt",
                    "npm_config_cache",
                    "npm_config_ignore_scripts",
                    "npm_config_prefix",
                    "npm_config_userconfig",
                }
                for key, value in extra_env.items():
                    if key.casefold() not in protected:
                        env[key] = value

            try:
                completed = self._runner(
                    argv,
                    cwd=destination,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SkillToPluginError(
                    f"`npx skills add` exceeded the {timeout_seconds}-second timeout.",
                    code="network_failed",
                    details={
                        "stdout": sanitize_text(_as_text(exc.stdout)),
                        "stderr": sanitize_text(_as_text(exc.stderr)),
                    },
                ) from exc
            except OSError as exc:
                raise SkillToPluginError(
                    f"Could not start npx safely: {sanitize_text(str(exc))}",
                    code="dependency_missing",
                ) from exc

        stdout = sanitize_text(_as_text(completed.stdout))
        stderr = sanitize_text(_as_text(completed.stderr))
        if completed.returncode != 0:
            raise SkillToPluginError(
                "The isolated `npx skills add` acquisition failed.",
                code=_classify_process_failure(stdout, stderr),
                details={
                    "exit_code": completed.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "rewritten_argv": [sanitize_text(part) for part in argv],
                },
            )

        _validate_snapshot_links(destination)
        installed = _locate_installed_skills(destination)
        return NpxFetchResult(
            snapshot_path=destination,
            resolved_source=resolved_source,
            installed_skill_dirs=installed,
            argv=tuple(argv),
            stdout=stdout,
            stderr=stderr,
        )


__all__ = ["NpxFetchResult", "NpxFetcher"]
