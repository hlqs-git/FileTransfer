#!/usr/bin/env python3
"""Cross-platform concurrent file transfer client."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import os
from pathlib import Path
import posixpath
import re
import tempfile
import time
from typing import Callable, Mapping, TypeVar
from urllib.parse import urljoin, urlsplit


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


class IntegrityError(TransferError):
    """Raised when transferred bytes do not match their expected digest."""


class HTTPStatusError(TransferError):
    def __init__(
        self,
        status: int,
        reason: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.reason = reason
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("retry delays cannot be negative")


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


def extract_download_url(text: str) -> str:
    candidates = re.findall(r"https?://[^\s<>\"']+", text)
    valid = []
    for candidate in candidates:
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            valid.append(candidate)
    if len(valid) != 1:
        raise TransferError(
            f"upload response must contain exactly one download URL; found {len(valid)}"
        )
    return valid[0]


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, HTTPStatusError):
        return error.status in {408, 429} or 500 <= error.status <= 599
    return isinstance(
        error,
        (OSError, TimeoutError, http.client.HTTPException, IntegrityError),
    )


T = TypeVar("T")


def run_with_retry(
    operation: Callable[[], T],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt >= policy.max_attempts or not _is_retryable(error):
                raise
            retry_after = None
            if isinstance(error, HTTPStatusError):
                value = error.headers.get("retry-after")
                if value and value.isdigit():
                    retry_after = float(value)
            delay = (
                retry_after
                if retry_after is not None
                else policy.base_delay * (2 ** (attempt - 1))
            )
            sleep(min(delay, policy.max_delay))
    raise AssertionError("retry loop exhausted")


class LimitedReader:
    def __init__(self, source, length: int) -> None:
        self.source = source
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = self.source.read(size)
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.source.close()


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TransferError(f"unsupported URL: {url!r}")
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port


def _request_target(url: str) -> str:
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return target


def _request_stream(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body_factory,
    content_length: int | None,
    timeout: float,
    consume,
):
    current_url = url
    current_headers = dict(headers)
    for redirect_count in range(6):
        scheme, hostname, port = _origin(current_url)
        connection_type = (
            http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_type(hostname, port, timeout=timeout)
        response = None
        body = None
        try:
            connection.putrequest(method, _request_target(current_url))
            for key, value in current_headers.items():
                connection.putheader(key, value)
            if content_length is not None:
                connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            if body_factory is not None:
                body = body_factory()
                while True:
                    block = body.read(BUFFER_SIZE)
                    if not block:
                        break
                    connection.send(block)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read()
                if not location:
                    raise TransferError("redirect response omitted Location")
                if method == "PUT" and response.status not in {307, 308}:
                    raise TransferError(
                        f"refusing ambiguous HTTP {response.status} redirect for PUT"
                    )
                next_url = urljoin(current_url, location)
                if _origin(next_url) != _origin(current_url):
                    current_headers.pop("Authorization", None)
                current_url = next_url
                continue
            if not 200 <= response.status <= 299:
                reason = response.read(4096).decode("utf-8", errors="replace").strip()
                raise HTTPStatusError(
                    response.status,
                    reason or response.reason,
                    dict(response.getheaders()),
                )
            return consume(response)
        finally:
            if body is not None:
                body.close()
            if response is not None:
                response.close()
            connection.close()
    raise TransferError("too many HTTP redirects")


def upload_once(
    source_path: Path,
    chunk: ChunkSpec,
    url: str,
    auth: str | None,
    expires: int,
    timeout: float,
) -> str:
    headers = {"Content-Type": "application/octet-stream"}
    if auth:
        headers["Authorization"] = auth
    if expires > 0:
        headers["X-Expiration-Seconds"] = str(expires)

    def body_factory():
        source = Path(source_path).open("rb")
        source.seek(chunk.offset)
        return LimitedReader(source, chunk.length)

    def consume(response) -> str:
        return extract_download_url(response.read().decode("utf-8", errors="replace"))

    return _request_stream(
        "PUT",
        url,
        headers,
        body_factory,
        chunk.length,
        timeout,
        consume,
    )


def download_once(
    url: str,
    destination: Path,
    auth: str | None,
    timeout: float,
) -> None:
    headers = {"Authorization": auth} if auth else {}

    def consume(response) -> None:
        with Path(destination).open("wb") as output:
            while True:
                block = response.read(BUFFER_SIZE)
                if not block:
                    break
                output.write(block)

    _request_stream("GET", url, headers, None, None, timeout, consume)
