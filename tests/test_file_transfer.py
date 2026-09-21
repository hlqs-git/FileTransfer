from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "file-transfer.py"


def load_module():
    spec = importlib.util.spec_from_file_location("file_transfer_cli", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalTransferServer:
    def __init__(self):
        self.routes = {}
        self.requests = []
        self.route_calls = {}
        self.active_requests = 0
        self.peak_active_requests = 0
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def _record(self, body=b""):
                record = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": body,
                }
                with owner.lock:
                    owner.requests.append(record)
                return record

            def _respond(self, record):
                route = owner.routes.get(self.path, {})
                with owner.lock:
                    call_number = owner.route_calls.get(self.path, 0) + 1
                    owner.route_calls[self.path] = call_number
                if callable(route):
                    route = route(record, call_number)
                time.sleep(route.get("delay", 0))
                self.send_response(route.get("status", 200))
                for key, value in route.get("headers", {}).items():
                    self.send_header(key, value)
                body = route.get("body", b"")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._handle(self._record())

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", "0"))
                self._handle(self._record(self.rfile.read(length)))

            def _handle(self, record):
                with owner.lock:
                    owner.active_requests += 1
                    owner.peak_active_requests = max(
                        owner.peak_active_requests,
                        owner.active_requests,
                    )
                try:
                    self._respond(record)
                finally:
                    with owner.lock:
                        owner.active_requests -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


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
            r"C:\source\archive.tar.gz": "archive.tar.gz",
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
        self.assertEqual(self.ft.plan_chunks(0, 4), ())

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
        self.assertEqual(
            [chunk.url for chunk in manifest.chunks],
            ["https://files.test/a.bin", "http://files.test/b.bin"],
        )

    def test_version_two_round_trip_is_literal_and_bash_compatible(self):
        chunks = (
            self.ft.ChunkSpec(
                0,
                0,
                5,
                "5d41402abc4b2a76b9719d911017c592",
                "https://files.test/a.bin",
            ),
        )
        manifest = self.ft.Manifest(
            name="数据 file.bin",
            file_md5="5d41402abc4b2a76b9719d911017c592",
            chunks=chunks,
            size=5,
            chunk_size=94371840,
            expires=3600,
            version=2,
        )
        text = self.ft.serialize_manifest(manifest)
        self.assertEqual(
            text.splitlines()[:6],
            [
                "HASH:5d41402abc4b2a76b9719d911017c592",
                "NAME:数据 file.bin",
                "VERSION:2",
                "SIZE:5",
                "CHUNK_SIZE:94371840",
                "EXPIRES:3600",
            ],
        )
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

            def blocked_replace(*_):
                raise OSError("blocked")

            with self.assertRaises(OSError):
                self.ft.atomic_write_text(target, "new", replace=blocked_replace)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class HttpPrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def test_extracts_one_http_or_https_download_url(self):
        self.assertEqual(
            self.ft.extract_download_url(
                "uploaded\nhttps://r2.test/a.bin\nexpires soon"
            ),
            "https://r2.test/a.bin",
        )
        for body in (
            "no link",
            "http://a.test/x.bin https://b.test/y.bin",
        ):
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

            with self.subTest(status=status), self.assertRaises(
                self.ft.HTTPStatusError
            ):
                self.ft.run_with_retry(
                    operation,
                    self.ft.RetryPolicy(),
                    lambda _: None,
                )
            self.assertEqual(calls, [status])


class HttpStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def test_upload_once_streams_exact_range_and_headers(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            source = Path(directory) / "source.bin"
            source.write_bytes(b"0123456789")
            server.routes["/upload"] = {
                "body": b"uploaded\nhttps://r2.test/result.bin\n"
            }
            chunk = self.ft.ChunkSpec(0, 3, 4)
            result = self.ft.upload_once(
                source, chunk, server.url + "/upload", "secret", 3600, 5
            )
            self.assertEqual(result, "https://r2.test/result.bin")
            request = server.requests[0]
            self.assertEqual(request["body"], b"3456")
            self.assertEqual(request["headers"]["content-length"], "4")
            self.assertEqual(request["headers"]["authorization"], "secret")
            self.assertEqual(request["headers"]["x-expiration-seconds"], "3600")

    def test_download_once_writes_response_to_temporary_path(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            destination = Path(directory) / "part.tmp"
            server.routes["/part"] = {"body": b"hello"}
            self.ft.download_once(server.url + "/part", destination, None, 5)
            self.assertEqual(destination.read_bytes(), b"hello")

    def test_cross_origin_redirect_drops_authorization(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as first, LocalTransferServer() as second:
            destination = Path(directory) / "part.tmp"
            first.routes["/old"] = {
                "status": 302,
                "headers": {"Location": second.url + "/new"},
            }
            second.routes["/new"] = {"body": b"redirected"}
            self.ft.download_once(first.url + "/old", destination, "secret", 5)
            self.assertEqual(destination.read_bytes(), b"redirected")
            self.assertEqual(first.requests[0]["headers"]["authorization"], "secret")
            self.assertNotIn("authorization", second.requests[0]["headers"])

    def test_same_origin_redirect_keeps_authorization(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            destination = Path(directory) / "part.tmp"
            server.routes["/old"] = {
                "status": 302,
                "headers": {"Location": "/new"},
            }
            server.routes["/new"] = {"body": b"redirected"}
            self.ft.download_once(server.url + "/old", destination, "secret", 5)
            self.assertEqual(destination.read_bytes(), b"redirected")
            self.assertEqual(server.requests[1]["headers"]["authorization"], "secret")


class PushTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def _push(self, source, manifest_path, server, **overrides):
        options = {
            "path": source,
            "url": server.url + "/upload",
            "auth": "secret",
            "manifest_path": manifest_path,
            "chunk_size": 3,
            "workers": 4,
            "expires": 3600,
            "retry_policy": self.ft.RetryPolicy(
                max_attempts=3, base_delay=0, max_delay=0
            ),
            "progress": lambda *_: None,
        }
        options.update(overrides)
        return self.ft.push_file(**options)

    def test_push_is_concurrent_and_manifest_remains_in_source_order(self):
        delays = {b"abc": 0.20, b"def": 0.15, b"ghi": 0.10, b"jkl": 0.05}

        def upload_response(record, _):
            name = record["body"].decode("ascii")
            return {
                "delay": delays[record["body"]],
                "body": f"https://files.test/{name}.bin\n".encode("ascii"),
            }

        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/upload"] = upload_response
            source = Path(directory) / "source.bin"
            manifest_path = Path(directory) / "manifest.txt"
            source.write_bytes(b"abcdefghijkl")
            manifest = self._push(source, manifest_path, server)

            self.assertGreaterEqual(server.peak_active_requests, 2)
            self.assertEqual([chunk.index for chunk in manifest.chunks], [0, 1, 2, 3])
            self.assertEqual(
                [chunk.md5 for chunk in manifest.chunks],
                [
                    "900150983cd24fb0d6963f7d28e17f72",
                    "4ed9407630eb1000c0f6b63842defa7d",
                    "826bbc5d0522f5f20a1da4b60fa8c871",
                    "699a474e923b8da5d7aefbfc54a8a2bd",
                ],
            )
            self.assertEqual(
                [chunk.url for chunk in manifest.chunks],
                [
                    "https://files.test/abc.bin",
                    "https://files.test/def.bin",
                    "https://files.test/ghi.bin",
                    "https://files.test/jkl.bin",
                ],
            )
            self.assertEqual(self.ft.parse_manifest(manifest_path.read_text("utf-8")), manifest)

    def test_push_sends_default_expiration_header(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/upload"] = {"body": b"https://files.test/a.bin\n"}
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abc")
            self._push(source, Path(directory) / "manifest.txt", server)
            self.assertEqual(
                server.requests[0]["headers"]["x-expiration-seconds"], "3600"
            )

    def test_push_expires_zero_omits_expiration_header(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/upload"] = {"body": b"https://files.test/a.bin\n"}
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abc")
            self._push(
                source,
                Path(directory) / "manifest.txt",
                server,
                expires=0,
            )
            self.assertNotIn(
                "x-expiration-seconds", server.requests[0]["headers"]
            )

    def test_push_empty_file_writes_valid_zero_chunk_manifest(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            source = Path(directory) / "empty.bin"
            source.write_bytes(b"")
            manifest_path = Path(directory) / "manifest.txt"
            manifest = self._push(source, manifest_path, server)
            self.assertEqual(manifest.file_md5, "d41d8cd98f00b204e9800998ecf8427e")
            self.assertEqual(manifest.size, 0)
            self.assertEqual(manifest.chunks, ())
            self.assertEqual(server.requests, [])
            self.assertTrue(manifest_path.exists())

    def test_failed_push_does_not_overwrite_existing_manifest(self):
        def upload_response(record, _):
            if record["body"] == b"def":
                return {"status": 500, "body": b"temporary failure"}
            return {"body": b"https://files.test/ok.bin\n"}

        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/upload"] = upload_response
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abcdef")
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text("old manifest", encoding="utf-8")
            with self.assertRaises(self.ft.TransferError) as caught:
                self._push(source, manifest_path, server)
            self.assertEqual(manifest_path.read_text("utf-8"), "old manifest")
            self.assertIn("chunk 2", str(caught.exception))
            self.assertIn("3 attempts", str(caught.exception))
            failed_requests = [
                request for request in server.requests if request["body"] == b"def"
            ]
            self.assertEqual(len(failed_requests), 3)

    def test_unauthorized_push_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/upload"] = {"status": 401, "body": b"Unauthorized"}
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abc")
            manifest_path = Path(directory) / "manifest.txt"
            with self.assertRaises(self.ft.TransferError) as caught:
                self._push(source, manifest_path, server)
            self.assertEqual(len(server.requests), 1)
            self.assertFalse(manifest_path.exists())
            self.assertIn("1 attempt", str(caught.exception))


class PullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def _pull(self, manifest_path, output_path, **overrides):
        options = {
            "manifest_path": manifest_path,
            "output_path": output_path,
            "auth": "secret",
            "workers": 4,
            "retry_policy": self.ft.RetryPolicy(
                max_attempts=3, base_delay=0, max_delay=0
            ),
            "progress": lambda *_: None,
        }
        options.update(overrides)
        return self.ft.pull_manifest(**options)

    def _manifest_text(self, name, file_md5, rows, size, chunk_size=3):
        lines = [
            f"HASH:{file_md5}",
            f"NAME:{name}",
            "VERSION:2",
            f"SIZE:{size}",
            f"CHUNK_SIZE:{chunk_size}",
            "EXPIRES:3600",
        ]
        lines.extend(f"{md5}|{url}" for md5, url in rows)
        return "\n".join(lines) + "\n"

    def _state_path(self, output_path, manifest_text):
        key = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()[:16]
        return output_path.parent / ".file-transfer" / f"{output_path.name}-{key}"

    def test_pull_downloads_concurrently_and_assembles_in_manifest_order(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes.update(
                {
                    "/c": {"delay": 0.20, "body": b"ccc"},
                    "/a": {"delay": 0.15, "body": b"aaa"},
                    "/b": {"delay": 0.10, "body": b"bbb"},
                }
            )
            rows = [
                ("9df62e693988eb4e1e1444ece0578579", server.url + "/c"),
                ("47bce5c74f589f4867dbd57e9ca9f808", server.url + "/a"),
                ("08f8e0260c64418510cefb2b06eee5cd", server.url + "/b"),
            ]
            text = self._manifest_text(
                "archive.bin", "586b0f0c56cba518f29c07085cf80ff7", rows, 9
            )
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            output = Path(directory) / "restored.bin"
            result = self._pull(manifest_path, output)
            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"cccaaabbb")
            self.assertGreaterEqual(server.peak_active_requests, 2)

    def test_pull_reuses_only_checksum_valid_completed_chunks(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/a"] = {"body": b"aaa"}
            server.routes["/b"] = {"body": b"bbb"}
            rows = [
                ("47bce5c74f589f4867dbd57e9ca9f808", server.url + "/a"),
                ("08f8e0260c64418510cefb2b06eee5cd", server.url + "/b"),
            ]
            text = self._manifest_text(
                "archive.bin", "6547436690a26a399603a7096e876a2d", rows, 6
            )
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            output = Path(directory) / "restored.bin"
            state = self._state_path(output, text)
            state.mkdir(parents=True)
            (state / "part_000000.bin").write_bytes(b"aaa")
            (state / "part_000001.bin").write_bytes(b"corrupt")
            self._pull(manifest_path, output)
            self.assertEqual(output.read_bytes(), b"aaabbb")
            self.assertEqual([request["path"] for request in server.requests], ["/b"])

    def test_pull_retries_checksum_mismatch(self):
        def changing_response(_, call_number):
            return {"body": b"bad" if call_number == 1 else b"aaa"}

        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/part"] = changing_response
            rows = [("47bce5c74f589f4867dbd57e9ca9f808", server.url + "/part")]
            text = self._manifest_text(
                "archive.bin", "47bce5c74f589f4867dbd57e9ca9f808", rows, 3
            )
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            output = Path(directory) / "restored.bin"
            self._pull(manifest_path, output)
            self.assertEqual(output.read_bytes(), b"aaa")
            self.assertEqual(len(server.requests), 2)

    def test_pull_empty_manifest_creates_verified_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._manifest_text(
                "empty.bin", "d41d8cd98f00b204e9800998ecf8427e", [], 0
            )
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            output = Path(directory) / "restored.bin"
            self._pull(manifest_path, output)
            self.assertTrue(output.exists())
            self.assertEqual(output.stat().st_size, 0)

    def test_final_md5_failure_preserves_existing_destination_and_parts(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/part"] = {"body": b"aaa"}
            rows = [("47bce5c74f589f4867dbd57e9ca9f808", server.url + "/part")]
            text = self._manifest_text("archive.bin", "0" * 32, rows, 3)
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            output = Path(directory) / "restored.bin"
            output.write_bytes(b"important old file")
            state = self._state_path(output, text)
            with self.assertRaises(self.ft.TransferError):
                self._pull(manifest_path, output)
            self.assertEqual(output.read_bytes(), b"important old file")
            self.assertEqual((state / "part_000000.bin").read_bytes(), b"aaa")

    def test_404_is_not_retried_and_preserves_resume_state(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/missing"] = {"status": 404, "body": b"gone"}
            rows = [
                ("47bce5c74f589f4867dbd57e9ca9f808", server.url + "/unused"),
                ("08f8e0260c64418510cefb2b06eee5cd", server.url + "/missing"),
            ]
            text = self._manifest_text(
                "archive.bin", "6547436690a26a399603a7096e876a2d", rows, 6
            )
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            output = Path(directory) / "restored.bin"
            state = self._state_path(output, text)
            state.mkdir(parents=True)
            (state / "part_000000.bin").write_bytes(b"aaa")
            with self.assertRaises(self.ft.TransferError):
                self._pull(manifest_path, output)
            self.assertEqual([request["path"] for request in server.requests], ["/missing"])
            self.assertEqual((state / "part_000000.bin").read_bytes(), b"aaa")
            self.assertFalse(output.exists())

    def test_output_name_from_windows_legacy_path_is_safe(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            server.routes["/part"] = {"body": b"aaa"}
            text = (
                "HASH:47bce5c74f589f4867dbd57e9ca9f808\n"
                "NAME:C:\\source\\数据 file.bin\n"
                f"47bce5c74f589f4867dbd57e9ca9f808|{server.url}/part\n"
            )
            manifest_path = Path(directory) / "manifest.txt"
            manifest_path.write_text(text, encoding="utf-8", newline="")
            unrelated = Path(directory) / ".file-transfer" / "other-state"
            unrelated.mkdir(parents=True)
            (unrelated / "keep.bin").write_bytes(b"keep")
            previous = Path.cwd()
            try:
                os.chdir(directory)
                result = self._pull(manifest_path, None)
            finally:
                os.chdir(previous)
            expected = Path(directory) / "数据 file.bin"
            self.assertEqual(result, expected)
            self.assertEqual(expected.read_bytes(), b"aaa")
            self.assertEqual((unrelated / "keep.bin").read_bytes(), b"keep")
            self.assertFalse((Path(directory) / "source").exists())


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ft = load_module()

    def test_push_cli_prefers_flags_over_environment(self):
        with patch.object(self.ft, "push_file") as push:
            code = self.ft.main(
                [
                    "push",
                    "archive.bin",
                    "--url",
                    "https://command.test",
                    "--auth",
                    "command-token",
                ],
                {
                    "FILE_TRANSFER_URL": "https://environment.test",
                    "FILE_TRANSFER_AUTH": "environment-token",
                },
            )
        self.assertEqual(code, 0)
        arguments = push.call_args.kwargs
        self.assertEqual(arguments["url"], "https://command.test")
        self.assertEqual(arguments["auth"], "command-token")
        self.assertEqual(arguments["workers"], 4)
        self.assertEqual(arguments["expires"], 3600)
        self.assertEqual(arguments["chunk_size"], 90 * 1024 * 1024)
        self.assertEqual(arguments["retry_policy"].max_attempts, 3)

    def test_push_requires_url_but_allows_empty_auth(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            missing_code = self.ft.main(["push", "archive.bin"], {})
        self.assertEqual(missing_code, 2)
        self.assertIn("--url", errors.getvalue())
        with patch.object(self.ft, "push_file") as push:
            code = self.ft.main(
                ["push", "archive.bin", "--url", "https://upload.test"],
                {},
            )
        self.assertEqual(code, 0)
        self.assertIsNone(push.call_args.kwargs["auth"])

    def test_pull_defaults_manifest_and_output(self):
        with patch.object(self.ft, "pull_manifest", return_value=Path("archive.bin")) as pull:
            code = self.ft.main(["pull"], {})
        self.assertEqual(code, 0)
        arguments = pull.call_args.kwargs
        self.assertEqual(arguments["manifest_path"], Path("manifest.txt"))
        self.assertIsNone(arguments["output_path"])
        self.assertEqual(arguments["workers"], 4)

    def test_invalid_workers_returns_usage_error(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = self.ft.main(["pull", "--workers", "17"], {})
        self.assertEqual(code, 2)
        self.assertIn("1 to 16", errors.getvalue())

    def test_cli_passes_custom_transfer_options(self):
        with patch.object(self.ft, "push_file") as push:
            push_code = self.ft.main(
                [
                    "push",
                    "archive.bin",
                    "--url",
                    "https://upload.test",
                    "--manifest",
                    "custom.txt",
                    "--chunk-size",
                    "8M",
                    "--workers",
                    "8",
                    "--retries",
                    "4",
                    "--expires",
                    "7200",
                ],
                {},
            )
        self.assertEqual(push_code, 0)
        push_arguments = push.call_args.kwargs
        self.assertEqual(push_arguments["manifest_path"], Path("custom.txt"))
        self.assertEqual(push_arguments["chunk_size"], 8 * 1024 * 1024)
        self.assertEqual(push_arguments["workers"], 8)
        self.assertEqual(push_arguments["retry_policy"].max_attempts, 5)
        self.assertEqual(push_arguments["expires"], 7200)

        with patch.object(
            self.ft, "pull_manifest", return_value=Path("restored.bin")
        ) as pull:
            pull_code = self.ft.main(
                [
                    "pull",
                    "manifest.txt",
                    "--output",
                    "restored.bin",
                    "--workers",
                    "6",
                    "--retries",
                    "1",
                ],
                {},
            )
        self.assertEqual(pull_code, 0)
        pull_arguments = pull.call_args.kwargs
        self.assertEqual(pull_arguments["output_path"], Path("restored.bin"))
        self.assertEqual(pull_arguments["workers"], 6)
        self.assertEqual(pull_arguments["retry_policy"].max_attempts, 2)

    def test_transfer_error_returns_one_without_traceback_or_token(self):
        errors = io.StringIO()
        with patch.object(
            self.ft, "push_file", side_effect=self.ft.TransferError("safe failure")
        ), redirect_stderr(errors):
            code = self.ft.main(
                [
                    "push",
                    "archive.bin",
                    "--url",
                    "https://upload.test",
                    "--auth",
                    "super-secret-token",
                ],
                {},
            )
        self.assertEqual(code, 1)
        self.assertIn("safe failure", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertNotIn("super-secret-token", errors.getvalue())

    def test_cli_push_then_pull_round_trip(self):
        with tempfile.TemporaryDirectory() as directory, LocalTransferServer() as server:
            source = Path(directory) / "source data.bin"
            manifest = Path(directory) / "manifest.txt"
            destination = Path(directory) / "restored data.bin"
            source.write_bytes(b"abcdefghijkl")

            def upload(record, call_number):
                object_path = f"/object/{call_number}"
                with server.lock:
                    server.routes[object_path] = {"body": record["body"]}
                return {"body": f"{server.url}{object_path}\n".encode()}

            server.routes["/upload"] = upload
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                push_code = self.ft.main(
                    [
                        "push",
                        str(source),
                        "--url",
                        f"{server.url}/upload",
                        "--manifest",
                        str(manifest),
                        "--chunk-size",
                        "4",
                        "--workers",
                        "3",
                    ],
                    {},
                )
                pull_code = self.ft.main(
                    [
                        "pull",
                        str(manifest),
                        "--output",
                        str(destination),
                        "--workers",
                        "3",
                    ],
                    {},
                )

            self.assertEqual(push_code, 0)
            self.assertEqual(pull_code, 0)
            self.assertEqual(
                hashlib.md5(source.read_bytes()).hexdigest(),
                hashlib.md5(destination.read_bytes()).hexdigest(),
            )
            methods = [request["method"] for request in server.requests]
            self.assertGreaterEqual(methods.count("PUT"), 2)
            self.assertGreaterEqual(methods.count("GET"), 2)
