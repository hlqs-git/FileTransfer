#!/usr/bin/env python3
"""Cross-platform concurrent file transfer client."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import os
from pathlib import Path
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Callable, Mapping, Sequence, TypeAlias, TypeVar
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
_PROGRESS_LOCK = threading.Lock()


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


class CLIUsageError(Exception):
    """Raised for command-line errors that should return exit code 2."""


class TransferArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


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
ProgressCallback: TypeAlias = Callable[[int, int, str, str], None]


def run_with_retry(
    operation: Callable[[], T],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            try:
                setattr(error, "attempts", attempt)
            except (AttributeError, TypeError):
                pass
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
            output.flush()
            os.fsync(output.fileno())

    _request_stream("GET", url, headers, None, None, timeout, consume)


def compute_chunk_md5(path: Path, chunk: ChunkSpec) -> str:
    digest = hashlib.md5()
    remaining = chunk.length
    with Path(path).open("rb") as source:
        source.seek(chunk.offset)
        while remaining:
            block = source.read(min(BUFFER_SIZE, remaining))
            if not block:
                raise TransferError(
                    f"source file ended while reading chunk {chunk.index + 1}"
                )
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def upload_chunk(
    path: Path,
    chunk: ChunkSpec,
    url: str,
    auth: str | None,
    expires: int,
    retry_policy: RetryPolicy,
) -> ChunkSpec:
    chunk_md5 = compute_chunk_md5(path, chunk)
    download_url = run_with_retry(
        lambda: upload_once(path, chunk, url, auth, expires, 60),
        retry_policy,
    )
    return ChunkSpec(
        index=chunk.index,
        offset=chunk.offset,
        length=chunk.length,
        md5=chunk_md5,
        url=download_url,
    )


def push_file(
    path: Path,
    url: str,
    auth: str | None,
    manifest_path: Path,
    chunk_size: int,
    workers: int,
    expires: int,
    retry_policy: RetryPolicy,
    progress: ProgressCallback,
) -> Manifest:
    source_path = Path(path)
    if not source_path.is_file():
        raise TransferError(f"source file does not exist: {source_path}")
    file_size = source_path.stat().st_size
    file_md5 = hash_file(source_path)
    planned = plan_chunks(file_size, chunk_size)
    results: list[ChunkSpec | None] = [None] * len(planned)

    if planned:
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {
            executor.submit(
                upload_chunk,
                source_path,
                chunk,
                url,
                auth,
                expires,
                retry_policy,
            ): chunk
            for chunk in planned
        }
        try:
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    completed = future.result()
                except Exception as error:
                    for pending in futures:
                        pending.cancel()
                    attempts = getattr(error, "attempts", 1)
                    attempt_word = "attempt" if attempts == 1 else "attempts"
                    raise TransferError(
                        f"chunk {chunk.index + 1} upload failed after "
                        f"{attempts} {attempt_word}: {error}"
                    ) from error
                results[completed.index] = completed
                progress(completed.index + 1, len(planned), "upload", "complete")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    completed_chunks = tuple(result for result in results if result is not None)
    if len(completed_chunks) != len(planned):
        raise TransferError("upload ended before every chunk completed")
    manifest = Manifest(
        name=safe_basename(str(source_path)),
        file_md5=file_md5,
        chunks=completed_chunks,
        size=file_size,
        chunk_size=chunk_size,
        expires=expires,
        version=2,
    )
    atomic_write_text(Path(manifest_path), serialize_manifest(manifest))
    return manifest


def manifest_fingerprint(manifest_text: str) -> str:
    return hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()[:16]


def state_directory(output_path: Path, manifest_text: str) -> Path:
    output = Path(output_path)
    return (
        output.parent
        / ".file-transfer"
        / f"{output.name}-{manifest_fingerprint(manifest_text)}"
    )


def _part_path(state_dir: Path, index: int) -> Path:
    return Path(state_dir) / f"part_{index:06d}.bin"


def download_chunk(
    chunk: ChunkSpec,
    state_dir: Path,
    auth: str | None,
    retry_policy: RetryPolicy,
) -> Path:
    if not chunk.url or not chunk.md5:
        raise ManifestError(f"chunk {chunk.index + 1} is incomplete")
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    completed = _part_path(state, chunk.index)
    temporary = completed.with_suffix(completed.suffix + ".tmp")

    def operation() -> Path:
        temporary.unlink(missing_ok=True)
        try:
            download_once(chunk.url, temporary, auth, 60)
            actual = hash_file(temporary)
            if not hmac.compare_digest(actual, chunk.md5):
                raise IntegrityError(
                    f"chunk {chunk.index + 1} checksum mismatch: "
                    f"expected {chunk.md5}, got {actual}"
                )
            os.replace(temporary, completed)
            return completed
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    return run_with_retry(operation, retry_policy)


def pull_manifest(
    manifest_path: Path,
    output_path: Path | None,
    auth: str | None,
    workers: int,
    retry_policy: RetryPolicy,
    progress: ProgressCallback,
) -> Path:
    manifest_file = Path(manifest_path)
    try:
        with manifest_file.open("r", encoding="utf-8", newline="") as source:
            manifest_text = source.read()
    except OSError as error:
        raise TransferError(f"cannot read manifest {manifest_file}: {error}") from error
    manifest = parse_manifest(manifest_text)
    output = Path(output_path) if output_path is not None else Path.cwd() / manifest.name
    output.parent.mkdir(parents=True, exist_ok=True)
    state = state_directory(output, manifest_text)
    state.mkdir(parents=True, exist_ok=True)

    missing = []
    for chunk in manifest.chunks:
        part = _part_path(state, chunk.index)
        if part.is_file() and chunk.md5 and hmac.compare_digest(hash_file(part), chunk.md5):
            progress(chunk.index + 1, len(manifest.chunks), "download", "reused")
        else:
            part.unlink(missing_ok=True)
            missing.append(chunk)

    if missing:
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {
            executor.submit(download_chunk, chunk, state, auth, retry_policy): chunk
            for chunk in missing
        }
        try:
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    future.result()
                except Exception as error:
                    for pending in futures:
                        pending.cancel()
                    attempts = getattr(error, "attempts", 1)
                    attempt_word = "attempt" if attempts == 1 else "attempts"
                    raise TransferError(
                        f"chunk {chunk.index + 1} download failed after "
                        f"{attempts} {attempt_word}: {error}"
                    ) from error
                progress(chunk.index + 1, len(manifest.chunks), "download", "complete")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    temporary_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as assembled:
            temporary_output = Path(assembled.name)
            for chunk in manifest.chunks:
                part = _part_path(state, chunk.index)
                with part.open("rb") as source:
                    shutil.copyfileobj(source, assembled, BUFFER_SIZE)
            assembled.flush()
            os.fsync(assembled.fileno())
        actual_md5 = hash_file(temporary_output)
        if not hmac.compare_digest(actual_md5, manifest.file_md5):
            raise IntegrityError(
                f"final checksum mismatch: expected {manifest.file_md5}, got {actual_md5}"
            )
        os.replace(temporary_output, output)
        temporary_output = None
        shutil.rmtree(state)
        try:
            state.parent.rmdir()
        except OSError:
            # Other manifests may still have resumable chunks here.
            pass
        return output
    except Exception as error:
        if isinstance(error, TransferError):
            raise
        raise TransferError(f"cannot assemble output {output}: {error}") from error
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)


def _workers_argument(value: str) -> int:
    try:
        return positive_workers(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _nonnegative_argument(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error
    if result < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return result


def _size_argument(value: str) -> int:
    try:
        return parse_size(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = TransferArgumentParser(
        description="Upload and download files in parallel chunks.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    push = commands.add_parser("push", help="upload a file and write its manifest")
    push.add_argument("file", type=Path, help="file to upload")
    push.add_argument("--url", help="upload endpoint (or FILE_TRANSFER_URL)")
    push.add_argument("--auth", help="authorization value (or FILE_TRANSFER_AUTH)")
    push.add_argument("--manifest", type=Path, default=Path("manifest.txt"))
    push.add_argument("--chunk-size", type=_size_argument, default=parse_size("90M"))
    push.add_argument("--workers", type=_workers_argument, default=4)
    push.add_argument("--retries", type=_nonnegative_argument, default=2)
    push.add_argument("--expires", type=_nonnegative_argument, default=3600)

    pull = commands.add_parser("pull", help="download and assemble a manifest")
    pull.add_argument("manifest", type=Path, nargs="?", default=Path("manifest.txt"))
    pull.add_argument("--output", type=Path)
    pull.add_argument("--auth", help="authorization value (or FILE_TRANSFER_AUTH)")
    pull.add_argument("--workers", type=_workers_argument, default=4)
    pull.add_argument("--retries", type=_nonnegative_argument, default=2)
    return parser


def console_progress(index: int, total: int, state: str, detail: str) -> None:
    with _PROGRESS_LOCK:
        print(f"[{index}/{total}] {state} {detail}", file=sys.stderr, flush=True)


def _validated_upload_url(value: str | None) -> str:
    if value is None:
        raise CLIUsageError("push requires --url or FILE_TRANSFER_URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CLIUsageError("--url must be an http or https URL with a hostname")
    return value


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = build_parser()
    environment = os.environ if env is None else env
    try:
        arguments = parser.parse_args(argv)
        auth = resolve_setting(arguments.auth, "FILE_TRANSFER_AUTH", environment)
        retry_policy = RetryPolicy(max_attempts=arguments.retries + 1)
        if arguments.command == "push":
            url = _validated_upload_url(
                resolve_setting(arguments.url, "FILE_TRANSFER_URL", environment)
            )
            push_file(
                path=arguments.file,
                url=url,
                auth=auth,
                manifest_path=arguments.manifest,
                chunk_size=arguments.chunk_size,
                workers=arguments.workers,
                expires=arguments.expires,
                retry_policy=retry_policy,
                progress=console_progress,
            )
            print(arguments.manifest)
        else:
            output = pull_manifest(
                manifest_path=arguments.manifest,
                output_path=arguments.output,
                auth=auth,
                workers=arguments.workers,
                retry_policy=retry_policy,
                progress=console_progress,
            )
            print(output)
        return 0
    except CLIUsageError as error:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2
    except TransferError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
