"""Isolated OOXML ZIP integrity validator.

This module is executed as a standalone, isolated Python process by the
LibreOffice adapter.  It intentionally depends only on the standard library
and emits no document paths or archive member names.
"""

from __future__ import annotations

import os
import re
import stat
import struct
import sys
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


EXIT_VALID = 0
EXIT_INVALID_ZIP = 20
EXIT_CORRUPT_ZIP = 21
EXIT_REQUIRED_MEMBER_MISSING = 22
EXIT_UNCOMPRESSED_LIMIT_EXCEEDED = 23
EXIT_ENCRYPTED_MEMBER = 24
EXIT_INVALID_ARGUMENT = 25
EXIT_UNEXPECTED_FAILURE = 26
EXIT_UNSAFE_MEMBER = 27
EXIT_UNSUPPORTED_COMPRESSION = 28
EXIT_DUPLICATE_MEMBER = 29
EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED = 30
EXIT_INVALID_ZIP_METADATA = 31

ZIP_MAX_MEMBER_COUNT = 100_000
ZIP_MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024

_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_EOCD_STRUCT = struct.Struct("<4s4H2IH")
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2I4Q")
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sIQI")
_CENTRAL_DIRECTORY_HEADER_STRUCT = struct.Struct("<4s6H3I5H2I")
_EOCD_MIN_BYTES = _EOCD_STRUCT.size
_EOCD_MAX_COMMENT_BYTES = (1 << 16) - 1
_ZIP64_EOCD_MIN_PAYLOAD_BYTES = _ZIP64_EOCD_STRUCT.size - 12
_CENTRAL_DIRECTORY_HEADER_MIN_BYTES = 46

_ALLOWED_COMPRESSION_METHODS = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
    }
)


class _InvalidZipMetadataError(Exception):
    """ZIP tail metadata is missing, ambiguous, unsupported, or malformed."""


class _ZipDirectoryLimitError(Exception):
    """ZIP tail metadata exceeds an immutable pre-ZipFile resource limit."""


@dataclass(frozen=True, slots=True)
class _ZipDirectoryMetadata:
    member_count: int
    central_directory_size: int
    central_directory_start: int
    uses_zip64: bool


def _read_exact_at(
    file_object: BinaryIO,
    *,
    offset: int,
    size: int,
    file_size: int,
) -> bytes:
    if (
        isinstance(offset, bool)
        or isinstance(size, bool)
        or offset < 0
        or size < 0
        or offset > file_size
        or size > file_size - offset
    ):
        raise _InvalidZipMetadataError
    file_object.seek(offset)
    payload = file_object.read(size)
    if len(payload) != size:
        raise _InvalidZipMetadataError
    return payload


def _find_eocd(
    file_object: BinaryIO,
    *,
    file_size: int,
) -> tuple[int, tuple[bytes, int, int, int, int, int, int, int]]:
    """Locate one unambiguous EOCD whose declared comment ends at EOF."""

    if file_size < _EOCD_MIN_BYTES:
        raise _InvalidZipMetadataError
    tail_size = min(
        file_size,
        _EOCD_MIN_BYTES + _EOCD_MAX_COMMENT_BYTES,
    )
    tail_offset = file_size - tail_size
    tail = _read_exact_at(
        file_object,
        offset=tail_offset,
        size=tail_size,
        file_size=file_size,
    )
    candidates: list[
        tuple[int, tuple[bytes, int, int, int, int, int, int, int]]
    ] = []
    search_offset = 0
    while True:
        relative_offset = tail.find(_EOCD_SIGNATURE, search_offset)
        if relative_offset < 0:
            break
        if relative_offset + _EOCD_MIN_BYTES <= tail_size:
            values = _EOCD_STRUCT.unpack_from(tail, relative_offset)
            comment_size = values[-1]
            if relative_offset + _EOCD_MIN_BYTES + comment_size == tail_size:
                candidates.append((tail_offset + relative_offset, values))
        search_offset = relative_offset + 1
    if len(candidates) != 1:
        raise _InvalidZipMetadataError
    return candidates[0]


def _enforce_zip_directory_limits(
    *,
    member_count: int,
    central_directory_size: int,
) -> None:
    if (
        member_count > ZIP_MAX_MEMBER_COUNT
        or central_directory_size > ZIP_MAX_CENTRAL_DIRECTORY_BYTES
    ):
        raise _ZipDirectoryLimitError
    if member_count < 0 or central_directory_size < 0:
        raise _InvalidZipMetadataError
    if member_count == 0:
        if central_directory_size != 0:
            raise _InvalidZipMetadataError
        return
    if (
        central_directory_size == 0
        or central_directory_size
        < member_count * _CENTRAL_DIRECTORY_HEADER_MIN_BYTES
    ):
        raise _InvalidZipMetadataError


def _validate_central_directory_geometry(
    *,
    central_directory_size: int,
    central_directory_start: int,
    expected_end: int,
) -> None:
    if (
        central_directory_start < 0
        or central_directory_size > expected_end
        or central_directory_start != expected_end - central_directory_size
    ):
        raise _InvalidZipMetadataError


def _scan_central_directory_member_count(
    file_object: BinaryIO,
    *,
    file_size: int,
    central_directory_start: int,
    central_directory_size: int,
) -> int:
    """Count bounded central-directory headers without materializing ZipInfo."""

    central_directory_end = (
        central_directory_start + central_directory_size
    )
    if (
        central_directory_start < 0
        or central_directory_size < 0
        or central_directory_end > file_size
    ):
        raise _InvalidZipMetadataError
    file_object.seek(central_directory_start)
    cursor = central_directory_start
    member_count = 0
    while cursor < central_directory_end:
        if (
            central_directory_end - cursor
            < _CENTRAL_DIRECTORY_HEADER_STRUCT.size
        ):
            raise _InvalidZipMetadataError
        header = file_object.read(_CENTRAL_DIRECTORY_HEADER_STRUCT.size)
        if len(header) != _CENTRAL_DIRECTORY_HEADER_STRUCT.size:
            raise _InvalidZipMetadataError
        values = _CENTRAL_DIRECTORY_HEADER_STRUCT.unpack(header)
        if (
            values[0] != _CENTRAL_DIRECTORY_SIGNATURE
            or values[13] != 0
        ):
            raise _InvalidZipMetadataError
        file_name_size = values[10]
        extra_size = values[11]
        comment_size = values[12]
        variable_size = file_name_size + extra_size + comment_size
        next_cursor = (
            cursor
            + _CENTRAL_DIRECTORY_HEADER_STRUCT.size
            + variable_size
        )
        if next_cursor > central_directory_end:
            raise _InvalidZipMetadataError
        member_count += 1
        if member_count > ZIP_MAX_MEMBER_COUNT:
            raise _ZipDirectoryLimitError
        file_object.seek(variable_size, os.SEEK_CUR)
        cursor = next_cursor
    if cursor != central_directory_end:
        raise _InvalidZipMetadataError
    return member_count


def _legacy_field_matches(
    legacy_value: int,
    zip64_value: int,
    *,
    sentinel: int,
) -> bool:
    return legacy_value == min(zip64_value, sentinel)


def _read_zip64_directory_metadata(
    file_object: BinaryIO,
    *,
    file_size: int,
    eocd_offset: int,
    eocd_values: tuple[bytes, int, int, int, int, int, int, int],
) -> _ZipDirectoryMetadata:
    locator_offset = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
    locator_payload = _read_exact_at(
        file_object,
        offset=locator_offset,
        size=_ZIP64_LOCATOR_STRUCT.size,
        file_size=file_size,
    )
    (
        locator_signature,
        zip64_eocd_disk,
        zip64_eocd_offset,
        disk_count,
    ) = _ZIP64_LOCATOR_STRUCT.unpack(locator_payload)
    if (
        locator_signature != _ZIP64_LOCATOR_SIGNATURE
        or zip64_eocd_disk != 0
        or disk_count != 1
        or zip64_eocd_offset >= locator_offset
    ):
        raise _InvalidZipMetadataError

    fixed_payload = _read_exact_at(
        file_object,
        offset=zip64_eocd_offset,
        size=_ZIP64_EOCD_STRUCT.size,
        file_size=file_size,
    )
    (
        zip64_signature,
        record_payload_size,
        _version_made_by,
        version_needed,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        member_count,
        central_directory_size,
        central_directory_offset,
    ) = _ZIP64_EOCD_STRUCT.unpack(fixed_payload)
    if (
        zip64_signature != _ZIP64_EOCD_SIGNATURE
        or record_payload_size < _ZIP64_EOCD_MIN_PAYLOAD_BYTES
        or zip64_eocd_offset + 12 + record_payload_size != locator_offset
        or version_needed < 45
        or disk_number != 0
        or central_directory_disk != 0
        or entries_on_disk != member_count
    ):
        raise _InvalidZipMetadataError

    (
        _signature,
        legacy_disk_number,
        legacy_central_directory_disk,
        legacy_entries_on_disk,
        legacy_member_count,
        legacy_central_directory_size,
        legacy_central_directory_offset,
        _comment_size,
    ) = eocd_values
    if not all(
        (
            legacy_disk_number == 0,
            legacy_central_directory_disk == 0,
            _legacy_field_matches(
                legacy_entries_on_disk,
                entries_on_disk,
                sentinel=0xFFFF,
            ),
            _legacy_field_matches(
                legacy_member_count,
                member_count,
                sentinel=0xFFFF,
            ),
            _legacy_field_matches(
                legacy_central_directory_size,
                central_directory_size,
                sentinel=0xFFFFFFFF,
            ),
            _legacy_field_matches(
                legacy_central_directory_offset,
                central_directory_offset,
                sentinel=0xFFFFFFFF,
            ),
        )
    ):
        raise _InvalidZipMetadataError

    _enforce_zip_directory_limits(
        member_count=member_count,
        central_directory_size=central_directory_size,
    )
    if central_directory_offset + central_directory_size != zip64_eocd_offset:
        raise _InvalidZipMetadataError
    _validate_central_directory_geometry(
        central_directory_size=central_directory_size,
        central_directory_start=central_directory_offset,
        expected_end=zip64_eocd_offset,
    )
    scanned_member_count = _scan_central_directory_member_count(
        file_object,
        file_size=file_size,
        central_directory_start=central_directory_offset,
        central_directory_size=central_directory_size,
    )
    if scanned_member_count != member_count:
        raise _InvalidZipMetadataError
    return _ZipDirectoryMetadata(
        member_count=member_count,
        central_directory_size=central_directory_size,
        central_directory_start=central_directory_offset,
        uses_zip64=True,
    )


def _read_zip_directory_metadata(
    file_object: BinaryIO,
    *,
    file_size: int,
) -> _ZipDirectoryMetadata:
    """Read bounded EOCD/ZIP64 metadata before ``zipfile.ZipFile`` is created."""

    eocd_offset, eocd_values = _find_eocd(
        file_object,
        file_size=file_size,
    )
    (
        _signature,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        member_count,
        central_directory_size,
        central_directory_offset,
        _comment_size,
    ) = eocd_values
    locator_offset = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
    has_zip64_locator = (
        locator_offset >= 0
        and _read_exact_at(
            file_object,
            offset=locator_offset,
            size=len(_ZIP64_LOCATOR_SIGNATURE),
            file_size=file_size,
        )
        == _ZIP64_LOCATOR_SIGNATURE
    )
    requires_zip64_metadata = (
        disk_number == 0xFFFF
        or central_directory_disk == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
    )
    if has_zip64_locator:
        return _read_zip64_directory_metadata(
            file_object,
            file_size=file_size,
            eocd_offset=eocd_offset,
            eocd_values=eocd_values,
        )
    if requires_zip64_metadata:
        raise _InvalidZipMetadataError
    if (
        disk_number != 0
        or central_directory_disk != 0
        or entries_on_disk != member_count
    ):
        raise _InvalidZipMetadataError

    _enforce_zip_directory_limits(
        member_count=member_count,
        central_directory_size=central_directory_size,
    )
    central_directory_start = eocd_offset - central_directory_size
    archive_prefix_size = central_directory_start - central_directory_offset
    if archive_prefix_size != 0:
        raise _InvalidZipMetadataError
    _validate_central_directory_geometry(
        central_directory_size=central_directory_size,
        central_directory_start=central_directory_start,
        expected_end=eocd_offset,
    )
    scanned_member_count = _scan_central_directory_member_count(
        file_object,
        file_size=file_size,
        central_directory_start=central_directory_start,
        central_directory_size=central_directory_size,
    )
    if scanned_member_count != member_count:
        raise _InvalidZipMetadataError
    return _ZipDirectoryMetadata(
        member_count=member_count,
        central_directory_size=central_directory_size,
        central_directory_start=central_directory_start,
        uses_zip64=False,
    )


def _file_snapshot(file_object: BinaryIO) -> tuple[int, int, int, int, int, int]:
    metadata = os.fstat(file_object.fileno())
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _is_safe_member(member: zipfile.ZipInfo) -> bool:
    """Reject entries that could be reinterpreted as unsafe filesystem paths."""

    original_name = member.orig_filename
    if (
        not original_name
        or "\x00" in original_name
        or "\\" in original_name
        or original_name.startswith(("/", "//"))
        or re.match(r"^[A-Za-z]:", original_name)
        or any(
            unicodedata.category(character).startswith("C")
            for character in original_name
        )
    ):
        return False
    path_value = original_name[:-1] if original_name.endswith("/") else original_name
    path_parts = path_value.split("/")
    if (
        not path_value
        or any(not part or part in {".", ".."} for part in path_parts)
    ):
        return False

    unix_mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        return False
    if member.is_dir():
        return file_type in {0, stat.S_IFDIR}
    return file_type in {0, stat.S_IFREG}


def validate_ooxml_archive(
    archive_path: Path,
    *,
    required_member: str,
    max_uncompressed_bytes: int,
) -> int:
    """Return a stable exit code after validating one OOXML archive."""

    if (
        not required_member
        or max_uncompressed_bytes <= 0
        or archive_path.is_symlink()
        or not archive_path.is_file()
    ):
        return EXIT_INVALID_ARGUMENT

    try:
        with archive_path.open("rb") as archive_file:
            initial_snapshot = _file_snapshot(archive_file)
            if not stat.S_ISREG(initial_snapshot[2]):
                return EXIT_INVALID_ARGUMENT
            try:
                directory_metadata = _read_zip_directory_metadata(
                    archive_file,
                    file_size=initial_snapshot[3],
                )
            except _ZipDirectoryLimitError:
                return EXIT_ZIP_DIRECTORY_LIMIT_EXCEEDED
            except _InvalidZipMetadataError:
                return EXIT_INVALID_ZIP_METADATA

            archive_file.seek(0)
            with zipfile.ZipFile(archive_file, "r") as archive:
                members = archive.infolist()
                if (
                    len(members) != directory_metadata.member_count
                    or archive.start_dir
                    != directory_metadata.central_directory_start
                ):
                    return EXIT_INVALID_ZIP_METADATA
                total_uncompressed_bytes = 0
                required_member_count = 0
                original_names: set[str] = set()
                for member in members:
                    if member.orig_filename in original_names:
                        return EXIT_DUPLICATE_MEMBER
                    original_names.add(member.orig_filename)
                    if not _is_safe_member(member):
                        return EXIT_UNSAFE_MEMBER
                    if member.compress_type not in _ALLOWED_COMPRESSION_METHODS:
                        return EXIT_UNSUPPORTED_COMPRESSION
                    if (
                        member.orig_filename == required_member
                        and not member.is_dir()
                    ):
                        required_member_count += 1
                    if member.flag_bits & 0x1:
                        return EXIT_ENCRYPTED_MEMBER
                    total_uncompressed_bytes += member.file_size
                    if total_uncompressed_bytes > max_uncompressed_bytes:
                        return EXIT_UNCOMPRESSED_LIMIT_EXCEEDED

                if required_member_count != 1:
                    return EXIT_REQUIRED_MEMBER_MISSING

                try:
                    bad_member = archive.testzip()
                except (EOFError, RuntimeError, zlib.error):
                    return EXIT_CORRUPT_ZIP
                if bad_member is not None:
                    return EXIT_CORRUPT_ZIP
            if _file_snapshot(archive_file) != initial_snapshot:
                return EXIT_INVALID_ZIP_METADATA
    except zipfile.BadZipFile:
        return EXIT_INVALID_ZIP
    except OSError:
        return EXIT_INVALID_ZIP
    except Exception:
        return EXIT_UNEXPECTED_FAILURE
    return EXIT_VALID


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        return EXIT_INVALID_ARGUMENT
    archive_value, required_member, max_uncompressed_value = arguments
    try:
        max_uncompressed_bytes = int(max_uncompressed_value)
    except (TypeError, ValueError):
        return EXIT_INVALID_ARGUMENT
    return validate_ooxml_archive(
        Path(archive_value),
        required_member=required_member,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
