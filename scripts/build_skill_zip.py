#!/usr/bin/env python3
"""Build the deterministic, single-root uploadable Skill ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unicodedata
import zipfile


REQUIRED_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "README.md",
    "README.ja.md",
    "LICENSE",
}
EXCLUDED_TOP_LEVEL = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
}


def _is_excluded(relative: Path) -> bool:
    if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    return any(
        part == "__pycache__" or part.endswith(".egg-info")
        for part in relative.parts
    ) or relative.suffix.lower() in {".pyc", ".pyo"}


def _release_files(source_root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    normalized: dict[str, str] = {}
    for path in sorted(source_root.rglob("*"), key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root)
        if _is_excluded(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"release source contains a symbolic link: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"release source contains a special file: {relative.as_posix()}")
        rendered = relative.as_posix()
        key = unicodedata.normalize("NFC", rendered).casefold()
        previous = normalized.get(key)
        if previous is not None and previous != rendered:
            raise ValueError(f"release paths collide after case/Unicode normalization: {previous}, {rendered}")
        normalized[key] = rendered
        files.append((path, rendered))
    present = {relative for _path, relative in files}
    missing = sorted(REQUIRED_FILES - present)
    if missing:
        raise ValueError(f"release source is missing required files: {', '.join(missing)}")
    return files


def build_skill_zip(source_root: Path, output: Path, *, force: bool = False) -> dict[str, object]:
    source_root = source_root.resolve()
    output = output.resolve()
    if not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    try:
        output.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("release ZIP must be written outside the source tree")
    if output.exists() and not force:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    files = _release_files(source_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path, relative in files:
                info = zipfile.ZipInfo(
                    f"{source_root.name}/{relative}",
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100644 & 0xFFFF) << 16
                info.flag_bits |= 0x800
                archive.writestr(info, path.read_bytes())
        with zipfile.ZipFile(temporary) as archive:
            names = archive.namelist()
            prefix = source_root.name + "/"
            if len(names) != len(set(names)) or not names or any(not name.startswith(prefix) for name in names):
                raise ValueError("release ZIP must contain exactly one top-level Skill directory")
            archived = {name[len(prefix):] for name in names}
            if not REQUIRED_FILES <= archived:
                raise ValueError("release ZIP is missing required Skill files")
            if any("/__pycache__/" in f"/{name}/" or name.endswith((".pyc", ".pyo")) for name in names):
                raise ValueError("release ZIP contains Python cache files")
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "schema_version": "1.0",
        "status": "ok",
        "source_root": str(source_root),
        "zip_path": str(output),
        "zip_sha256": digest,
        "top_level": source_root.name,
        "file_count": len(files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = build_skill_zip(args.source_root, args.output, force=args.force)
    except (OSError, ValueError) as exc:
        print(json.dumps({"schema_version": "1.0", "status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
