"""Safe Git transport that never checks out untrusted repository content."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Sequence
import urllib.parse

from ..errors import SkillToPluginError
from ..limits import DEFAULT_TIMEOUT_SECONDS, MAX_REPORT_TEXT
from ..utils import TOKEN_PATTERN, sanitize_text, validate_url_credentials


@dataclass(frozen=True)
class GitRefs:
    default_ref: str | None
    head_commit: str | None
    refs: dict[str, str]


@dataclass(frozen=True)
class GitFetchResult:
    source: str
    requested_ref: str | None
    resolved_ref: str | None
    commit: str
    snapshot_path: Path
    skipped_symbolic_links: tuple[str, ...] = ()


def _null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


class GitFetcher:
    """Fetch a fixed Git commit into a validated filesystem snapshot.

    Repository files are materialized through local ``git archive`` rather than
    ``git checkout``. This avoids repository hooks, submodules, and checkout
    filters from the untrusted source.
    """

    def __init__(self, executable: str | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.executable = executable or shutil.which("git") or ""
        self.timeout = timeout
        if not self.executable:
            raise SkillToPluginError("Git is required for this source but was not found.", code="dependency_missing")

    @staticmethod
    def validate_source(source: str) -> None:
        if not source or source.startswith("-"):
            raise SkillToPluginError("Invalid Git source.", code="unknown_source")
        lowered = source.casefold().strip()
        if lowered.startswith(("ext::", "file://", "git+file://")) or "::" in lowered.split("@", 1)[0]:
            raise SkillToPluginError("Git ext/file transports are not allowed for remote sources.", code="security_rejected")
        if re.search(r"[\x00\r\n]", source):
            raise SkillToPluginError("Control characters are not allowed in a Git source.", code="security_rejected")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", source):
            scheme = urllib.parse.urlsplit(source).scheme.casefold()
            if scheme not in {"https", "ssh", "git"}:
                raise SkillToPluginError(f"Unsupported Git URL scheme `{scheme}`.", code="unsupported_source")
            # SSH URLs conventionally include a non-secret username such as
            # ``git``. Passwords, token-shaped text, and credential query
            # parameters remain rejected by the shared validator.
            validate_url_credentials(source, allow_username=scheme == "ssh")
        elif re.match(r"^[^/@\s]+@[^:\s]+:.+$", source):
            # SCP-like SSH syntax. It has a username but no place for a password.
            if any(char in source for char in "\r\n\x00"):
                raise SkillToPluginError("Invalid SCP-like Git source.", code="security_rejected")
            userinfo = source.split("@", 1)[0]
            if ":" in userinfo or TOKEN_PATTERN.search(userinfo):
                raise SkillToPluginError("SCP-like Git sources may not embed credentials.", code="security_rejected")
        else:
            raise SkillToPluginError("The value is not a supported remote Git URL.", code="unknown_source")

    def _base_argv(self) -> list[str]:
        return [
            self.executable,
            "-c", "protocol.ext.allow=never",
            "-c", f"core.hooksPath={_null_device()}",
            "-c", "submodule.recurse=false",
        ]

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        })
        argv = self._base_argv() + list(args)
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SkillToPluginError(
                "Git operation timed out.",
                code="network_failed",
                details={"stderr": sanitize_text(str(exc.stderr or ""))},
            ) from exc
        except OSError as exc:
            raise SkillToPluginError(f"Could not start Git safely: {exc}", code="dependency_missing") from exc
        if completed.returncode:
            stderr = sanitize_text(completed.stderr, MAX_REPORT_TEXT)
            lowered = stderr.casefold()
            if any(marker in lowered for marker in (
                "authentication failed", "permission denied", "could not read username",
                "terminal prompts disabled", "publickey", "access denied",
            )):
                code = "authentication_failed"
            elif any(marker in lowered for marker in (
                "could not resolve host", "unable to access", "connection timed out",
                "connection refused", "network is unreachable", "tls",
            )):
                code = "network_failed"
            else:
                code = "unknown_source"
            raise SkillToPluginError(
                "Git could not resolve or fetch the requested source.",
                code=code,
                details={"exit_code": completed.returncode, "stderr": stderr},
            )
        return completed

    def list_refs(self, source: str) -> GitRefs:
        self.validate_source(source)
        result = self._run(["ls-remote", "--symref", source, "HEAD", "refs/heads/*", "refs/tags/*"])
        default_ref: str | None = None
        head_commit: str | None = None
        refs: dict[str, str] = {}
        peeled: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line.startswith("ref: ") and line.endswith("\tHEAD"):
                default_ref = line[5:].split("\t", 1)[0]
                continue
            if "\t" not in line:
                continue
            commit, name = line.split("\t", 1)
            if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                continue
            commit = commit.lower()
            if name == "HEAD":
                head_commit = commit
            elif name.endswith("^{}"):
                peeled[name[:-3]] = commit
            else:
                refs[name] = commit
        refs.update(peeled)
        return GitRefs(default_ref=default_ref, head_commit=head_commit, refs=refs)

    @staticmethod
    def resolve_ref(refs: GitRefs, requested_ref: str | None) -> tuple[str | None, str]:
        if requested_ref:
            requested_ref = requested_ref.removeprefix("refs/")
            candidates = [
                f"refs/{requested_ref}",
                f"refs/heads/{requested_ref}",
                f"refs/tags/{requested_ref}",
            ]
            for candidate in candidates:
                if candidate in refs.refs:
                    return candidate, refs.refs[candidate]
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", requested_ref):
                matching = sorted({commit for commit in refs.refs.values() if commit.startswith(requested_ref.lower())})
                if refs.head_commit and refs.head_commit.startswith(requested_ref.lower()):
                    matching = sorted(set(matching + [refs.head_commit]))
                if len(matching) == 1:
                    return requested_ref.lower(), matching[0]
                # A full SHA may be fetchable even when it is not an advertised ref.
                if len(requested_ref) == 40:
                    return requested_ref.lower(), requested_ref.lower()
            raise SkillToPluginError(
                f"Git ref `{sanitize_text(requested_ref)}` was not found or was ambiguous.",
                code="unknown_source",
            )
        if refs.default_ref and refs.default_ref in refs.refs:
            return refs.default_ref, refs.refs[refs.default_ref]
        if refs.head_commit:
            return "HEAD", refs.head_commit
        raise SkillToPluginError("The Git repository did not advertise a default branch.", code="unknown_source")

    def fetch(
        self,
        source: str,
        destination: Path,
        *,
        requested_ref: str | None = None,
        expected_commit: str | None = None,
    ) -> GitFetchResult:
        """Fetch and archive one immutable commit into a new destination."""
        self.validate_source(source)
        if destination.exists():
            raise SkillToPluginError(f"Snapshot destination already exists: `{destination}`.", code="output_conflict")
        refs = self.list_refs(source)
        resolved_ref, advertised_commit = self.resolve_ref(refs, requested_ref)
        if expected_commit and advertised_commit != expected_commit.lower():
            raise SkillToPluginError(
                "The resolved Git commit changed before it could be pinned.",
                code="resolution_integrity_failed",
                details={"expected": expected_commit.lower(), "actual": advertised_commit},
            )

        with tempfile.TemporaryDirectory(prefix="agent-skill-to-plugin-git-") as temporary_name:
            temporary = Path(temporary_name)
            bare = temporary / "repo.git"
            archive_path = temporary / "snapshot.tar"
            self._run(["init", "--bare", str(bare)], cwd=temporary)
            fetch_target = resolved_ref or advertised_commit
            self._run(
                ["--git-dir", str(bare), "fetch", "--no-tags", "--depth=1", source, fetch_target],
                cwd=temporary,
            )
            commit_result = self._run(
                ["--git-dir", str(bare), "rev-parse", "FETCH_HEAD^{commit}"],
                cwd=temporary,
            )
            commit = commit_result.stdout.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise SkillToPluginError("Git returned an invalid commit identifier.", code="resolution_integrity_failed")
            if commit != advertised_commit:
                raise SkillToPluginError(
                    "Fetched Git commit does not match the advertised immutable commit.",
                    code="resolution_integrity_failed",
                    details={"advertised": advertised_commit, "fetched": commit},
                )
            self._run(
                ["--git-dir", str(bare), "archive", "--format=tar", f"--output={archive_path}", commit],
                cwd=temporary,
            )
            # Imported lazily to keep Git-only callers free of archive dependencies.
            from .archive import extract_archive

            extraction = extract_archive(
                archive_path,
                destination,
                skip_symbolic_links=True,
            )

        return GitFetchResult(
            source=source,
            requested_ref=requested_ref,
            resolved_ref=resolved_ref,
            commit=commit,
            snapshot_path=destination,
            skipped_symbolic_links=extraction.skipped_symbolic_links,
        )
