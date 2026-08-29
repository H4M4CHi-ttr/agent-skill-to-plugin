"""Machine-readable and human-readable conversion reports."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .utils import atomic_write_text


def _markdown_text(value: Any) -> str:
    """Render untrusted data as one escaped Markdown text fragment."""

    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    return re.sub(r"([\\`*_{}\[\]<>()#+.!|])", r"\\\1", text)


def _markdown_code(value: Any) -> str:
    """Render an arbitrary one-line value in a non-breakable code span."""

    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}" if "`" in text else f"{fence}{text}{fence}"


def render_markdown(report: dict[str, Any]) -> str:
    provenance = report.get("provenance", {})
    lines = [
        f"# Conversion report: {_markdown_text(report['plugin_name'])}",
        "",
        f"- Created: {_markdown_code(report['created_at'])}",
        f"- Tool: {_markdown_code('agent-skill-to-plugin ' + str(provenance.get('tool_version', 'unknown')))}",
        f"- Input kind: {_markdown_code(provenance.get('input_kind', 'unknown'))}",
        f"- Normalized source: {_markdown_code(provenance.get('normalized_source', ''))}",
        f"- Repository: {_markdown_code(provenance.get('repository_url') or 'not applicable')}",
        f"- Requested ref: {_markdown_code(provenance.get('requested_ref') or 'not specified')}",
        f"- Resolved commit: {_markdown_code(provenance.get('resolved_commit') or 'not applicable')}",
        f"- Marketplace: {_markdown_code(provenance.get('marketplace_name') or 'not applicable')}",
        f"- Original plugin: {_markdown_code(provenance.get('original_plugin_name') or 'not applicable')}",
        f"- Source snapshot SHA-256: {_markdown_code(provenance.get('source_snapshot_sha256', ''))}",
        f"- Generated plugin tree SHA-256: {_markdown_code(report['plugin_tree_sha256'])}",
        f"- ZIP SHA-256: {_markdown_code(report['zip_sha256'])}",
        "",
        "## Bundled skills",
        "",
    ]
    for skill in report.get("skills", []):
        lines.extend([
            f"### {_markdown_code(skill['name'])}",
            "",
            f"- Source path: {_markdown_code(skill['path'])}",
            f"- Candidate ID: {_markdown_code(skill['candidate_id'])}",
            f"- Source tree SHA-256: {_markdown_code(skill['tree_sha256'])}",
            f"- Description: {_markdown_text(skill['description'])}",
            "",
        ])
    lines.extend(["## License evidence", ""])
    findings = provenance.get("license_findings") or []
    if findings:
        for finding in findings:
            label = finding.get("spdx_id") or finding.get("license") or finding.get("name") or "detected file"
            source = finding.get("path") or finding.get("url") or finding.get("source") or "unknown"
            lines.append(f"- {_markdown_text(label)}: {_markdown_code(source)}")
    else:
        lines.append("- No license evidence was detected. Redistribution rights have not been established.")
    lines.extend(["", "## Compatibility adaptations", ""])
    adaptations = report.get("compatibility_adaptations") or []
    if adaptations:
        lines.append(
            "The following changes apply only to the generated copy. The fixed source snapshot and source hashes remain unchanged."
        )
        lines.append("")
        for adaptation in adaptations:
            source_hash = adaptation.get("source_sha256") or "not present in source"
            lines.append(
                f"- **{_markdown_code(adaptation.get('skill', 'unknown'))}** "
                f"({_markdown_code(adaptation.get('path', 'unknown'))}): "
                f"{_markdown_text(adaptation.get('change', 'metadata normalized'))}. "
                f"Reason: {_markdown_text(adaptation.get('reason', 'OpenAI compatibility'))}. "
                f"Source SHA-256: {_markdown_code(source_hash)}; "
                f"generated SHA-256: {_markdown_code(adaptation.get('generated_sha256', 'unknown'))}."
            )
            for label, key in (
                ("Fields added", "added_fields"),
                ("Fields removed", "removed_fields"),
                ("Fields changed", "changed_fields"),
            ):
                fields = adaptation.get(key) or []
                if fields:
                    lines.append(
                        f"  - {label}: " + ", ".join(_markdown_code(field) for field in fields)
                    )
    else:
        lines.append("- No generated-copy compatibility adaptation was applied.")

    lines.extend(["", "## Compatibility and security diagnostics", ""])
    diagnostics = report.get("diagnostics") or []
    if diagnostics:
        for diagnostic in diagnostics:
            path = f" ({_markdown_code(diagnostic['path'])})" if diagnostic.get("path") else ""
            severity = _markdown_text(diagnostic.get("severity", "warning"))
            code = _markdown_text(diagnostic["code"])
            message = _markdown_text(diagnostic["message"])
            lines.append(f"- **{severity} / {code}**{path}: {message}")
    else:
        lines.append("- No static warnings were detected. This is not a trust or behavioral guarantee.")
    lines.extend([
        "",
        "## Generated artifacts",
        "",
        f"- Plugin directory: {_markdown_code(report['plugin_dir'])}",
        f"- ZIP: {_markdown_code(report['zip_path'])}",
        f"- Marketplace root: {_markdown_code(report['marketplace_root'])}",
        f"- Marketplace file: {_markdown_code(report['marketplace_file'])}",
        f"- Register manually: {_markdown_code(report['marketplace_add_command'])}",
        "",
        "The tool did not register the marketplace, install the plugin, modify a user home directory, or publish anything.",
        "",
    ])
    return "\n".join(lines)


def write_reports(json_path: Path, markdown_path: Path, report: dict[str, Any]) -> None:
    atomic_write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(markdown_path, render_markdown(report))
