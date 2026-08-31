"""Parse one logical Agent Skill import request without evaluating shell text.

The parser deliberately separates recognition from acquisition.  Remote text is
never interpreted as instructions: only allow-listed command grammar, URLs, and
path-shaped values contribute structured fields to :class:`ParsedInput`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
from typing import Iterable
import urllib.parse

from .errors import NeedsInputError, SkillToPluginError
from .models import ParsedInput
from .utils import SENSITIVE_QUERY_KEYS, TOKEN_PATTERN, sanitize_text


_NPX_PACKAGE_RE = re.compile(
    r"skills(?:@(?:[A-Za-z][A-Za-z0-9._-]*|[~^]?\d+(?:\.\d+){0,2}"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?))?\Z"
)
_URL_RE = re.compile(
    r"(?i)\b(?:https?|git\+https?|git|ssh)://[^\s<>()\[\]\"'`]+"
)
_SCP_GIT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+@)?"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}:[A-Za-z0-9_~./%+-]+"
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\r\n]*)\]\(([^\s)]+)(?:\s+[\"'][^)]*[\"'])?\)")
_FENCED_BLOCK_RE = re.compile(r"```[^\r\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\r\n]+)`(?!`)")
_PROMPT_RE = re.compile(r"(?i)^\s*(?:(?:PS\s+[^>\r\n]+>|PS>|\$|>)\s*)")
_COMMAND_START_RE = re.compile(r"(?i)(?<![\w.])(?:npx(?:\.cmd)?|/plugin|claude\s+plugin)\s+")
_GITHUB_SHORTHAND_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?\Z")
_PLUGIN_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_LOCAL_PATH_RE = re.compile(
    r"(?:(?:[A-Za-z]:[\\/]|\\\\|\./|\.\./|\.\\|\.\.\\|/|~/|~\\).+|"
    r"(?:[^\\/:*?\"<>|\r\n]+\\)+[^\\/:*?\"<>|\r\n]+)\Z"
)
_QUOTED_LOCAL_PATH_RE = re.compile(
    r"[\"']((?:[A-Za-z]:[\\/]|\\\\|\./|\.\./|\.\\|\.\.\\|/|~/|~\\)[^\"'\r\n]+)[\"']"
)
_LOCAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9:/])(?:[A-Za-z]:[\\/]|\\\\|\./|\.\./|\.\\|\.\.\\|/(?!/)|~/|~\\)"
    r"[^\s<>\"'`。、]+|(?<![A-Za-z0-9])(?:[^\s\\/:*?\"<>|]+\\)+[^\s\\/:*?\"<>|]+"
)
_EXPLICIT_COMBINE_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:combine|merge|bundle|port|package)\s+(?:them\s+)?all\b"
    r"|\ball\s+(?:sources?|skills?)\s+(?:in|into|as)\s+(?:a|one)\s+(?:openai\s+)?plugin\b"
    r"|(?:すべて|全部|全て).{0,20}(?:一つ|1つ|ひとつ).{0,12}(?:plugin|プラグイン).{0,8}(?:まとめ|統合)"
    r"|(?:一つ|1つ|ひとつ).{0,12}(?:plugin|プラグイン).{0,12}(?:まとめ|統合)"
    r")"
)
_SKILL_INVOCATION_LABEL_RE = re.compile(r"\$([a-z0-9]+(?:-[a-z0-9]+)*)\Z", re.IGNORECASE)

_FORBIDDEN_SHELL_PARTS: tuple[tuple[str, str], ...] = (
    ("&&", "command chaining (`&&`)"),
    ("||", "command chaining (`||`)"),
    (";", "command chaining (`;`)"),
    ("|", "a pipeline (`|`)"),
    (">", "output redirection (`>`)"),
    ("<", "input redirection (`<`)"),
    ("$(`", "command substitution"),
    ("$(", "command substitution (`$()` )"),
    ("${", "shell expansion (`${...}`)"),
)


@dataclass(frozen=True)
class _LogicalEntity:
    parsed: ParsedInput
    source_key: str
    label: str


def normalize_input_text(raw: str) -> str:
    """Normalize transport newlines and the three supported continuations."""

    text = raw.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise SkillToPluginError("The import request is empty.", code="unknown_input_format")
    if "\x00" in text:
        raise SkillToPluginError("NUL bytes are not allowed in an import request.", code="security_rejected")

    # Do not consume a backtick that belongs to a Markdown fence.  A remaining
    # single backtick inside a parsed command is rejected later.
    text = re.sub(r"\\[ \t]*\n", " ", text)
    text = re.sub(r"(?<!`)`(?!`)[ \t]*\n", " ", text)
    text = re.sub(r"(?<!\^)\^(?!\^)[ \t]*\n", " ", text)
    return text.strip()


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        value = value[1:-1]
        return value.replace("''", "'") if quote == "'" else value.replace(r'\"', '"')
    return value


def _tokenize(command: str) -> list[str]:
    try:
        # posix=False retains Windows backslashes.  Simple surrounding quotes
        # are removed explicitly so no shell semantics are inherited.
        tokens = shlex.split(command, posix=False)
    except ValueError as exc:
        raise SkillToPluginError(
            f"Could not parse command quoting: {sanitize_text(str(exc))}",
            code="unknown_input_format",
        ) from exc
    return [_strip_matching_quotes(token) for token in tokens]


def _reject_shell_syntax(command: str) -> None:
    if "\n" in command or "\r" in command:
        raise SkillToPluginError("A command must occupy one logical line.", code="security_rejected")
    if "`" in command:
        raise SkillToPluginError(
            "Backticks are not allowed except as a consumed PowerShell line continuation.",
            code="security_rejected",
        )
    for pattern, label in _FORBIDDEN_SHELL_PARTS:
        if pattern in command:
            raise SkillToPluginError(
                f"Rejected {label}; pasted commands are parsed as one allow-listed argv request.",
                code="security_rejected",
            )
    if re.search(r"(?:^|\s)&(?:\s|$)", command):
        raise SkillToPluginError("Rejected command chaining (`&`).", code="security_rejected")


def _validate_urlish_credentials(value: str) -> None:
    """Reject secrets in URL wrappers while allowing ordinary ``git@host`` SSH."""

    if TOKEN_PATTERN.search(value):
        raise SkillToPluginError(
            "The source appears to contain an access token. Use an existing credential helper or SSH configuration instead.",
            code="security_rejected",
        )

    candidate = value[4:] if value.casefold().startswith("git+") else value
    parsed = urllib.parse.urlsplit(candidate)
    scheme = parsed.scheme.casefold()
    if scheme:
        if parsed.password is not None:
            raise SkillToPluginError("URLs with embedded passwords are not allowed.", code="security_rejected")
        if scheme in {"http", "https", "git"} and parsed.username is not None:
            raise SkillToPluginError("URLs with embedded credentials are not allowed.", code="security_rejected")
        if scheme in {"file", "data", "javascript"}:
            raise SkillToPluginError(f"The `{scheme}` URL scheme is not a supported source.", code="unsupported_source")
        sensitive = sorted(
            key
            for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() in SENSITIVE_QUERY_KEYS
        )
        if sensitive:
            raise SkillToPluginError(
                "Credential-like URL query parameters are not allowed.",
                code="security_rejected",
                details={"parameters": sensitive},
            )
        return

    # SCP-like Git syntax permits a non-secret username (normally ``git``), but
    # never a token-shaped username or a user:password pair.
    scp = re.match(r"(?P<userinfo>[^@/\s]+)@(?P<host>[^:/\s]+):", value)
    if scp:
        userinfo = scp.group("userinfo")
        if ":" in userinfo or TOKEN_PATTERN.search(userinfo):
            raise SkillToPluginError("Git sources with embedded credentials are not allowed.", code="security_rejected")


def _validate_source_value(source: str) -> None:
    if not source or source.startswith("-"):
        raise SkillToPluginError("A non-option source must immediately follow the command.", code="unknown_source")
    if any(ord(char) < 32 or ord(char) == 127 for char in source):
        raise SkillToPluginError("Control characters are not allowed in a source.", code="security_rejected")
    _validate_urlish_credentials(source)


def _validate_option_value(value: str, *, option: str, allow_star: bool = False) -> str:
    if not value:
        raise SkillToPluginError(f"`{option}` requires a value.", code="unknown_input_format")
    if value.startswith("-"):
        # Prevent ``--skill=--agent`` becoming ``--skill --agent`` when argv is
        # reconstructed by the fetcher.
        raise SkillToPluginError(
            f"`{option}` values may not begin with `-`.",
            code="security_rejected",
        )
    if value == "*" and allow_star:
        return value
    if len(value) > 256 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise SkillToPluginError(f"`{option}` contains an unsafe value.", code="security_rejected")
    if any(part in value for part, _ in _FORBIDDEN_SHELL_PARTS):
        raise SkillToPluginError(f"`{option}` contains shell syntax.", code="security_rejected")
    return value


def parse_npx_command(command: str, *, raw_input: str | None = None) -> ParsedInput:
    """Parse the allow-listed ``npx skills add`` grammar into common input data."""

    command = _PROMPT_RE.sub("", command.strip(), count=1)
    _reject_shell_syntax(command)
    tokens = _tokenize(command)
    if len(tokens) < 4 or tokens[0].casefold() not in {"npx", "npx.cmd"}:
        raise SkillToPluginError("Expected `npx skills add <source>`.", code="unknown_input_format")

    index = 1
    while index < len(tokens) and tokens[index] in {"-y", "--yes"}:
        index += 1
    if index >= len(tokens):
        raise SkillToPluginError("The npm package name is missing after `npx`.", code="unknown_input_format")

    package_spec = tokens[index]
    if not _NPX_PACKAGE_RE.fullmatch(package_spec):
        raise SkillToPluginError(
            "Only the canonical unscoped `skills` npm package is allowed, optionally with a safe tag or version.",
            code="security_rejected",
            details={"package_spec": sanitize_text(package_spec)},
        )
    index += 1
    if index >= len(tokens) or tokens[index].casefold() != "add":
        raise SkillToPluginError("Only the `npx skills add` subcommand is supported.", code="unknown_input_format")
    index += 1
    if index >= len(tokens):
        raise SkillToPluginError("The skill source is missing after `add`.", code="unknown_source")

    source = tokens[index]
    _validate_source_value(source)
    index += 1
    selected: list[str] = []
    full_depth = False
    all_requested = False

    while index < len(tokens):
        token = tokens[index]
        if token in {"-g", "--global", "--copy", "-y", "--yes"}:
            index += 1
            continue
        if token in {"-l", "--list"}:
            raise SkillToPluginError("`--list` cannot produce a plugin.", code="unsupported_source")
        if token == "--full-depth":
            full_depth = True
            index += 1
            continue
        if token == "--all":
            all_requested = True
            index += 1
            continue
        if token.startswith("--skill="):
            selected.append(_validate_option_value(token.split("=", 1)[1], option="--skill", allow_star=True))
            index += 1
            continue
        if token in {"-s", "--skill"}:
            index += 1
            start = index
            while index < len(tokens) and not tokens[index].startswith("-"):
                selected.append(_validate_option_value(tokens[index], option="--skill", allow_star=True))
                index += 1
            if index == start:
                raise SkillToPluginError("`--skill` requires at least one value.", code="unknown_input_format")
            continue
        if token.startswith("--agent="):
            _validate_option_value(token.split("=", 1)[1], option="--agent")
            index += 1
            continue
        if token in {"-a", "--agent"}:
            index += 1
            start = index
            while index < len(tokens) and not tokens[index].startswith("-"):
                _validate_option_value(tokens[index], option="--agent")
                index += 1
            if index == start:
                raise SkillToPluginError("`--agent` requires at least one value.", code="unknown_input_format")
            continue
        if token.startswith("-"):
            raise SkillToPluginError(
                f"Unsupported option `{sanitize_text(token)}`; unknown options are never forwarded.",
                code="security_rejected",
            )
        raise SkillToPluginError(
            f"Unexpected positional argument `{sanitize_text(token)}`; provide only one source.",
            code="unknown_input_format",
        )

    if all_requested and selected and "*" not in selected:
        raise SkillToPluginError("Do not combine `--all` with named `--skill` values.", code="unknown_input_format")
    if "*" in selected:
        if len(set(selected)) != 1 or (selected and not all_requested):
            # A literal wildcard has the same semantics as --all, and may not be
            # mixed with semantic names.
            if any(value != "*" for value in selected):
                raise SkillToPluginError("Do not combine `*` with named Skill selections.", code="unknown_input_format")
        all_requested = True
        selected = []

    requested_skills = tuple(dict.fromkeys(selected))
    sanitized_command = sanitize_text(command)
    return ParsedInput(
        kind="npx_skills",
        raw_input=raw_input if raw_input is not None else command,
        normalized_input=sanitized_command,
        source=source,
        requested_skills=requested_skills,
        select_all=all_requested,
        plugin_scope=False,
        logical_sources=(source,),
        metadata={
            "npx_executable": tokens[0].casefold(),
            "package_spec": package_spec,
            "full_depth": full_depth,
            "sanitized_command": sanitized_command,
        },
    )


def _parse_claude_command(command: str) -> tuple[str, str]:
    command = _PROMPT_RE.sub("", command.strip(), count=1)
    _reject_shell_syntax(command)
    tokens = _tokenize(command)
    lowered = [item.casefold() for item in tokens]
    if lowered[:2] == ["/plugin", "install"] and len(tokens) == 3:
        return "install", tokens[2]
    if lowered[:3] == ["claude", "plugin", "install"] and len(tokens) == 4:
        return "install", tokens[3]
    if lowered[:3] == ["/plugin", "marketplace", "add"] and len(tokens) == 4:
        return "marketplace_add", tokens[3]
    if lowered[:4] == ["claude", "plugin", "marketplace", "add"] and len(tokens) == 5:
        return "marketplace_add", tokens[4]
    raise SkillToPluginError(
        "Unsupported Claude Plugin command. Expected marketplace add or plugin install.",
        code="unknown_input_format",
    )


def _split_plugin_spec(value: str) -> tuple[str, str]:
    if value.count("@") != 1:
        raise SkillToPluginError(
            "Claude Plugin install values must use `plugin@marketplace`.",
            code="unknown_input_format",
        )
    plugin, marketplace = value.rsplit("@", 1)
    if not _PLUGIN_PART_RE.fullmatch(plugin) or not _PLUGIN_PART_RE.fullmatch(marketplace):
        raise SkillToPluginError("Claude Plugin or Marketplace name is invalid.", code="unknown_input_format")
    return plugin, marketplace


def _claude_entity(
    *,
    raw: str,
    installs: list[str],
    marketplace_adds: list[str],
) -> _LogicalEntity | None:
    installs = list(dict.fromkeys(installs))
    marketplace_adds = list(dict.fromkeys(marketplace_adds))
    if not installs and not marketplace_adds:
        return None
    if len(installs) > 1 or len(marketplace_adds) > 1:
        choices = [
            {"id": f"claude-{index + 1}", "label": sanitize_text(value), "source": sanitize_text(value)}
            for index, value in enumerate(installs + marketplace_adds)
        ]
        raise NeedsInputError(
            "Several Claude Plugin requests were supplied; select one logical request.",
            prompt_kind="multiple_sources",
            choices=choices,
        )

    if not installs:
        source = marketplace_adds[0]
        _validate_source_value(source)
        parsed = ParsedInput(
            kind="claude_marketplace",
            raw_input=raw,
            normalized_input=source,
            source=source,
            marketplace_source=source,
            plugin_scope=True,
            logical_sources=(source,),
        )
        return _LogicalEntity(parsed, _source_key(source), f"Claude Marketplace: {sanitize_text(source)}")

    plugin, marketplace = _split_plugin_spec(installs[0])
    source = marketplace_adds[0] if marketplace_adds else None
    if source is not None:
        _validate_source_value(source)
    logical = source or f"{plugin}@{marketplace}"
    parsed = ParsedInput(
        kind="claude_plugin",
        raw_input=raw,
        normalized_input=(f"{source}\n" if source else "") + f"{plugin}@{marketplace}",
        source=source,
        marketplace_source=source,
        marketplace_name=marketplace,
        plugin_name=plugin,
        plugin_scope=True,
        logical_sources=(logical,),
        metadata={"install_spec": f"{plugin}@{marketplace}"},
    )
    key = _source_key(source) if source else f"claude:{plugin.casefold()}@{marketplace.casefold()}"
    return _LogicalEntity(parsed, key, f"Claude Plugin: {plugin}@{marketplace}")


def _command_regions(text: str) -> list[str]:
    regions = [match.group(1) for match in _FENCED_BLOCK_RE.finditer(text)]
    without_fences = _FENCED_BLOCK_RE.sub(" ", text)
    regions.extend(match.group(1) for match in _INLINE_CODE_RE.finditer(without_fences))
    regions.append(_INLINE_CODE_RE.sub(" ", without_fences))
    return regions


def _is_transport_skill_invocation_link(label: str, target: str) -> bool:
    """Recognize Codex Chat's local ``$skill`` invocation link only."""

    label_match = _SKILL_INVOCATION_LABEL_RE.fullmatch(label.strip())
    if label_match is None or not _LOCAL_PATH_RE.fullmatch(target):
        return False
    parts = [part for part in re.split(r"[\\/]+", target.rstrip("\\/")) if part]
    is_invocation = (
        len(parts) >= 3
        and parts[-1].casefold() == "skill.md"
        and parts[-2].casefold() == label_match.group(1).casefold()
        and parts[-3].casefold() == "skills"
    )
    if is_invocation:
        _reject_shell_syntax(target)
    return is_invocation


def _strip_transport_skill_invocation_links(text: str) -> str:
    """Remove UI invocation metadata without discarding user source links."""

    def replace(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        return " " if _is_transport_skill_invocation_link(label, target) else match.group(0)

    return _MARKDOWN_LINK_RE.sub(replace, text)


def _unwrap_command_markdown_autolinks(command: str) -> str:
    """Restore Chat-generated URL autolinks inside an allow-listed command."""

    def replace(match: re.Match[str]) -> str:
        label, target = match.group(1).strip(), match.group(2)
        if label != target:
            raise SkillToPluginError(
                "Markdown links inside commands must display the same source they target.",
                code="security_rejected",
                details={"label": sanitize_text(label), "target": sanitize_text(target)},
            )
        return target

    return _MARKDOWN_LINK_RE.sub(replace, command)


def _extract_commands(text: str) -> list[str]:
    commands: list[str] = []
    for region in _command_regions(text):
        for line in region.splitlines():
            line = _PROMPT_RE.sub("", line, count=1)
            match = _COMMAND_START_RE.search(line)
            if match:
                command = line[match.start():].strip()
                # Validate the complete Markdown representation before
                # unwrapping it so link titles cannot erase shell operators.
                _reject_shell_syntax(command)
                commands.append(_unwrap_command_markdown_autolinks(command))
    return list(dict.fromkeys(commands))


def _trim_url(value: str) -> str:
    return value.rstrip(".,;:!?。、「」』）")


def _extract_urls(text: str) -> list[str]:
    found = [_trim_url(match.group(2)) for match in _MARKDOWN_LINK_RE.finditer(text)]
    found.extend(_trim_url(match.group(0)) for match in _URL_RE.finditer(text))
    found.extend(_trim_url(match.group(0)) for match in _SCP_GIT_RE.finditer(text))
    return list(dict.fromkeys(item for item in found if item))


def _normalize_url(value: str) -> str:
    _validate_urlish_credentials(value)
    wrapper = "git+" if value.casefold().startswith("git+") else ""
    parsed = urllib.parse.urlsplit(value[len(wrapper):])
    host = (parsed.hostname or "").casefold()
    if not parsed.scheme or not host:
        return value
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise SkillToPluginError("The source URL has an invalid port.", code="unknown_source") from exc
    port = f":{parsed_port}" if parsed_port is not None else ""
    rendered_host = f"[{host}]" if ":" in host else host
    ssh_user = f"{parsed.username}@" if parsed.scheme.casefold() == "ssh" and parsed.username else ""
    netloc = ssh_user + rendered_host + port
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if host in {"github.com", "www.github.com"}:
        netloc = "github.com"
        path = path.rstrip("/")
        query = ""
    else:
        query = parsed.query
    return wrapper + urllib.parse.urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def _source_key(source: str | None) -> str:
    if source is None:
        return ""
    if _GITHUB_SHORTHAND_RE.fullmatch(source):
        owner, repo = source.removesuffix(".git").split("/", 1)
        return f"https://github.com/{owner.casefold()}/{repo.casefold()}"
    if re.match(r"(?i)^(?:https?|git\+https?|git|ssh)://", source):
        return _normalize_url(source).casefold()
    return str(Path(source).expanduser()).replace("\\", "/").casefold()


def _parse_url_or_source(raw: str, source: str) -> ParsedInput:
    _validate_source_value(source)
    normalized = _normalize_url(source) if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", source) else source
    parsed = urllib.parse.urlsplit(normalized[4:] if normalized.casefold().startswith("git+") else normalized)
    host = (parsed.hostname or "").casefold()
    lower_path = urllib.parse.unquote(parsed.path).casefold()
    metadata: dict[str, object] = {}
    requested_path: str | None = None
    kind = "url"

    if host in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
        segments = [segment for segment in parsed.path.split("/") if segment]
        kind = "github_repository"
        if host == "raw.githubusercontent.com":
            kind = "github_path"
            metadata["github_view"] = "raw"
            metadata["github_tail"] = "/".join(segments[2:]) if len(segments) > 2 else ""
        elif len(segments) >= 3 and segments[2].casefold() in {"tree", "blob", "raw"}:
            kind = "github_path"
            metadata["github_view"] = segments[2].casefold()
            metadata["github_tail"] = "/".join(segments[3:])
        if lower_path.endswith("/skill.md"):
            kind = "skill_manifest_url"
    elif lower_path.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        kind = "archive_url"
    elif lower_path.endswith("/skill.md") or lower_path == "skill.md":
        kind = "skill_manifest_url"
    elif parsed.scheme.casefold() in {"git", "ssh"} or lower_path.endswith(".git"):
        kind = "git_url"

    return ParsedInput(
        kind=kind,
        raw_input=raw,
        normalized_input=normalized,
        source=normalized,
        requested_path=requested_path,
        plugin_scope=False,
        logical_sources=(normalized,),
        metadata=metadata,
    )


def _local_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _INLINE_CODE_RE.finditer(_FENCED_BLOCK_RE.sub(" ", text)):
        value = match.group(1).strip()
        if _LOCAL_PATH_RE.fullmatch(value):
            candidates.append(value)
    for line in text.splitlines():
        value = _PROMPT_RE.sub("", line.strip(), count=1)
        if _LOCAL_PATH_RE.fullmatch(value):
            candidates.append(value)
    if _LOCAL_PATH_RE.fullmatch(text.strip()):
        candidates.append(text.strip())
    candidates.extend(match.group(1).strip() for match in _QUOTED_LOCAL_PATH_RE.finditer(text))
    token_text = _QUOTED_LOCAL_PATH_RE.sub(" ", text)
    token_text = _INLINE_CODE_RE.sub(" ", token_text)
    candidates.extend(
        match.group(0).rstrip(".,;:!?。、「」』）")
        for match in _LOCAL_TOKEN_RE.finditer(token_text)
    )
    return list(
        dict.fromkeys(
            value
            for value in candidates
            if value and not value.casefold().startswith("/plugin")
        )
    )


def _needs_source_choice(entities: Iterable[_LogicalEntity]) -> NeedsInputError:
    items = list(entities)
    choices = [
        {
            "id": f"source-{index + 1}",
            "label": item.label,
            "kind": item.parsed.kind,
            "source": sanitize_text(item.parsed.source or item.parsed.normalized_input),
        }
        for index, item in enumerate(items)
    ]
    choices.append(
        {
            "id": "combine-all",
            "label": "Combine all listed sources into one OpenAI Plugin",
            "sources": [item["id"] for item in choices],
        }
    )
    return NeedsInputError(
        "Several unrelated acquisition sources were found. Select one source or explicitly combine all of them.",
        prompt_kind="multiple_sources",
        choices=choices,
    )


def parse_input(raw: str) -> ParsedInput:
    """Parse exactly one logical request or raise a structured safe-choice error."""

    text = normalize_input_text(raw)
    semantic_text = _strip_transport_skill_invocation_links(text)
    entities: list[_LogicalEntity] = []
    installs: list[str] = []
    marketplace_adds: list[str] = []

    for command in _extract_commands(semantic_text):
        if re.match(r"(?i)^npx(?:\.cmd)?\s+", command):
            parsed = parse_npx_command(command, raw_input=raw)
            entities.append(
                _LogicalEntity(parsed, _source_key(parsed.source), f"npx source: {sanitize_text(parsed.source or '')}")
            )
        else:
            kind, value = _parse_claude_command(command)
            if kind == "install":
                installs.append(value)
            else:
                marketplace_adds.append(value)

    claude = _claude_entity(raw=raw, installs=installs, marketplace_adds=marketplace_adds)
    if claude is not None:
        entities.append(claude)

    occupied = {item.source_key for item in entities}
    for source in _extract_urls(semantic_text):
        key = _source_key(source)
        if key in occupied:
            continue
        parsed = _parse_url_or_source(raw, source)
        entities.append(_LogicalEntity(parsed, key, f"URL: {sanitize_text(parsed.source or source)}"))
        occupied.add(key)

    for source in _local_candidates(semantic_text):
        key = _source_key(source)
        if key in occupied:
            continue
        parsed = ParsedInput(
            kind="local",
            raw_input=raw,
            normalized_input=source,
            source=source,
            logical_sources=(source,),
        )
        entities.append(_LogicalEntity(parsed, key, f"Local path: {sanitize_text(source)}"))
        occupied.add(key)

    if not entities:
        stripped = semantic_text.strip().strip("`").strip()
        if _GITHUB_SHORTHAND_RE.fullmatch(stripped):
            _validate_source_value(stripped)
            parsed = ParsedInput(
                kind="github_shorthand",
                raw_input=raw,
                normalized_input=stripped.removesuffix(".git"),
                source=stripped,
                logical_sources=(stripped,),
            )
            entities.append(_LogicalEntity(parsed, _source_key(stripped), f"GitHub: {stripped}"))

    # Exact duplicate mentions (for example a Markdown URL as both label and
    # target) are one logical source.  Rich command forms win over bare URLs.
    rich_groups: dict[str, list[_LogicalEntity]] = {}
    for entity in entities:
        if entity.parsed.kind in {"npx_skills", "claude_plugin"}:
            rich_groups.setdefault(entity.source_key, []).append(entity)
    for group in rich_groups.values():
        distinct_requests = {item.parsed.normalized_input for item in group}
        if len(distinct_requests) > 1:
            raise _needs_source_choice(group)

    deduplicated: dict[str, _LogicalEntity] = {}
    for entity in entities:
        existing = deduplicated.get(entity.source_key)
        if existing is None or entity.parsed.kind in {"npx_skills", "claude_plugin"}:
            deduplicated[entity.source_key] = entity
    entities = list(deduplicated.values())

    if not entities:
        raise SkillToPluginError(
            "Could not identify an npx command, Claude Plugin request, supported URL, GitHub shorthand, or local path.",
            code="unknown_input_format",
            details={"sanitized_input": sanitize_text(text)},
        )
    if len(entities) > 1:
        if _EXPLICIT_COMBINE_RE.search(semantic_text):
            return ParsedInput(
                kind="multi_source",
                raw_input=raw,
                normalized_input="\n".join(item.parsed.normalized_input for item in entities),
                select_all=True,
                plugin_scope=False,
                logical_sources=tuple(item.parsed.source or item.parsed.normalized_input for item in entities),
                metadata={"sources": [item.parsed.to_dict() for item in entities], "combination": "explicit-all"},
            )
        raise _needs_source_choice(entities)
    return entities[0].parsed


# Descriptive alias for callers that prefer the workflow term used in docs.
parse_logical_import_request = parse_input


__all__ = [
    "normalize_input_text",
    "parse_input",
    "parse_logical_import_request",
    "parse_npx_command",
]
