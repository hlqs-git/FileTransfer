from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
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


class LocalTransferServer:
    def __init__(self):
        self.routes = {}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def _record(self, body=b""):
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": body,
                    }
                )

            def _respond(self):
                route = owner.routes.get(self.path, {})
                self.send_response(route.get("status", 200))
                for key, value in route.get("headers", {}).items():
                    self.send_header(key, value)
                body = route.get("body", b"")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record()
                self._respond()

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", "0"))
                self._record(self.rfile.read(length))
                self._respond()

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
