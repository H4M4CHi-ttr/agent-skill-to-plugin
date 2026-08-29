"""Safe, bounded extraction for ZIP and tar-family source archives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tarfile
import tempfile
from typing import BinaryIO, Iterable
import unicodedata
import zipfile

from ..errors import SkillToPluginError
from ..limits import (
    MAX_COMPRESSED_BYTES,
    MAX_DEPTH,
    MAX_FILES,
    MAX_MEMBER_BYTES,
    MAX_PATH_CHARS,
    MAX_TOTAL_BYTES,
)
from ..utils import hash_tree, normalized_path_key, safe_posix_relative, sha256_file


_COPY_CHUNK = 1024 * 1024
_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_ZIP_EOCD = struct.Struct("<4s4H2LH")
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP_MAX_COMMENT = 0xFFFF
_ZIP16_SENTINEL = 0xFFFF
_ZIP32_SENTINEL = 0xFFFFFFFF


@dataclass(frozen=True)
class ArchiveExtractionResult:
    """Verified extraction metadata for a source archive."""

    archive_format: str
    archive_sha256: str
    destination: Path
    tree_sha256: str
    file_count: int
    total_bytes: int
    skipped_symbolic_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Entry:
    source: object
    path: PurePosixPath
    is_directory: bool
    size: int


class _PathRegistry:
    """Detect portable path collisions before any entry is extracted."""

    def __init__(self) -> None:
        self._paths: dict[str, tuple[str, bool]] = {}
        self._compatibility_paths: dict[str, str] = {}
        self._explicit: set[str] = set()

    @staticmethod
    def _validate_portable_path(path: PurePosixPath) -> None:
        rendered = path.as_posix()
        if len(rendered) > MAX_PATH_CHARS:
            raise SkillToPluginError(
                "An archive entry exceeds the portable path-length limit.",
                code="security_rejected",
                details={"path": rendered, "max_chars": MAX_PATH_CHARS},
            )
        if len(path.parts) > MAX_DEPTH:
            raise SkillToPluginError(
                "An archive entry exceeds the directory-depth limit.",
                code="security_rejected",
                details={"path": rendered, "max_depth": MAX_DEPTH},
            )
        for segment in path.parts:
            if any(character in _WINDOWS_INVALID_CHARS for character in segment):
                raise SkillToPluginError(
                    "An archive entry contains a Windows-invalid filename character.",
                    code="security_rejected",
                    details={"path": rendered},
                )

    def _register_one(self, rendered: str, *, is_directory: bool, explicit: bool) -> None:
        key = normalized_path_key(rendered)
        previous = self._paths.get(key)
        if previous is not None:
            previous_rendered, previous_is_directory = previous
            if previous_rendered != rendered:
                raise SkillToPluginError(
                    "Archive entries collide after case or Unicode normalization.",
                    code="security_rejected",
                    details={"first_path": previous_rendered, "second_path": rendered},
                )
            if previous_is_directory != is_directory:
                raise SkillToPluginError(
                    "An archive path is used as both a file and a directory.",
                    code="security_rejected",
                    details={"path": rendered},
                )
            if explicit and key in self._explicit:
                raise SkillToPluginError(
                    "The archive contains a duplicate entry.",
                    code="security_rejected",
                    details={"path": rendered},
                )
        else:
            self._paths[key] = (rendered, is_directory)

        # NFKC catches compatibility-equivalent names in addition to the NFC
        # and case-fold key used by the shared portable-path policy.
        compatibility_key = unicodedata.normalize("NFKC", rendered).casefold()
        compatibility_previous = self._compatibility_paths.get(compatibility_key)
        if compatibility_previous is not None and compatibility_previous != rendered:
            raise SkillToPluginError(
                "Archive entries collide after compatibility Unicode normalization.",
                code="security_rejected",
                details={"first_path": compatibility_previous, "second_path": rendered},
            )
        self._compatibility_paths[compatibility_key] = rendered
        if explicit:
            self._explicit.add(key)

    def add(self, path: PurePosixPath, *, is_directory: bool) -> None:
        self._validate_portable_path(path)
        for depth in range(1, len(path.parts)):
            ancestor = PurePosixPath(*path.parts[:depth]).as_posix()
            self._register_one(ancestor, is_directory=True, explicit=False)
        self._register_one(path.as_posix(), is_directory=is_directory, explicit=True)


def _bounded_copy(source: BinaryIO, destination: BinaryIO, *, expected_size: int) -> int:
    copied = 0
    while True:
        chunk = source.read(min(_COPY_CHUNK, expected_size - copied + 1))
        if not chunk:
            break
        copied += len(chunk)
        if copied > expected_size or copied > MAX_MEMBER_BYTES:
            raise SkillToPluginError(
                "An archive member expanded beyond its declared or allowed size.",
                code="security_rejected",
            )
        destination.write(chunk)
    if copied != expected_size:
        raise SkillToPluginError(
            "An archive member size does not match its header.",
            code="security_rejected",
            details={"declared_bytes": expected_size, "actual_bytes": copied},
        )
    return copied


def _validate_totals(entries: Iterable[_Entry]) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    entry_count = 0
    registry = _PathRegistry()
    for entry in entries:
        entry_count += 1
        if entry_count > MAX_FILES:
            raise SkillToPluginError(
                "The archive contains too many entries.",
                code="security_rejected",
                details={"max_entries": MAX_FILES},
            )
        registry.add(entry.path, is_directory=entry.is_directory)
        if entry.is_directory:
            continue
        file_count += 1
        if entry.size < 0 or entry.size > MAX_MEMBER_BYTES:
            raise SkillToPluginError(
                "An archive member exceeds the size limit.",
                code="security_rejected",
                details={"path": entry.path.as_posix(), "size": entry.size, "max_bytes": MAX_MEMBER_BYTES},
            )
        total_bytes += entry.size
        if total_bytes > MAX_TOTAL_BYTES:
            raise SkillToPluginError(
                "The archive exceeds the total expanded-size limit.",
                code="security_rejected",
                details={"max_bytes": MAX_TOTAL_BYTES},
            )
    return file_count, total_bytes


def _zip_security_rejection(message: str, **details: object) -> SkillToPluginError:
    return SkillToPluginError(message, code="security_rejected", details=dict(details))


def _read_at(handle: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        return b""
    handle.seek(offset)
    return handle.read(size)


def _find_zip_eocd(handle: BinaryIO, file_size: int) -> tuple[int, tuple[object, ...]]:
    """Find one unambiguous EOCD whose declared comment reaches EOF."""

    tail_size = min(file_size, _ZIP_EOCD.size + _ZIP_MAX_COMMENT)
    tail_offset = file_size - tail_size
    tail = _read_at(handle, tail_offset, tail_size)
    candidates: list[tuple[int, tuple[object, ...]]] = []
    search_from = 0
    while True:
        relative = tail.find(_ZIP_EOCD_SIGNATURE, search_from)
        if relative < 0:
            break
        search_from = relative + 1
        record = tail[relative:relative + _ZIP_EOCD.size]
        if len(record) != _ZIP_EOCD.size:
            continue
        fields = _ZIP_EOCD.unpack(record)
        comment_size = int(fields[-1])
        absolute = tail_offset + relative
        if absolute + _ZIP_EOCD.size + comment_size == file_size:
            candidates.append((absolute, fields))
    if len(candidates) != 1:
        raise _zip_security_rejection(
            "The ZIP end-of-central-directory record is missing or ambiguous.",
            candidates=len(candidates),
        )
    return candidates[0]


def _parse_zip64_eocd(
    handle: BinaryIO,
    *,
    eocd_offset: int,
    locator: tuple[object, ...],
) -> tuple[tuple[object, ...], int, int]:
    """Return ZIP64 fields, its absolute offset, and any prepended-byte delta."""

    _signature, locator_disk, declared_offset, disk_count = locator
    locator_disk = int(locator_disk)
    declared_offset = int(declared_offset)
    disk_count = int(disk_count)
    if locator_disk != 0 or disk_count != 1:
        raise _zip_security_rejection(
            "Multi-disk ZIP64 archives are not supported.",
            locator_disk=locator_disk,
            disk_count=disk_count,
        )

    locator_offset = eocd_offset - _ZIP64_LOCATOR.size
    # The locator offset is relative to the ZIP payload. A self-extracting ZIP
    # may prepend bytes, so also try the fixed record position immediately
    # before the locator. Both candidates remain bounded fixed-size reads.
    possible_offsets = list(dict.fromkeys((declared_offset, locator_offset - _ZIP64_EOCD.size)))
    candidates: list[tuple[tuple[object, ...], int, int]] = []
    for record_offset in possible_offsets:
        if record_offset < declared_offset or record_offset < 0:
            continue
        record = _read_at(handle, record_offset, _ZIP64_EOCD.size)
        if len(record) != _ZIP64_EOCD.size or not record.startswith(_ZIP64_EOCD_SIGNATURE):
            continue
        fields = _ZIP64_EOCD.unpack(record)
        declared_record_size = int(fields[1])
        if declared_record_size < 44:
            continue
        if record_offset + 12 + declared_record_size != locator_offset:
            continue
        candidates.append((fields, record_offset, record_offset - declared_offset))
    if len(candidates) != 1:
        raise _zip_security_rejection(
            "The ZIP64 end-of-central-directory record is missing, malformed, or ambiguous.",
            candidates=len(candidates),
        )
    return candidates[0]


def _preflight_zip_entry_count(archive_path: Path) -> int:
    """Reject oversized or multi-disk ZIP declarations before ZipFile opens.

    ``zipfile.ZipFile`` materializes the central directory during
    initialization. Reading the fixed-size EOCD records first prevents a ZIP
    that honestly declares an excessive entry count from reaching that work.
    The ordinary post-open entry validation remains the authoritative check
    for archives whose central directory contradicts the declaration.
    """

    try:
        file_size = archive_path.stat().st_size
        with archive_path.open("rb") as handle:
            eocd_offset, eocd = _find_zip_eocd(handle, file_size)
            (
                _signature,
                disk_number,
                central_disk,
                entries_this_disk,
                entries_total,
                central_size,
                central_offset,
                _comment_size,
            ) = eocd
            disk_number = int(disk_number)
            central_disk = int(central_disk)
            entries_this_disk = int(entries_this_disk)
            entries_total = int(entries_total)
            central_size = int(central_size)
            central_offset = int(central_offset)

            locator_offset = eocd_offset - _ZIP64_LOCATOR.size
            locator_data = _read_at(handle, locator_offset, _ZIP64_LOCATOR.size)
            has_locator = (
                len(locator_data) == _ZIP64_LOCATOR.size
                and locator_data.startswith(_ZIP64_LOCATOR_SIGNATURE)
            )
            requires_zip64 = (
                disk_number == _ZIP16_SENTINEL
                or central_disk == _ZIP16_SENTINEL
                or entries_this_disk == _ZIP16_SENTINEL
                or entries_total == _ZIP16_SENTINEL
                or central_size == _ZIP32_SENTINEL
                or central_offset == _ZIP32_SENTINEL
            )

            prepended_bytes = 0
            record_offset = eocd_offset
            if has_locator:
                locator = _ZIP64_LOCATOR.unpack(locator_data)
                zip64, record_offset, prepended_bytes = _parse_zip64_eocd(
                    handle,
                    eocd_offset=eocd_offset,
                    locator=locator,
                )
                (
                    _zip64_signature,
                    _zip64_size,
                    _version_made,
                    _version_needed,
                    zip64_disk,
                    zip64_central_disk,
                    zip64_entries_this_disk,
                    zip64_entries_total,
                    zip64_central_size,
                    zip64_central_offset,
                ) = zip64
                zip64_disk = int(zip64_disk)
                zip64_central_disk = int(zip64_central_disk)
                zip64_entries_this_disk = int(zip64_entries_this_disk)
                zip64_entries_total = int(zip64_entries_total)
                zip64_central_size = int(zip64_central_size)
                zip64_central_offset = int(zip64_central_offset)
                if (
                    zip64_disk != 0
                    or zip64_central_disk != 0
                    or zip64_entries_this_disk != zip64_entries_total
                ):
                    raise _zip_security_rejection(
                        "Multi-disk ZIP64 archives are not supported.",
                        disk_number=zip64_disk,
                        central_disk=zip64_central_disk,
                        entries_this_disk=zip64_entries_this_disk,
                        entries_total=zip64_entries_total,
                    )
                consistency = (
                    (disk_number, _ZIP16_SENTINEL, zip64_disk, "disk_number"),
                    (central_disk, _ZIP16_SENTINEL, zip64_central_disk, "central_disk"),
                    (entries_this_disk, _ZIP16_SENTINEL, zip64_entries_this_disk, "entries_this_disk"),
                    (entries_total, _ZIP16_SENTINEL, zip64_entries_total, "entries_total"),
                    (central_size, _ZIP32_SENTINEL, zip64_central_size, "central_size"),
                    (central_offset, _ZIP32_SENTINEL, zip64_central_offset, "central_offset"),
                )
                mismatched = [
                    label
                    for ordinary, sentinel, extended, label in consistency
                    if ordinary != sentinel and ordinary != extended
                ]
                if mismatched:
                    raise _zip_security_rejection(
                        "ZIP and ZIP64 end records contradict each other.",
                        fields=mismatched,
                    )
                entries_total = zip64_entries_total
                central_size = zip64_central_size
                central_offset = zip64_central_offset
            elif requires_zip64:
                raise _zip_security_rejection(
                    "The ZIP uses ZIP64 sentinel values without a valid ZIP64 locator."
                )
            else:
                if disk_number != 0 or central_disk != 0 or entries_this_disk != entries_total:
                    raise _zip_security_rejection(
                        "Multi-disk ZIP archives are not supported.",
                        disk_number=disk_number,
                        central_disk=central_disk,
                        entries_this_disk=entries_this_disk,
                        entries_total=entries_total,
                    )

            if entries_total > MAX_FILES:
                raise _zip_security_rejection(
                    "The ZIP declares too many entries.",
                    declared_entries=entries_total,
                    max_entries=MAX_FILES,
                )

            if has_locator:
                actual_central_offset = central_offset + prepended_bytes
                if (
                    actual_central_offset < 0
                    or actual_central_offset > record_offset
                    or central_size > record_offset - actual_central_offset
                ):
                    raise _zip_security_rejection(
                        "The ZIP64 central-directory bounds are invalid."
                    )
            elif central_size > eocd_offset or central_offset > eocd_offset - central_size:
                raise _zip_security_rejection(
                    "The ZIP central-directory bounds are invalid."
                )
            return entries_total
    except SkillToPluginError:
        raise
    except (OSError, EOFError, struct.error) as exc:
        raise SkillToPluginError(
            "The ZIP end-of-central-directory records could not be read safely.",
            code="invalid_manifest",
        ) from exc


def _zip_entries(archive: zipfile.ZipFile) -> list[_Entry]:
    entries: list[_Entry] = []
    for info in archive.infolist():
        raw_name = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
        if info.is_dir() and raw_name in {"", "."}:
            continue
        path = safe_posix_relative(raw_name)
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode) if unix_mode else 0
        if file_type == stat.S_IFLNK:
            raise SkillToPluginError(
                "Symbolic links are not allowed in ZIP sources.",
                code="security_rejected",
                details={"path": path.as_posix()},
            )
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise SkillToPluginError(
                "Special files are not allowed in ZIP sources.",
                code="security_rejected",
                details={"path": path.as_posix()},
            )
        if info.flag_bits & 0x1:
            raise SkillToPluginError(
                "Encrypted ZIP members are not supported.",
                code="unsupported_source",
                details={"path": path.as_posix()},
            )
        is_directory = info.is_dir() or file_type == stat.S_IFDIR
        if is_directory and info.file_size:
            raise SkillToPluginError(
                "A ZIP directory entry declares file content.",
                code="security_rejected",
                details={"path": path.as_posix()},
            )
        entries.append(_Entry(info, path, is_directory, 0 if is_directory else info.file_size))
    return entries


def _tar_entries(
    archive: tarfile.TarFile,
    *,
    skip_symbolic_links: bool = False,
) -> tuple[list[_Entry], tuple[str, ...]]:
    entries: list[_Entry] = []
    skipped_symbolic_links: list[str] = []
    # Iterate headers instead of calling getmembers(), which materializes the
    # complete archive before the configured entry limit can be enforced.  A
    # highly-compressible tar can otherwise exhaust memory with zero-byte
    # members even though extraction would eventually reject it.
    for entry_count, info in enumerate(archive, start=1):
        if entry_count > MAX_FILES:
            raise SkillToPluginError(
                "The archive contains too many entries.",
                code="security_rejected",
                details={"max_entries": MAX_FILES},
            )
        raw_name = info.name[:-1] if info.isdir() and info.name.endswith("/") else info.name
        if info.isdir() and raw_name in {"", "."}:
            continue
        path = safe_posix_relative(raw_name)
        if info.issym():
            if skip_symbolic_links:
                skipped_symbolic_links.append(path.as_posix())
                continue
            raise SkillToPluginError(
                "Symbolic and hard links are not allowed in tar sources.",
                code="security_rejected",
                details={"path": path.as_posix()},
            )
        if info.islnk():
            raise SkillToPluginError(
                "Symbolic and hard links are not allowed in tar sources.",
                code="security_rejected",
                details={"path": path.as_posix()},
            )
        if info.ischr() or info.isblk() or info.isfifo() or info.isdev():
            raise SkillToPluginError(
                "Device nodes and FIFOs are not allowed in tar sources.",
                code="security_rejected",
                details={"path": path.as_posix()},
            )
        if not (info.isdir() or info.isreg()):
            raise SkillToPluginError(
                "Unsupported special entry in tar source.",
                code="security_rejected",
                details={"path": path.as_posix(), "type": repr(info.type)},
            )
        entries.append(_Entry(info, path, info.isdir(), 0 if info.isdir() else info.size))
    return entries, tuple(skipped_symbolic_links)


def _extract_zip(archive_path: Path, destination: Path) -> tuple[int, int, tuple[str, ...]]:
    try:
        _preflight_zip_entry_count(archive_path)
        with zipfile.ZipFile(archive_path, "r") as archive:
            entries = _zip_entries(archive)
            expected_count, expected_total = _validate_totals(entries)
            actual_count = 0
            actual_total = 0
            for entry in entries:
                target = destination.joinpath(*entry.path.parts)
                if entry.is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry.source, "r") as source, target.open("xb") as output:
                    actual_total += _bounded_copy(source, output, expected_size=entry.size)
                actual_count += 1
            if (actual_count, actual_total) != (expected_count, expected_total):
                raise SkillToPluginError("ZIP extraction totals changed after validation.", code="security_rejected")
            return actual_count, actual_total, ()
    except SkillToPluginError:
        raise
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise SkillToPluginError("The ZIP archive is corrupt or unsupported.", code="invalid_manifest") from exc


def _extract_tar(
    archive_path: Path,
    destination: Path,
    *,
    skip_symbolic_links: bool = False,
) -> tuple[int, int, tuple[str, ...]]:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            entries, skipped_symbolic_links = _tar_entries(
                archive,
                skip_symbolic_links=skip_symbolic_links,
            )
            expected_count, expected_total = _validate_totals(entries)
            actual_count = 0
            actual_total = 0
            for entry in entries:
                target = destination.joinpath(*entry.path.parts)
                if entry.is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(entry.source)
                if source is None:
                    raise SkillToPluginError(
                        "A regular tar member could not be read.",
                        code="invalid_manifest",
                        details={"path": entry.path.as_posix()},
                    )
                with source, target.open("xb") as output:
                    actual_total += _bounded_copy(source, output, expected_size=entry.size)
                actual_count += 1
            if (actual_count, actual_total) != (expected_count, expected_total):
                raise SkillToPluginError("Tar extraction totals changed after validation.", code="security_rejected")
            return actual_count, actual_total, skipped_symbolic_links
    except SkillToPluginError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise SkillToPluginError("The tar archive is corrupt or unsupported.", code="invalid_manifest") from exc


def _detect_format(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise SkillToPluginError("Could not read the source archive.", code="unknown_source") from exc
    # Do not ask zipfile.is_zipfile() to interpret ZIP64 EOCD records before
    # our fixed-size preflight has rejected excessive or multi-disk metadata.
    # Any PK-prefixed candidate is routed through that stricter parser first.
    if magic.startswith(b"PK"):
        return "zip"
    if magic.startswith(b"\x1f\x8b"):
        try:
            with tarfile.open(path, mode="r:gz"):
                return "tar.gz"
        except tarfile.TarError as exc:
            raise SkillToPluginError(
                "The gzip payload is not a supported tar archive.",
                code="unsupported_source",
            ) from exc
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError as exc:
        raise SkillToPluginError("Could not inspect the source archive.", code="unknown_source") from exc
    raise SkillToPluginError(
        "The source is not a supported ZIP, tar, tar.gz, or tgz archive.",
        code="unsupported_source",
    )


def extract_archive(
    archive_path: Path,
    destination: Path,
    *,
    skip_symbolic_links: bool = False,
) -> ArchiveExtractionResult:
    """Validate all entries, then extract into a newly-created destination.

    ``skip_symbolic_links`` is reserved for Git-created tar snapshots. Generic
    user archives retain strict link rejection. Git callers record every
    skipped path and later refuse conversion if a selected Skill contains one.
    """

    archive_path = Path(archive_path)
    destination = Path(destination)
    if not archive_path.is_file():
        raise SkillToPluginError("The source archive does not exist.", code="unknown_source")
    archive_size = archive_path.stat().st_size
    if archive_size > MAX_COMPRESSED_BYTES:
        raise SkillToPluginError(
            "The compressed archive exceeds the configured size limit.",
            code="security_rejected",
            details={"size": archive_size, "max_bytes": MAX_COMPRESSED_BYTES},
        )
    if destination.exists() or destination.is_symlink():
        raise SkillToPluginError(
            "Archive extraction requires a new destination path.",
            code="output_conflict",
            details={"destination": str(destination)},
        )

    archive_format = _detect_format(archive_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".extract", dir=destination.parent))
    try:
        if archive_format == "zip":
            file_count, total_bytes, skipped_symbolic_links = _extract_zip(archive_path, temporary)
        else:
            file_count, total_bytes, skipped_symbolic_links = _extract_tar(
                archive_path,
                temporary,
                skip_symbolic_links=skip_symbolic_links,
            )
        tree_sha256 = hash_tree(temporary)
        temporary.replace(destination)
        return ArchiveExtractionResult(
            archive_format=archive_format,
            archive_sha256=sha256_file(archive_path),
            destination=destination,
            tree_sha256=tree_sha256,
            file_count=file_count,
            total_bytes=total_bytes,
            skipped_symbolic_links=skipped_symbolic_links,
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


__all__ = ["ArchiveExtractionResult", "extract_archive"]
