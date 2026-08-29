"""Structured errors and stable process exit codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    NEEDS_SELECTION = 10
    NEEDS_INPUT = 11
    UNKNOWN_INPUT_FORMAT = 20
    UNKNOWN_SOURCE = 21
    UNKNOWN_MARKETPLACE = 22
    UNKNOWN_PLUGIN = 23
    NO_SKILL_CANDIDATES = 24
    AUTHENTICATION_FAILED = 25
    NETWORK_FAILED = 26
    INVALID_MANIFEST = 27
    UNSUPPORTED_SOURCE = 28
    SECURITY_REJECTED = 29
    PACKAGE_VALIDATION_FAILED = 30
    DEPENDENCY_MISSING = 31
    INVALID_SELECTION = 32
    RESOLUTION_INTEGRITY_FAILED = 33
    OUTPUT_CONFLICT = 34
    INTERNAL_ERROR = 70


ERROR_EXIT_CODES: dict[str, ExitCode] = {
    "unknown_input_format": ExitCode.UNKNOWN_INPUT_FORMAT,
    "unknown_source": ExitCode.UNKNOWN_SOURCE,
    "unknown_marketplace": ExitCode.UNKNOWN_MARKETPLACE,
    "unknown_plugin": ExitCode.UNKNOWN_PLUGIN,
    "no_skill_candidates": ExitCode.NO_SKILL_CANDIDATES,
    "authentication_failed": ExitCode.AUTHENTICATION_FAILED,
    "network_failed": ExitCode.NETWORK_FAILED,
    "invalid_manifest": ExitCode.INVALID_MANIFEST,
    "unsupported_source": ExitCode.UNSUPPORTED_SOURCE,
    "security_rejected": ExitCode.SECURITY_REJECTED,
    "package_validation_failed": ExitCode.PACKAGE_VALIDATION_FAILED,
    "dependency_missing": ExitCode.DEPENDENCY_MISSING,
    "invalid_selection": ExitCode.INVALID_SELECTION,
    "resolution_integrity_failed": ExitCode.RESOLUTION_INTEGRITY_FAILED,
    "output_conflict": ExitCode.OUTPUT_CONFLICT,
}


@dataclass
class SkillToPluginError(RuntimeError):
    message: str
    code: str = "internal_error"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    @property
    def exit_code(self) -> int:
        return int(ERROR_EXIT_CODES.get(self.code, ExitCode.INTERNAL_ERROR))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NeedsInputError(SkillToPluginError):
    """A safe user choice is required before source resolution can proceed."""

    def __init__(self, message: str, *, prompt_kind: str, choices: list[dict[str, Any]]) -> None:
        super().__init__(
            message=message,
            code="needs_input",
            details={"prompt_kind": prompt_kind, "choices": choices},
        )

    @property
    def exit_code(self) -> int:
        return int(ExitCode.NEEDS_INPUT)
