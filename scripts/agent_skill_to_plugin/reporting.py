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
    lines.extend(["", "## Personal Marketplace registration", ""])
    registration = report.get("personal_marketplace")
    if isinstance(registration, dict):
        status = registration.get("status", "unknown")
        lines.append(f"- Status: {_markdown_code(status)}")
        if status == "failed":
            retry = registration.get("registration_retry")
            failed_lines = [
                f"- Error code: {_markdown_code(registration.get('error_code', 'unknown'))}",
                f"- Error: {_markdown_text(registration.get('error', 'Personal registration did not complete.'))}",
            ]
            if registration.get("failure_state"):
                failed_lines.append(f"- Failure state: {_markdown_code(registration['failure_state'])}")
            if registration.get("lock_path"):
                failed_lines.append(f"- Lock retained at: {_markdown_code(registration['lock_path'])}")
            if registration.get("backup_path"):
                failed_lines.append(f"- Backup retained at: {_markdown_code(registration['backup_path'])}")
            if registration.get("recovery"):
                failed_lines.append(f"- Recovery: {_markdown_text(registration['recovery'])}")
            lines.extend(failed_lines)
            if isinstance(retry, dict) and retry.get("plugin_dir"):
                lines.extend([
                    "",
                    "Personal Marketplace registration did not complete. The validated workspace Plugin remains available for an explicit `register-personal` retry after the reported condition is resolved.",
                ])
                force_suffix = " --force-personal" if retry.get("force_personal_required") else ""
                lines.append(
                    "Retry: "
                    + _markdown_code(
                        "agent-skill-to-plugin register-personal --plugin-dir "
                        f'"{retry["plugin_dir"]}"{force_suffix}'
                    )
                )
            else:
                lines.extend([
                    "",
                    "Personal Marketplace registration did not complete and retained recovery state requires inspection before any retry. The validated workspace Plugin remains available.",
                ])
        elif registration.get("commit_durable"):
            lines.extend([
                f"- Error code: {_markdown_code(registration.get('error_code', 'unknown'))}",
                f"- Error: {_markdown_text(registration.get('error', 'Personal registration needs recovery review.'))}",
                f"- Durable commit: {_markdown_code(str(bool(registration.get('commit_durable'))).lower())}",
                f"- Commit verified: {_markdown_code(str(bool(registration.get('commit_verified'))).lower())}",
                f"- Lock retained at: {_markdown_code(registration.get('lock_path', 'unknown'))}",
                f"- Recovery: {_markdown_text(registration.get('recovery', 'Inspect the retained lock and transaction journal before retrying.'))}",
                "",
                "The personal Marketplace may already contain the new Plugin state. Inspect the retained recovery metadata before retrying; no installation or reinstallation was performed.",
            ])
        else:
            lines.extend([
                f"- Marketplace: {_markdown_code(registration.get('marketplace_name', 'personal'))}",
                f"- Plugin directory: {_markdown_code(registration.get('plugin_dir', 'unknown'))}",
                f"- Marketplace file: {_markdown_code(registration.get('marketplace_file', 'unknown'))}",
                f"- Installation policy: {_markdown_code(registration.get('policy_installation', 'AVAILABLE'))}",
                f"- Authentication policy: {_markdown_code(registration.get('policy_authentication', 'ON_INSTALL'))}",
                f"- Category: {_markdown_code(registration.get('category', 'Productivity'))}",
                f"- Plugin installation performed: {_markdown_code(str(bool(registration.get('installation_performed', False))).lower())}",
                f"- Reinstallation required: {_markdown_code(str(bool(registration.get('reinstall_required', False))).lower())}",
                f"- View: {_markdown_code(registration.get('view_url', 'unavailable'))}",
                f"- Share: {_markdown_code(registration.get('share_url', 'unavailable'))}",
                "",
                "The Plugin was registered in the personal Marketplace. Plugin installation or reinstallation, publication, and repository push were not performed.",
            ])
            if registration.get("reinstall_required"):
                lines.append(
                    "An existing personal Plugin registration was updated. Reinstall it explicitly before expecting an already-installed cached copy to use the new files."
                )
    else:
        lines.append("- Personal Marketplace registration was disabled for this conversion.")

    lines.extend([
        "",
        "## Workspace artifacts",
        "",
        f"- Validated Plugin copy: {_markdown_code(report['plugin_dir'])}",
        f"- Conversion workspace: {_markdown_code(report['marketplace_root'])}",
        f"- Workspace Marketplace file: {_markdown_code(report['marketplace_file'])}",
        "",
    ])
    return "\n".join(lines)


def write_reports(json_path: Path, markdown_path: Path, report: dict[str, Any]) -> None:
    atomic_write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(markdown_path, render_markdown(report))
