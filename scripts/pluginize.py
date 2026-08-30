#!/usr/bin/env python3
"""Backward-compatible entry point for the former ``npx-skill-to-plugin`` CLI.

New integrations should use ``skill_to_plugin.py``.  This wrapper preserves the
published argument spelling while delegating parsing, acquisition, validation,
selection, packaging, and reporting to the common Agent Skill to Plugin pipeline.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Sequence

from agent_skill_to_plugin.application import run_request
from agent_skill_to_plugin.cli import _human, make_payload, result_payload
from agent_skill_to_plugin.errors import ExitCode, NeedsInputError, SkillToPluginError
from agent_skill_to_plugin.input_parser import parse_npx_command
from agent_skill_to_plugin.utils import sanitize_text


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper: convert an npx skills add request into a skills-only plugin.",
    )
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--command-file", type=Path)
    command.add_argument("--command")
    parser.add_argument("--output-root", type=Path, default=Path("converted-skills-marketplace"))
    parser.add_argument("--source-base", type=Path, default=Path.cwd())
    parser.add_argument("--plugin-name")
    parser.add_argument("--display-name")
    parser.add_argument("--author-name", default="Local conversion")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-personal", action="store_true")
    parser.add_argument(
        "--register-personal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Register in the standard personal Marketplace (default: enabled)",
    )
    parser.add_argument("--show-zip", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--staged-skills-dir", type=Path, help=argparse.SUPPRESS)
    return parser


def _read_command(args: argparse.Namespace) -> str:
    if args.command is not None:
        return args.command
    path: Path = args.command_file
    if path.is_symlink() or not path.is_file():
        raise SkillToPluginError("The command file is missing or is a symbolic link.", code="unknown_input_format")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillToPluginError(
            f"Could not read the UTF-8 command file: {sanitize_text(str(exc))}",
            code="unknown_input_format",
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    json_requested = "--json" in arguments
    try:
        args = build_argument_parser().parse_args(arguments)
        if args.timeout <= 0:
            raise SkillToPluginError("Timeout must be greater than zero.", code="unknown_input_format")
        raw_command = _read_command(args)
        parsed = parse_npx_command(raw_command, raw_input=raw_command)
        source_base = args.source_base.expanduser().resolve()
        if args.staged_skills_dir:
            staged = args.staged_skills_dir.expanduser().resolve()
            if not staged.is_dir():
                raise SkillToPluginError("The staged skills directory does not exist.", code="unknown_source")
            parsed = replace(
                parsed,
                kind="local",
                source=str(staged),
                normalized_input=str(staged),
                requested_skills=(),
                select_all=True,
                plugin_scope=False,
                metadata={**parsed.metadata, "snapshot_exact_root": True, "legacy_staged_input": True},
            )
        result = run_request(
            parsed,
            output_root=args.output_root,
            source_base=source_base,
            timeout_seconds=args.timeout,
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
            _human(result, show_zip=args.show_zip)
        # The legacy CLI used 2 for any expected failure or unresolved choice.
        return 0 if code == 0 else 2
    except NeedsInputError as exc:
        payload = make_payload(
            "needs_input",
            {"error": exc.message, "prompt_kind": exc.details.get("prompt_kind"), "choices": exc.details.get("choices", [])},
            error_code="needs_input",
        )
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Input required: {exc.message}", file=sys.stderr)
        return 2
    except SkillToPluginError as exc:
        payload = make_payload("error", {"error": exc.message, "details": exc.details}, error_code=exc.code)
        if json_requested:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
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
