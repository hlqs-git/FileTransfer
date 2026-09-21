# Parallel Cross-Platform File Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a zero-dependency Python 3 CLI that uploads and downloads file chunks concurrently on Windows and Linux while preserving integrity, retries, legacy manifests, and resumable downloads.

**Architecture:** Keep distribution to one executable Python source file, `file-transfer.py`, organized into importable data classes and focused functions. Use `ThreadPoolExecutor` for chunk concurrency, `http.client` for streaming HTTP without loading chunks into memory, and atomic same-directory temporary files for manifests, downloaded chunks, and final output.

**Tech Stack:** Python 3 standard library (`argparse`, `concurrent.futures`, `dataclasses`, `hashlib`, `http.client`, `pathlib`, `tempfile`, `unittest`, `http.server`)

**Spec:** `docs/superpowers/specs/2026-09-21-parallel-cross-platform-transfer-design.md`

## Global Constraints

- Support Windows and Linux with Python 3.10 or newer and no third-party packages.
- Expose `python file-transfer.py push <file>` and `python file-transfer.py pull [manifest]`.
- Default to 4 workers; accept only 1 through 16.
- Default chunk size is 90 MiB.
- A chunk gets at most 3 total attempts: the initial request plus 2 retries.
- Default uploaded-object lifetime is 3600 seconds; `--expires 0` selects one-time download mode.
- CLI values override `FILE_TRANSFER_URL` and `FILE_TRANSFER_AUTH`.
- Keep `file-push.sh` and `file-pull.sh`; the Python CLI becomes the recommended interface.
- Read existing version-1 manifests and write version-2 manifests that old Bash pull logic can still consume.
- Never log or serialize the authentication token.
- Never publish a partial upload manifest or a checksum-invalid final download.

## File Structure

- Create `file-transfer.py`: importable implementation and CLI entry point; contains configuration, manifest parsing, HTTP streaming, retry policy, upload/download orchestration, and `main()`.
- Create `tests/test_file_transfer.py`: standard-library unit and local HTTP integration tests; dynamically imports `file-transfer.py` by path.
- Modify `README.md`: cross-platform installation, push/pull examples, options, environment variables, retry/resume behavior, manifest compatibility, and Bash compatibility notice.
- Keep `tests/file-transfer-test.sh`: regression coverage for the legacy Bash scripts; do not rewrite or remove it.

## Review Focus

- An existing destination file must remain byte-for-byte unchanged when a download or final checksum verification fails; Task 4 adds this test.
- Empty source files must produce a valid zero-chunk manifest and restore as an empty file; Tasks 3 and 4 add these tests.
- Names with spaces, non-ASCII characters, leading hyphens, Windows separators, or POSIX separators must resolve to one safe basename; Task 1 adds this test matrix.
- Cross-origin redirects must not forward `Authorization`; Task 2 adds a two-server integration test.
- A response containing unrelated text or several URLs must accept exactly one valid HTTP(S) download URL and reject ambiguous or missing results; Task 2 adds these parser tests.

---

### Task 1: Manifest, chunk planning, and configuration primitives

**Files:**
- Create: `file-transfer.py`
- Create: `tests/test_file_transfer.py`

**Interfaces:**
- Produces: `TransferError`, `ManifestError`, `ChunkSpec`, `Manifest`, `parse_size(text: str) -> int`, `safe_basename(value: str) -> str`, `plan_chunks(file_size: int, chunk_size: int) -> tuple[ChunkSpec, ...]`, `hash_file(path: Path) -> str`, `parse_manifest(text: str) -> Manifest`, `serialize_manifest(manifest: Manifest) -> str`, `atomic_write_text(path: Path, text: str, replace: Callable = os.replace) -> None`, `positive_workers(value: str) -> int`, and `resolve_setting(cli_value: str | None, env_name: str, env: Mapping[str, str]) -> str | None`.
- Consumes: no project code; Python standard library only.

- [ ] **Step 1: Create a dynamic import helper and failing primitive tests**

Create `tests/test_file_transfer.py` with an import helper that can load a hyphenated script and literal expectations:

```python
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "file-transfer.py"


def load_module():
    spec = importlib.util.spec_from_file_location("file_transfer_cli", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def test_parse_size_uses_binary_units(self):
        self.assertEqual(self.ft.parse_size("90M"), 90 * 1024 * 1024)
        self.assertEqual(self.ft.parse_size("1GiB"), 1024**3)
        self.assertEqual(self.ft.parse_size("512"), 512)

    def test_parse_size_rejects_zero_negative_and_unknown_units(self):
        for value in ("0", "-1", "2MBX", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ft.parse_size(value)

    def test_safe_basename_handles_cross_platform_names(self):
        cases = {
            "/mnt/source/archive.tar.gz": "archive.tar.gz",
            r"C:\\source\\archive.tar.gz": "archive.tar.gz",
            "folder/file with spaces.bin": "file with spaces.bin",
            "/tmp/数据.bin": "数据.bin",
            "/tmp/-b": "-b",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.ft.safe_basename(raw), expected)
        for raw in ("", ".", "..", "/", "C:\\"):
            with self.subTest(raw=raw), self.assertRaises(self.ft.ManifestError):
                self.ft.safe_basename(raw)

    def test_plan_chunks_covers_file_without_overlap(self):
        chunks = self.ft.plan_chunks(11, 4)
        self.assertEqual(
            [(c.index, c.offset, c.length) for c in chunks],
            [(0, 0, 4), (1, 4, 4), (2, 8, 3)],
        )
        self.assertEqual(self.ft.plan_chunks(0, 4), [])

    def test_workers_accept_only_one_through_sixteen(self):
        self.assertEqual(self.ft.positive_workers("4"), 4)
        for value in ("0", "17", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ft.positive_workers(value)

    def test_cli_setting_wins_over_environment(self):
        env = {"FILE_TRANSFER_AUTH": "environment"}
        self.assertEqual(
            self.ft.resolve_setting("command", "FILE_TRANSFER_AUTH", env),
            "command",
        )
        self.assertEqual(
            self.ft.resolve_setting(None, "FILE_TRANSFER_AUTH", env),
            "environment",
        )
```

- [ ] **Step 2: Run primitive tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.PrimitiveTests -v
```

Expected: FAIL because `file-transfer.py` or the named interfaces do not exist.

- [ ] **Step 3: Implement primitive models and helpers**

Create `file-transfer.py` with these exact public shapes:

```python
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
```

Implement binary suffix parsing for `K`, `KB`, `KiB`, `M`, `MB`, `MiB`, `G`, `GB`, and `GiB`; normalize both slash styles before selecting a basename; build exact non-overlapping chunk ranges; hash files in 1 MiB blocks; and parse worker counts with a clear `argparse.ArgumentTypeError` or `ValueError` message.

- [ ] **Step 4: Add failing manifest compatibility and atomic-write tests**

Extend `tests/test_file_transfer.py`:

```python
class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def test_reads_legacy_absolute_name_and_chunk_order(self):
        text = (
            "HASH:5d41402abc4b2a76b9719d911017c592\n"
            "NAME:/mnt/source/archive.tar.gz\n"
            "aaaabbbbccccddddeeeeffff00001111|https://files.test/a.bin\n"
            "11110000ffffeeeeddddccccbbbbaaaa|http://files.test/b.bin\n"
        )
        manifest = self.ft.parse_manifest(text)
        self.assertEqual(manifest.name, "archive.tar.gz")
        self.assertEqual([chunk.url for chunk in manifest.chunks], [
            "https://files.test/a.bin", "http://files.test/b.bin"
        ])

    def test_version_two_round_trip_is_literal_and_bash_compatible(self):
        chunks = (
            self.ft.ChunkSpec(0, 0, 5, "5d41402abc4b2a76b9719d911017c592", "https://files.test/a.bin"),
        )
        manifest = self.ft.Manifest(
            name="数据 file.bin", file_md5="5d41402abc4b2a76b9719d911017c592",
            chunks=chunks, size=5, chunk_size=94371840, expires=3600, version=2,
        )
        text = self.ft.serialize_manifest(manifest)
        self.assertEqual(text.splitlines()[:6], [
            "HASH:5d41402abc4b2a76b9719d911017c592",
            "NAME:数据 file.bin",
            "VERSION:2",
            "SIZE:5",
            "CHUNK_SIZE:94371840",
            "EXPIRES:3600",
        ])
        self.assertEqual(self.ft.parse_manifest(text), manifest)

    def test_rejects_duplicate_missing_or_malformed_fields(self):
        invalid = (
            "NAME:a\n",
            "HASH:" + "0" * 32 + "\nNAME:a\nNAME:b\n",
            "HASH:not-md5\nNAME:a\n",
            "HASH:" + "0" * 32 + "\nNAME:..\n",
            "HASH:" + "0" * 32 + "\nNAME:a\nbad chunk\n",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(self.ft.ManifestError):
                self.ft.parse_manifest(text)

    def test_atomic_write_preserves_existing_file_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.txt"
            target.write_text("old", encoding="utf-8")
            with self.assertRaises(OSError):
                self.ft.atomic_write_text(target, "new", replace=lambda *_: (_ for _ in ()).throw(OSError("blocked")))
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
```

- [ ] **Step 5: Run manifest tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.ManifestTests -v
```

Expected: FAIL because manifest parsing, serialization, and atomic writes are not implemented.

- [ ] **Step 6: Implement strict backward-compatible manifest handling**

Implement `parse_manifest`, `serialize_manifest`, and `atomic_write_text`. Parse with `partition(":")` for metadata and `partition("|")` for chunk rows so URLs keep colons. Validate MD5 values with exactly 32 hexadecimal characters, allow only one `HASH` and one `NAME`, ignore unknown `KEY:value` metadata, and assign chunk indexes by row order. `atomic_write_text` must create a UTF-8 temporary file in the target directory, flush and `os.fsync`, call the injectable `replace` function (default `os.replace`), and remove the temporary file on error.

- [ ] **Step 7: Run Task 1 tests and commit**

Run:

```text
python -m unittest tests.test_file_transfer.PrimitiveTests tests.test_file_transfer.ManifestTests -v
python -m py_compile file-transfer.py tests/test_file_transfer.py
```

Expected: all tests PASS and compilation exits 0.

Commit:

```text
git add file-transfer.py tests/test_file_transfer.py
git commit -m "Add transfer manifest and configuration primitives"
```

---

### Task 2: Streaming HTTP, URL extraction, redirects, and retry policy

**Files:**
- Modify: `file-transfer.py`
- Modify: `tests/test_file_transfer.py`

**Interfaces:**
- Consumes: `TransferError`, `ChunkSpec` from Task 1.
- Produces: `HTTPStatusError(status: int, reason: str, headers: Mapping[str, str] | None = None)`, `RetryPolicy(max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 30.0)`, `extract_download_url(text: str) -> str`, `upload_once(source_path: Path, chunk: ChunkSpec, url: str, auth: str | None, expires: int, timeout: float) -> str`, `download_once(url: str, destination: Path, auth: str | None, timeout: float) -> None`, and generic `run_with_retry(operation: Callable[[], T], policy: RetryPolicy, sleep: Callable[[float], None]) -> T`. Internal `_request_stream` owns connection construction, bounded request streaming, response streaming, and redirects.

- [ ] **Step 1: Add failing response parsing and retry tests**

Add tests with hand-derived outcomes:

```python
class HttpPrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def test_extracts_one_http_or_https_download_url(self):
        self.assertEqual(
            self.ft.extract_download_url("uploaded\nhttps://r2.test/a.bin\nexpires soon"),
            "https://r2.test/a.bin",
        )
        for body in ("no link", "http://a.test/x.bin https://b.test/y.bin"):
            with self.subTest(body=body), self.assertRaises(self.ft.TransferError):
                self.ft.extract_download_url(body)

    def test_retry_uses_three_total_attempts_and_expected_delays(self):
        attempts = []
        delays = []
        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("temporary")
            return "ok"
        result = self.ft.run_with_retry(
            operation,
            self.ft.RetryPolicy(max_attempts=3, base_delay=1, max_delay=30),
            delays.append,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [1, 2])

    def test_authentication_and_not_found_are_not_retried(self):
        for status in (401, 403, 404):
            calls = []
            def operation(status=status):
                calls.append(status)
                raise self.ft.HTTPStatusError(status, "failure")
            with self.subTest(status=status), self.assertRaises(self.ft.HTTPStatusError):
                self.ft.run_with_retry(operation, self.ft.RetryPolicy(), lambda _: None)
            self.assertEqual(calls, [status])
```

- [ ] **Step 2: Run HTTP primitive tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.HttpPrimitiveTests -v
```

Expected: FAIL because the HTTP and retry interfaces do not exist.

- [ ] **Step 3: Implement deterministic retry classification and URL extraction**

Use `urllib.parse.urlsplit` to validate extracted URLs. Retry `OSError`, `TimeoutError`, `http.client.HTTPException`, HTTP 408, 429, and 500 through 599. Retry checksum mismatch through a dedicated `IntegrityError`. Do not retry other 4xx responses. If an `HTTPStatusError` contains a numeric `Retry-After` header, clamp it to `RetryPolicy.max_delay`; otherwise use `base_delay * 2**(attempt - 1)`.

- [ ] **Step 4: Add failing local-server streaming, status, and redirect tests**

Add a reusable `LocalTransferServer` test utility based on `ThreadingHTTPServer`. Its handler must read exactly `Content-Length`, record method/path/headers/body, optionally delay, return scripted statuses, and stream scripted GET bodies. Add tests that prove:

```python
def test_upload_once_streams_exact_range_and_headers(self):
    # Fixture source bytes: b"0123456789"; upload offset 3, length 4.
    # Assert server receives b"3456", Content-Length "4",
    # Authorization "secret", and X-Expiration-Seconds "3600".

def test_download_once_writes_response_to_temporary_path(self):
    # Server returns b"hello". Assert the target contains exactly b"hello".

def test_cross_origin_redirect_drops_authorization(self):
    # Server A redirects GET to server B. Assert A receives Authorization,
    # B receives no Authorization, and the downloaded body is correct.

def test_same_origin_redirect_keeps_authorization(self):
    # One server redirects /old to /new. Assert /new receives Authorization.
```

The two-server test is mandatory: it is the mutation check for accidental credential forwarding.

- [ ] **Step 5: Run streaming integration tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.HttpStreamingTests -v
```

Expected: FAIL because streaming request functions and safe redirect handling are missing.

- [ ] **Step 6: Implement bounded streaming HTTP**

Implement a `LimitedReader` that wraps an independently opened source file and exposes `read(size)` without passing the chunk boundary. Build `HTTPConnection` or `HTTPSConnection` from `urlsplit`, send explicit `Content-Length`, then copy request or response bodies in 1 MiB blocks. Close the response and connection in `finally` blocks.

Follow GET redirects for 301, 302, 303, 307, and 308; preserve PUT only for 307 and 308 and reject ambiguous PUT redirects. Resolve relative `Location` with `urljoin`, allow at most 5 redirects, and compare `(scheme, hostname, effective_port)` before carrying `Authorization` to the next request.

- [ ] **Step 7: Run Task 2 tests and commit**

Run:

```text
python -m unittest tests.test_file_transfer.HttpPrimitiveTests tests.test_file_transfer.HttpStreamingTests -v
python -m py_compile file-transfer.py tests/test_file_transfer.py
```

Expected: all tests PASS and compilation exits 0.

Commit:

```text
git add file-transfer.py tests/test_file_transfer.py
git commit -m "Add streaming HTTP retries and safe redirects"
```

---

### Task 3: Concurrent upload and atomic manifest publication

**Files:**
- Modify: `file-transfer.py`
- Modify: `tests/test_file_transfer.py`

**Interfaces:**
- Consumes: `Manifest`, `ChunkSpec`, `plan_chunks`, `hash_file`, `upload_once`, `run_with_retry`, `serialize_manifest`, and `atomic_write_text`.
- Produces: `ProgressCallback = Callable[[int, int, str, str], None]`, `compute_chunk_md5(path: Path, chunk: ChunkSpec) -> str`, `upload_chunk(path: Path, chunk: ChunkSpec, url: str, auth: str | None, expires: int, retry_policy: RetryPolicy) -> ChunkSpec`, and `push_file(path: Path, url: str, auth: str | None, manifest_path: Path, chunk_size: int, workers: int, expires: int, retry_policy: RetryPolicy, progress: ProgressCallback) -> Manifest`.

- [ ] **Step 1: Add failing successful-upload integration tests**

Extend the local server so PUT responses can complete out of order and track peak concurrent requests. Add tests:

```python
class PushTests(unittest.TestCase):
    def test_push_is_concurrent_and_manifest_remains_in_source_order(self):
        # Write bytes b"abcdefghijkl" and use chunk_size=3, workers=4.
        # The server delays chunks in reverse order and returns one unique URL
        # per received body. Assert peak_active_requests >= 2, Manifest chunk
        # indexes are [0, 1, 2, 3], and each MD5/URL matches its source slice.

    def test_push_sends_default_expiration_header(self):
        # Assert every PUT has X-Expiration-Seconds: 3600.

    def test_push_expires_zero_omits_expiration_header(self):
        # Assert a push with expires=0 sends no X-Expiration-Seconds header.

    def test_push_empty_file_writes_valid_zero_chunk_manifest(self):
        # Assert HASH is d41d8cd98f00b204e9800998ecf8427e,
        # SIZE is 0, and no chunk rows exist.
```

- [ ] **Step 2: Run successful-upload tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.PushTests -v
```

Expected: FAIL because upload orchestration is missing.

- [ ] **Step 3: Implement concurrent upload with ordered results**

Compute the full-file MD5 before starting uploads. Submit one future per `ChunkSpec` to `ThreadPoolExecutor(max_workers=workers)`. Each worker opens the file independently, hashes only its range, and creates a fresh `LimitedReader` for each HTTP attempt. Store completed results by `chunk.index`, never by completion order. Invoke `progress(index, total, state, detail)` only from the coordinator thread after consuming completed futures.

For an empty file, skip the executor and publish a valid zero-chunk manifest. Use `safe_basename` for the manifest name and URL-encode no local filename because the service assigns its own object name.

- [ ] **Step 4: Add failing upload-failure atomicity tests**

Add tests with a pre-existing manifest containing literal `old manifest`:

```python
def test_failed_push_does_not_overwrite_existing_manifest(self):
    # Server makes one chunk return 500 for all three attempts.
    # Assert push_file raises, existing manifest remains "old manifest",
    # and the failed chunk was attempted exactly three times.

def test_unauthorized_push_stops_without_retry(self):
    # Server returns 401. Assert one request for that chunk and no manifest.
```

- [ ] **Step 5: Run failure tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.PushTests.test_failed_push_does_not_overwrite_existing_manifest tests.test_file_transfer.PushTests.test_unauthorized_push_stops_without_retry -v
```

Expected: FAIL until `push_file` only calls `atomic_write_text` after all futures succeed and cancels pending futures on terminal failure.

- [ ] **Step 6: Implement failure cancellation and atomic publication**

On the first terminal future failure, call `cancel()` for futures not started, let running request contexts close, raise a `TransferError` that names the chunk and attempt count, and leave the existing manifest untouched. Only construct and atomically serialize `Manifest(version=2, size=..., chunk_size=..., expires=...)` after every result slot is populated.

- [ ] **Step 7: Run Task 3 tests and commit**

Run:

```text
python -m unittest tests.test_file_transfer.PushTests -v
python -m unittest tests.test_file_transfer.PrimitiveTests tests.test_file_transfer.ManifestTests tests.test_file_transfer.HttpPrimitiveTests tests.test_file_transfer.HttpStreamingTests -v
```

Expected: all tests PASS.

Commit:

```text
git add file-transfer.py tests/test_file_transfer.py
git commit -m "Add concurrent chunk uploads"
```

---

### Task 4: Concurrent download, verified resume, and atomic assembly

**Files:**
- Modify: `file-transfer.py`
- Modify: `tests/test_file_transfer.py`

**Interfaces:**
- Consumes: `Manifest`, `ChunkSpec`, `hash_file`, `download_once`, and `run_with_retry`.
- Produces: `manifest_fingerprint(manifest_text: str) -> str`, `state_directory(output_path: Path, manifest_text: str) -> Path`, `download_chunk(chunk: ChunkSpec, state_dir: Path, auth: str | None, retry_policy: RetryPolicy) -> Path`, and `pull_manifest(manifest_path: Path, output_path: Path | None, auth: str | None, workers: int, retry_policy: RetryPolicy, progress: ProgressCallback) -> Path`.

- [ ] **Step 1: Add failing concurrent download and resume tests**

Add GET fixtures keyed by URL path and tests:

```python
class PullTests(unittest.TestCase):
    def test_pull_downloads_concurrently_and_assembles_in_manifest_order(self):
        # Manifest rows point to /c, /a, /b but represent b"ccc", b"aaa", b"bbb".
        # Delay responses in reverse completion order. Assert peak GET activity
        # >= 2 and output bytes are b"cccaaabbb".

    def test_pull_reuses_only_checksum_valid_completed_chunks(self):
        # Seed state with one valid chunk and one corrupt chunk. Assert the
        # server does not receive a request for the valid path, does receive
        # one for the corrupt path, and final content is correct.

    def test_pull_retries_checksum_mismatch(self):
        # First GET returns corrupt bytes and second returns correct bytes.
        # Assert exactly two requests and a correct final file.

    def test_pull_empty_manifest_creates_verified_empty_file(self):
        # Use the empty MD5 and zero chunks. Assert output exists with size 0.
```

- [ ] **Step 2: Run download and resume tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.PullTests -v
```

Expected: FAIL because pull orchestration and state directories are missing.

- [ ] **Step 3: Implement deterministic state and verified chunk reuse**

Compute the state key as the first 16 hexadecimal characters of SHA-256 over the exact manifest bytes. Store chunks as `part_000000.bin`, `part_000001.bin`, and so on under `.file-transfer/<safe-name>-<key>/` beside the output file. Before scheduling, hash existing part files and skip only exact matches. Download to `<part>.tmp`, flush and `fsync`, verify MD5, then `os.replace` it into the completed part path.

Submit only missing or corrupt parts to the executor. Keep manifest order solely in chunk indexes, not filesystem enumeration order.

- [ ] **Step 4: Add failing final-output safety tests**

Add these tests:

```python
def test_final_md5_failure_preserves_existing_destination_and_parts(self):
    # Pre-create output with b"important old file" and use a manifest whose
    # per-part MD5 values pass but whose final HASH is intentionally different.
    # Assert pull raises, output stays unchanged, and verified part files remain.

def test_404_is_not_retried_and_preserves_resume_state(self):
    # Assert one GET, no final output, and any other verified part remains.

def test_output_name_from_windows_legacy_path_is_safe(self):
    # NAME:C:\\source\\数据 file.bin restores only 数据 file.bin under the
    # selected output directory, never under a manifest-provided directory.
```

- [ ] **Step 5: Run output-safety tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.PullTests.test_final_md5_failure_preserves_existing_destination_and_parts tests.test_file_transfer.PullTests.test_404_is_not_retried_and_preserves_resume_state tests.test_file_transfer.PullTests.test_output_name_from_windows_legacy_path_is_safe -v
```

Expected: FAIL until final assembly uses a same-directory temporary file and replaces the destination only after full-file verification.

- [ ] **Step 6: Implement atomic assembly and cleanup**

Create the final temporary output beside the destination, concatenate parts in numeric index order using 1 MiB copies, flush and `fsync`, calculate its MD5, and compare with `Manifest.file_md5` using `hmac.compare_digest`. On success, `os.replace` the destination and remove only this manifest's state directory. On any error, delete the incomplete final temporary file, preserve the previous destination and verified parts, and raise a concise `TransferError`.

- [ ] **Step 7: Run Task 4 tests and commit**

Run:

```text
python -m unittest tests.test_file_transfer.PullTests -v
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: all Python tests PASS.

Commit:

```text
git add file-transfer.py tests/test_file_transfer.py
git commit -m "Add concurrent resumable downloads"
```

---

### Task 5: CLI wiring, progress output, documentation, and end-to-end verification

**Files:**
- Modify: `file-transfer.py`
- Modify: `tests/test_file_transfer.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `push_file` and `pull_manifest` from Tasks 3 and 4.
- Produces: `build_parser() -> argparse.ArgumentParser`, `console_progress(index: int, total: int, state: str, detail: str) -> None`, and `main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int`; executable behavior under `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 1: Add failing CLI parsing and exit-code tests**

Add tests that invoke `main()` with injected `argv` and environment mappings while capturing stdout/stderr:

```python
class CliTests(unittest.TestCase):
    def test_push_cli_prefers_flags_over_environment(self):
        # Patch push_file and call main with --url/--auth plus conflicting env.
        # Assert push_file receives flag values, workers=4, expires=3600,
        # chunk_size=90*1024*1024, and max_attempts=3.

    def test_push_requires_url_but_allows_empty_auth(self):
        # No flag and no FILE_TRANSFER_URL returns exit code 2 with a useful error.

    def test_pull_defaults_manifest_and_output(self):
        # Patch pull_manifest, call main(["pull"]), and assert manifest.txt is used.

    def test_invalid_workers_returns_usage_error(self):
        # --workers 17 returns exit code 2 before starting transfer.

    def test_cli_passes_custom_transfer_options(self):
        # Call push with --manifest custom.txt --chunk-size 8M --workers 8
        # --retries 4 --expires 7200. Assert push_file receives the literal
        # path, 8*1024*1024 bytes, 8 workers, max_attempts=5, and 7200 seconds.
        # Call pull with --output restored.bin --workers 6 --retries 1 and
        # assert pull_manifest receives that path, 6 workers, and max_attempts=2.

    def test_transfer_error_returns_one_without_traceback_or_token(self):
        # Patch push_file to raise TransferError containing a safe message.
        # Assert exit 1, no traceback, and supplied token absent from stderr.
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```text
python -m unittest tests.test_file_transfer.CliTests -v
```

Expected: FAIL because the parser and `main` wiring do not exist.

- [ ] **Step 3: Implement the CLI and thread-safe progress rendering**

Build `argparse` subparsers with the exact options from the spec. Normalize upload URLs by requiring `http` or `https` and a hostname. Accept an empty authentication value for unprotected deployments. Convert internal errors to exit code 1 and usage/configuration errors to exit code 2. Handle `KeyboardInterrupt` with exit code 130 and a short cancellation message.

Use a `threading.Lock` in `console_progress` and print one complete line per event:

```text
[3/46] upload attempt 1
[3/46] upload complete
[8/46] download retry 2 in 1s
```

Never place request headers or authentication values in progress details.

- [ ] **Step 4: Update README with runnable Windows and Linux instructions**

Replace the Bash-only primary workflow with these literal examples while retaining a legacy section:

```text
# Windows PowerShell
$env:FILE_TRANSFER_URL = "https://r2.example.com"
$env:FILE_TRANSFER_AUTH = "your-token"
python .\file-transfer.py push C:\path\archive.zip
python .\file-transfer.py pull .\manifest.txt

# Linux
export FILE_TRANSFER_URL="https://r2.example.com"
export FILE_TRANSFER_AUTH="your-token"
python3 ./file-transfer.py push /path/archive.tar.gz
python3 ./file-transfer.py pull ./manifest.txt
```

Document every flag, the 4-worker/16-worker-limit behavior, 3-total-attempt retry policy, one-hour default expiration, `--expires 0` risk, download state directory, old manifest compatibility, and the fact that upload cannot resume across separate runs.

- [ ] **Step 5: Run CLI tests and full cross-platform-safe test suite**

Run:

```text
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile file-transfer.py tests/test_file_transfer.py
python file-transfer.py --help
python file-transfer.py push --help
python file-transfer.py pull --help
git diff --check
```

Expected: all tests PASS; compilation and help commands exit 0; diff check reports no errors.

On a Linux environment with Bash and GNU coreutils, also run:

```text
sh tests/file-transfer-test.sh
```

Expected: `All file transfer tests passed.` If the current host lacks Bash/GNU coreutils, report that constraint explicitly and retain the previously passing legacy regression evidence rather than claiming a fresh Bash result.

- [ ] **Step 6: Perform manual localhost smoke test without external R2**

Start the test HTTP server fixture through its test harness, push a generated file larger than two test chunks, pull its manifest into a separate directory, and compare hashes. The smoke-test assertion must be the literal source and destination MD5 equality, not reuse of the production comparison helper.

Run the dedicated end-to-end test added to `CliTests`:

```text
python -m unittest tests.test_file_transfer.CliTests.test_cli_push_then_pull_round_trip -v
```

Expected: PASS with at least two PUT requests and two GET requests recorded by the local server.

- [ ] **Step 7: Commit the completed CLI and documentation**

```text
git add file-transfer.py tests/test_file_transfer.py README.md
git commit -m "Add cross-platform parallel transfer CLI"
```

- [ ] **Step 8: Request final code review and verify the reviewed tree**

Request a read-only review against the design and this plan, fix every Critical or Important issue, then rerun:

```text
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile file-transfer.py tests/test_file_transfer.py
git diff --check HEAD~5..HEAD
git status --short
```

Expected: all tests PASS, compilation exits 0, diff check is clean, and the working tree contains no unintended files.
