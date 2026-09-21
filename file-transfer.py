#!/usr/bin/env python3
"""Cross-platform concurrent file transfer client."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import posixpath
import re
import tempfile
from typing import Mapping


BUFFER_SIZE = 1024 * 1024
_SIZE_PATTERN = re.compile(r"^([0-9]+)\s*(B|K|KB|KIB|M|MB|MIB|G|GB|GIB)?$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    None: 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
}
_MD5_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


class TransferError(Exception):
    """Base error for user-facing transfer failures."""


class ManifestError(TransferError):
    """Raised when a manifest is unsafe or malformed."""


@dataclass(frozen=True)
class ChunkSpec:
    index: int
    offset: int
    length: int
    md5: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Manifest:
    name: str
    file_md5: str
    chunks: tuple[ChunkSpec, ...]
    size: int | None = None
    chunk_size: int | None = None
    expires: int | None = None
    version: int = 1


def parse_size(text: str) -> int:
    match = _SIZE_PATTERN.fullmatch(text.strip())
    if not match:
        raise ValueError(f"invalid size: {text!r}")
    number = int(match.group(1))
    if number <= 0:
        raise ValueError("size must be greater than zero")
    suffix = match.group(2)
    multiplier = _SIZE_MULTIPLIERS[suffix.upper() if suffix else None]
    return number * multiplier


def safe_basename(value: str) -> str:
    normalized = value.replace("\\", "/")
    name = posixpath.basename(normalized)
    if not name or name in {".", ".."}:
        raise ManifestError(f"unsafe file name: {value!r}")
    return name


def plan_chunks(file_size: int, chunk_size: int) -> tuple[ChunkSpec, ...]:
    if file_size < 0:
        raise ValueError("file size cannot be negative")
    if chunk_size <= 0:
        raise ValueError("chunk size must be greater than zero")
    return tuple(
        ChunkSpec(index=index, offset=offset, length=min(chunk_size, file_size - offset))
        for index, offset in enumerate(range(0, file_size, chunk_size))
    )


def hash_file(path: Path) -> str:
    digest = hashlib.md5()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(BUFFER_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def positive_workers(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise ValueError("workers must be an integer from 1 to 16") from exc
    if not 1 <= workers <= 16:
        raise ValueError("workers must be from 1 to 16")
    return workers


def resolve_setting(
    cli_value: str | None,
    env_name: str,
    env: Mapping[str, str],
) -> str | None:
    return cli_value if cli_value is not None else env.get(env_name)


def _parse_nonnegative_int(key: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ManifestError(f"{key} must be an integer") from exc
    if parsed < 0:
        raise ManifestError(f"{key} cannot be negative")
    return parsed


def parse_manifest(text: str) -> Manifest:
    metadata: dict[str, str] = {}
    chunk_rows: list[tuple[str, str]] = []
    recognized = {"HASH", "NAME", "VERSION", "SIZE", "CHUNK_SIZE", "EXPIRES"}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            md5_value, separator, url = line.partition("|")
            if not separator or not _MD5_PATTERN.fullmatch(md5_value) or not url.startswith(("http://", "https://")):
                raise ManifestError(f"malformed chunk row: {raw_line!r}")
            chunk_rows.append((md5_value.lower(), url))
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ManifestError(f"malformed manifest row: {raw_line!r}")
        if key in recognized:
            if key in metadata:
                raise ManifestError(f"duplicate {key} field")
            metadata[key] = value

    if "HASH" not in metadata or "NAME" not in metadata:
        raise ManifestError("manifest requires exactly one HASH and NAME")
    if not _MD5_PATTERN.fullmatch(metadata["HASH"]):
        raise ManifestError("HASH must be a 32-character MD5 value")

    name = safe_basename(metadata["NAME"])
    version = _parse_nonnegative_int("VERSION", metadata.get("VERSION", "1"))
    size = _parse_nonnegative_int("SIZE", metadata["SIZE"]) if "SIZE" in metadata else None
    chunk_size = (
        _parse_nonnegative_int("CHUNK_SIZE", metadata["CHUNK_SIZE"])
        if "CHUNK_SIZE" in metadata
        else None
    )
    if chunk_size == 0:
        raise ManifestError("CHUNK_SIZE must be greater than zero")
    expires = _parse_nonnegative_int("EXPIRES", metadata["EXPIRES"]) if "EXPIRES" in metadata else None

    chunks = []
    for index, (md5_value, url) in enumerate(chunk_rows):
        offset = index * chunk_size if chunk_size is not None else 0
        if chunk_size is None:
            length = 0
        elif size is not None:
            length = min(chunk_size, max(0, size - offset))
        else:
            length = chunk_size
        chunks.append(ChunkSpec(index, offset, length, md5_value, url))

    return Manifest(
        name=name,
        file_md5=metadata["HASH"].lower(),
        chunks=tuple(chunks),
        size=size,
        chunk_size=chunk_size,
        expires=expires,
        version=version,
    )


def serialize_manifest(manifest: Manifest) -> str:
    name = safe_basename(manifest.name)
    if not _MD5_PATTERN.fullmatch(manifest.file_md5):
        raise ManifestError("file_md5 must be a 32-character MD5 value")
    lines = [f"HASH:{manifest.file_md5.lower()}", f"NAME:{name}"]
    if manifest.version >= 2:
        if manifest.size is None or manifest.chunk_size is None or manifest.expires is None:
            raise ManifestError("version 2 manifest requires size, chunk_size, and expires")
        lines.extend(
            (
                f"VERSION:{manifest.version}",
                f"SIZE:{manifest.size}",
                f"CHUNK_SIZE:{manifest.chunk_size}",
                f"EXPIRES:{manifest.expires}",
            )
        )
    for chunk in sorted(manifest.chunks, key=lambda item: item.index):
        if not chunk.md5 or not _MD5_PATTERN.fullmatch(chunk.md5) or not chunk.url:
            raise ManifestError(f"chunk {chunk.index} is incomplete")
        lines.append(f"{chunk.md5.lower()}|{chunk.url}")
    return "\n".join(lines) + "\n"


def atomic_write_text(path: Path, text: str, replace=os.replace) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
