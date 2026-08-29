"""Static compatibility diagnostics; imported content is never executed or rewritten."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

from .models import Diagnostic


TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bclaude\b|\banthropic\b|\.claude(?:/|\\)", re.IGNORECASE),
     "claude_product_reference", "Claude/Anthropic-specific instructions may not work unchanged in ChatGPT or Codex."),
    (re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}"),
     "claude_plugin_root", "`${CLAUDE_PLUGIN_ROOT}` is Claude-specific and is not rewritten."),
    (re.compile(r"\$\{CLAUDE_PROJECT_DIR\}"),
     "claude_project_dir", "`${CLAUDE_PROJECT_DIR}` is Claude-specific and is not rewritten."),
    (re.compile(r"(?m)(?:^|\s)/[a-z][a-z0-9_-]+\b"),
     "claude_slash_command", "The skill appears to invoke a slash command; verify an equivalent command exists."),
    (re.compile(r"\blive artifacts?\b|\bartifact_tool\b", re.IGNORECASE),
     "live_artifact", "Product-specific live artifact behavior is not adapted."),
    (re.compile(r"\b(?:mcpServers|mcp_servers|\.mcp\.json|\bMCP\b)"),
     "mcp_reference", "MCP references are preserved as text, but no MCP server is added to the skills-only plugin."),
    (re.compile(r"\b(?:lspServers|LSP)\b"),
     "lsp_reference", "LSP integrations are not converted into OpenAI skills."),
    (re.compile(r"\b(?:monitor|monitors)\b", re.IGNORECASE),
     "monitor_reference", "Claude monitor behavior is not converted."),
    (re.compile(r"\b(?:userConfig|settings\.json|hook|hooks)\b", re.IGNORECASE),
     "claude_configuration_reference", "Claude configuration or hook behavior is not converted."),
)


COMPONENT_PATHS: tuple[tuple[str, str], ...] = (
    ("commands", "Claude commands are excluded; commands are not semantically converted into skills."),
    ("agents", "Claude agents are excluded; agents are not semantically converted into skills."),
    ("hooks", "Claude hooks are excluded and are never executed."),
    (".mcp.json", "Claude MCP configuration is excluded from the skills-only plugin."),
    ("settings.json", "Claude settings are excluded from the skills-only plugin."),
    (".claude/settings.json", "Claude project settings are excluded from the skills-only plugin."),
    ("lsp", "Claude LSP components are excluded."),
    ("monitors", "Claude monitors are excluded."),
)


def scan_skill_text(skill_dir: Path) -> tuple[Diagnostic, ...]:
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    diagnostics: list[Diagnostic] = []
    for pattern, code, message in TEXT_PATTERNS:
        if pattern.search(text):
            diagnostics.append(Diagnostic(code=code, message=message, path="SKILL.md"))
    return tuple(diagnostics)


def scan_claude_components(plugin_root: Path | None) -> tuple[Diagnostic, ...]:
    if plugin_root is None:
        return ()
    diagnostics: list[Diagnostic] = []
    for relative, message in COMPONENT_PATHS:
        candidate = plugin_root / Path(relative)
        if candidate.exists():
            diagnostics.append(
                Diagnostic(
                    code="excluded_claude_component",
                    message=message,
                    path=Path(relative).as_posix(),
                    details={"converted": False},
                )
            )
    recursive_names = {
        "commands": "Claude command resources are preserved only as files and are not registered as commands.",
        "agents": "Claude agent resources are preserved only as files and are not registered as agents.",
        "hooks": "Claude hook resources are preserved only as files and are never executed by the tool.",
        "lsp": "Claude LSP resources are preserved only as files and are not registered.",
        "monitors": "Claude monitor resources are preserved only as files and are not registered.",
        ".mcp.json": "Claude MCP configuration is preserved only as source data and is not registered.",
        "settings.json": "Claude settings are preserved only as source data and are not applied.",
    }
    for candidate in plugin_root.rglob("*"):
        message = recursive_names.get(candidate.name.casefold())
        if message is None:
            continue
        relative = candidate.relative_to(plugin_root).as_posix()
        if relative in {item[0] for item in COMPONENT_PATHS}:
            continue
        diagnostics.append(
            Diagnostic(
                code="nested_claude_component",
                message=message,
                path=relative,
                details={"converted": False},
            )
        )
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    if manifest_path.is_file():
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            manifest_text = ""
        for key, message in (
            ("mcpServers", "Manifest-declared MCP servers are not copied into the skills-only plugin."),
            ("hooks", "Manifest-declared hooks are not copied or executed."),
            ("commands", "Manifest-declared command directories are not semantically converted."),
            ("agents", "Manifest-declared agent directories are not semantically converted."),
            ("lspServers", "Manifest-declared LSP servers are not converted."),
            ("dependencies", "Claude Plugin dependencies are reported but not installed automatically."),
        ):
            if f'"{key}"' in manifest_text:
                diagnostics.append(
                    Diagnostic(
                        code="excluded_claude_manifest_component",
                        message=message,
                        path=".claude-plugin/plugin.json",
                        details={"component": key, "converted": False},
                    )
                )
    return tuple(diagnostics)


def compatibility_diagnostics(skill_dirs: Iterable[Path], plugin_root: Path | None = None) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    for skill_dir in skill_dirs:
        diagnostics.extend(scan_skill_text(skill_dir))
    diagnostics.extend(scan_claude_components(plugin_root))
    unique: dict[tuple[str, str | None, str], Diagnostic] = {}
    for diagnostic in diagnostics:
        unique[(diagnostic.code, diagnostic.path, diagnostic.message)] = diagnostic
    return tuple(unique.values())
