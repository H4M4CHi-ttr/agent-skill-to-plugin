"""Command-line interface with stable JSON envelopes and exit codes."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from .application import (
    RegistrationOutcome,
    ResolutionOutcome,
    convert_resolution,
    register_plugin_directory,
    resolve_request,
    run_request,
)
from .errors import ExitCode, NeedsInputError, SkillToPluginError
from .limits import DEFAULT_PLUGIN_VERSION, DEFAULT_TIMEOUT_SECONDS, SCHEMA_VERSION, TOOL_VERSION
from .models import ConversionResult
from .utils import sanitize_text


MAX_INPUT_BYTES = 1024 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SkillToPluginError(message, code="unknown_input_format")


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input-file", type=Path, help="UTF-8 file containing one logical import request")
    group.add_argument("--input", help="One logical import request (input-file is safer for agents)")
    parser.add_argument("--output-root", type=Path, default=Path("converted-skills-marketplace"))
    parser.add_argument("--source-base", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="Print one JSON object to stdout")


def _add_packaging_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plugin-name")
    parser.add_argument("--display-name")
    parser.add_argument("--author-name", default="Local conversion")
    parser.add_argument("--version", default=DEFAULT_PLUGIN_VERSION)
    parser.add_argument("--force", action="store_true", help="Explicitly replace workspace artifacts with the same name")
    parser.add_argument(
        "--force-personal",
        action="store_true",
        help="Separately authorize replacement of a divergent same-name personal Plugin registration",
    )
    parser.add_argument(
        "--register-personal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Register in the standard personal Marketplace (default: enabled)",
    )
    parser.add_argument(
        "--show-zip",
        action="store_true",
        help="Print the generated distribution ZIP path and SHA-256",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="agent-skill-to-plugin", description="Safely package Agent Skills as OpenAI skills-only plugins.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Resolve and convert, or return needs_selection")
    _add_input_arguments(run)
    _add_packaging_arguments(run)

    resolve = subparsers.add_parser("resolve", help="Resolve to a pinned snapshot without packaging")
    _add_input_arguments(resolve)

    convert = subparsers.add_parser("convert", help="Convert only from a saved fixed resolution")
    convert.add_argument("--resolution", type=Path, required=True)
    convert.add_argument(
        "--select",
        action="append",
        help="Candidate ID, exact Skill name, path, or `all`; repeat for multiple explicit candidates",
    )
    _add_packaging_arguments(convert)
    convert.add_argument("--json", action="store_true", help="Print one JSON object to stdout")

    register_personal = subparsers.add_parser(
        "register-personal",
        help="Register an already generated Plugin without resolving or packaging again",
    )
    register_personal.add_argument("--plugin-dir", type=Path, required=True)
    register_personal.add_argument(
        "--force-personal",
        action="store_true",
        help="Authorize replacement of a divergent same-name personal Plugin registration",
    )
    register_personal.add_argument("--json", action="store_true", help="Print one JSON object to stdout")
    return parser


def _read_input(args: argparse.Namespace) -> str:
    if args.input is not None:
        return args.input
    path: Path = args.input_file
    if path.is_symlink() or not path.is_file():
        raise SkillToPluginError("Input file is missing or is a symbolic link.", code="unknown_input_format")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise SkillToPluginError("Input file exceeds the 1 MiB safety limit.", code="security_rejected")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillToPluginError(f"Could not read UTF-8 input: {sanitize_text(str(exc))}", code="unknown_input_format") from exc


def make_payload(status: str, data: dict[str, Any] | None = None, *, error_code: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "agent-skill-to-plugin", "version": TOOL_VERSION},
        "status": status,
        "error_code": error_code,
    }
    if data:
        copied = dict(data)
        copied.pop("schema_version", None)
        copied.pop("status", None)
        payload.update(copied)
    return payload


def result_payload(result: ConversionResult | ResolutionOutcome | RegistrationOutcome) -> tuple[dict[str, Any], int]:
    if isinstance(result, RegistrationOutcome):
        return make_payload("ok", result.to_dict()), int(ExitCode.OK)
    if isinstance(result, ResolutionOutcome):
        data = result.to_dict()
        status = data.get("status", "resolved")
        exit_code = ExitCode.NEEDS_SELECTION if status == "needs_selection" else ExitCode.OK
        return make_payload(status, data), int(exit_code)
    return make_payload("ok", result.to_dict()), int(ExitCode.OK)


def _print_personal_registration(registration: Any) -> None:
    print(
        "Personal Marketplace: "
        f"{registration.status} ({registration.marketplace_name})"
    )
    print(f"Registered plugin directory: {registration.plugin_dir}")
    print(f"Marketplace file: {registration.marketplace_file}")
    print("Plugin installation performed: no")
    if registration.reinstall_required:
        print("Reinstallation required: yes (an existing personal Plugin registration was updated)")
    print(f"View: {registration.view_url}")
    print(f"Share: {registration.share_url}")


def _human(
    result: ConversionResult | ResolutionOutcome | RegistrationOutcome,
    *,
    show_zip: bool = False,
) -> None:
    if isinstance(result, RegistrationOutcome):
        _print_personal_registration(result.registration)
        if result.warnings:
            print("Warnings:")
            for warning in result.warnings:
                print(f"  - {warning}")
        return
    if isinstance(result, ResolutionOutcome):
        if result.decision.needs_selection:
            print(f"Selection required (resolution {result.state.resolution_id}):")
            for index, candidate in enumerate(result.decision.available, start=1):
                validity = "valid" if candidate.valid else "invalid"
                plugin = f"; plugin={candidate.plugin}" if candidate.plugin else ""
                print(f"  {index}. {candidate.name or '<invalid>'} [{validity}] — {candidate.path}{plugin}")
                if candidate.description:
                    print(f"     {candidate.description}")
                print(f"     {candidate.selection_reason}; id={candidate.id}")
                for diagnostic in candidate.diagnostics:
                    print(f"     {diagnostic.severity}: {diagnostic.message}")
            print(f"Resume with: agent-skill-to-plugin convert --resolution \"{result.state.resolution_file}\" --select <candidate-id>")
        else:
            print(f"Resolved {result.state.resolution_id} at {result.state.resolution_file}")
        return
    print(f"Created plugin: {result.plugin_name}")
    print("Skills: " + ", ".join(skill.name for skill in result.skills))
    registration = result.personal_marketplace
    if registration is not None:
        _print_personal_registration(registration)
    else:
        print("Personal Marketplace: not registered")
        print(f"Generated plugin directory: {result.plugin_dir}")
    if show_zip:
        print(f"ZIP: {result.zip_path}")
        print(f"ZIP SHA-256: {result.zip_sha256}")
    print(f"JSON report: {result.report_json}")
    print(f"Markdown report: {result.report_markdown}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"  - {warning}")


def _selectors(values: Sequence[str] | None) -> str | tuple[str, ...] | None:
    if not values:
        return None
    flattened: list[str] = []
    for value in values:
        flattened.extend(item.strip() for item in value.split(",") if item.strip())
    if len(flattened) == 1:
        return flattened[0]
    return tuple(flattened)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    json_requested = "--json" in arguments
    try:
        args = build_argument_parser().parse_args(arguments)
        if getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS) <= 0:
            raise SkillToPluginError("Timeout must be greater than zero.", code="unknown_input_format")
        if args.command == "register-personal":
            result: ConversionResult | ResolutionOutcome | RegistrationOutcome = register_plugin_directory(
                args.plugin_dir,
                force_personal=args.force_personal,
            )
        elif args.command == "resolve":
            result = resolve_request(
                _read_input(args),
                output_root=args.output_root,
                source_base=args.source_base,
                timeout_seconds=args.timeout,
            )
        elif args.command == "run":
            result = run_request(
                _read_input(args),
                output_root=args.output_root,
                source_base=args.source_base,
                timeout_seconds=args.timeout,
                requested_plugin_name=args.plugin_name,
                display_name=args.display_name,
                author_name=args.author_name,
                version=args.version,
                force=args.force,
                register_personal=args.register_personal,
                force_personal=args.force_personal,
            )
        else:
            result = convert_resolution(
                args.resolution,
                selected=_selectors(args.select),
                requested_plugin_name=args.plugin_name,
                display_name=args.display_name,
                author_name=args.author_name,
                version=args.version,
                force=args.force,
                register_personal=args.register_personal,
                force_personal=args.force_personal,
            )
        payload, code = result_payload(result)
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _human(result, show_zip=getattr(args, "show_zip", False))
        return code
    except NeedsInputError as exc:
        payload = make_payload(
            "needs_input",
            {
                "error": exc.message,
                "prompt_kind": exc.details.get("prompt_kind"),
                "choices": exc.details.get("choices", []),
            },
            error_code="needs_input",
        )
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Input required: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except SkillToPluginError as exc:
        payload = make_payload(
            "error",
            {"error": exc.message, "details": exc.details},
            error_code=exc.code,
        )
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Error [{exc.code}]: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        payload = make_payload(
            "error",
            {"error": f"Unexpected failure ({type(exc).__name__}): {sanitize_text(str(exc))}"},
            error_code="internal_error",
        )
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["error"], file=sys.stderr)
        return int(ExitCode.INTERNAL_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
