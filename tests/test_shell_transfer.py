"""Real HTTP integration for Shell clients; Python is needed only to run tests."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_file_transfer import LocalTransferServer, ROOT, load_module


def find_bash():
    configured = os.environ.get("FILE_TRANSFER_TEST_BASH")
    if configured:
        return configured
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        return None  # Do not accidentally select the WSL launcher.
    return shutil.which("bash")


@unittest.skipUnless(find_bash(), "Bash unavailable; set FILE_TRANSFER_TEST_BASH")
class ShellHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.server = LocalTransferServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.ft = load_module()
        self.server.routes["/upload"] = self.upload

    def upload(self, record, call):
        path = f"/chunk/{call}?signed=a&b=c"
        self.server.routes[path] = {"body": record["body"]}
        return {"body": f"Download: {self.server.url}{path}\n".encode()}

    def run_shell(self, script, *args, ok=True, env_extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FILE_TRANSFER_")}
        env.update(FILE_TRANSFER_URL=self.server.url + "/upload",
                   FILE_TRANSFER_RETRIES="0", FILE_TRANSFER_AUTH="secret")
        env.update(env_extra or {})
        env = {key: value for key, value in env.items() if value is not None}
        command = [find_bash(), "-c", 'export PATH="/usr/bin:$PATH"; exec bash "$@"',
                   "shell-test", (ROOT / script).as_posix(), *map(str, args)]
        result = subprocess.run(command, cwd=self.directory, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stderr)
        return result

    def write_manifest(self, data=b"hello", url=None, digest=None):
        self.server.routes["/data"] = {"body": data}
        md5 = hashlib.md5(data).hexdigest()
        text = f"HASH:{digest or md5}\nNAME:restored\n{md5}|{url or self.server.url + '/data'}\n"
        (self.directory / "manifest.txt").write_text(text)

    def test_shell_upload_is_readable_by_python_and_shell(self):
        data = b"helloworld" * 4
        (self.directory / "source file").write_bytes(data)
        self.run_shell("file-push.sh", "source file", "--chunk-size", "7")
        manifest = self.directory / "manifest.txt"
        parsed = self.ft.parse_manifest(manifest.read_text())
        self.assertEqual(parsed.size, 40)
        self.assertEqual(len(parsed.chunks), 6)
        self.ft.pull_manifest(manifest, self.directory / "python-out", "secret", 2,
                              self.ft.RetryPolicy(1), lambda *args: None)
        self.run_shell("file-pull.sh", "manifest.txt", "--output", "shell-out")
        self.assertEqual((self.directory / "python-out").read_bytes(), data)
        self.assertEqual((self.directory / "shell-out").read_bytes(), data)
        puts = [r for r in self.server.requests if r["method"] == "PUT"]
        self.assertEqual(b"".join(r["body"] for r in puts), data)
        self.assertTrue(all(r["headers"]["x-expiration-seconds"] == "3600" for r in puts))

    def test_shell_reads_python_upload_manifest(self):
        source = self.directory / "python-source"
        source.write_bytes(b"abcdefghijk")
        self.ft.push_file(source, self.server.url + "/upload", None,
                          self.directory / "manifest.txt", 4, 1, 3600,
                          self.ft.RetryPolicy(1), lambda *args: None)
        self.run_shell("file-pull.sh", "--output", "shell-out")
        self.assertEqual((self.directory / "shell-out").read_bytes(), b"abcdefghijk")

    def test_auth_failure_is_not_retried_and_manifest_survives(self):
        self.server.routes["/upload"] = {"status": 401}
        (self.directory / "source").write_bytes(b"hello")
        (self.directory / "manifest.txt").write_text("previous")
        self.run_shell("file-push.sh", "source", "--retries", "3", ok=False)
        self.assertEqual(self.server.route_calls["/upload"], 1)
        self.assertEqual((self.directory / "manifest.txt").read_text(), "previous")

    def test_transient_http_failure_retries(self):
        self.server.routes["/upload"] = lambda record, call: (
            {"status": 503} if call == 1 else self.upload(record, call))
        (self.directory / "source").write_bytes(b"hello")
        self.run_shell("file-push.sh", "source", "--retries", "1")
        self.assertEqual(self.server.route_calls["/upload"], 2)

    def test_corrupt_download_retries_and_restores(self):
        self.write_manifest()
        self.server.routes["/data"] = lambda record, call: {
            "body": b"wrong" if call == 1 else b"hello"}
        self.run_shell("file-pull.sh", "--retries", "1")
        self.assertEqual(self.server.route_calls["/data"], 2)
        self.assertEqual((self.directory / "restored").read_bytes(), b"hello")

    def test_cross_origin_redirect_drops_auth(self):
        with LocalTransferServer() as other:
            other.routes["/data"] = {"body": b"hello"}
            self.server.routes["/old"] = {"status": 302, "headers": {"Location": other.url + "/data"}}
            self.write_manifest(url=self.server.url + "/old")
            self.run_shell("file-pull.sh")
            self.assertEqual((self.directory / "restored").read_bytes(), b"hello")
            self.assertNotIn("authorization", other.requests[0]["headers"])
            self.assertEqual(self.server.requests[0]["headers"]["authorization"], "secret")

    def test_upload_redirect_does_not_publish_manifest(self):
        self.server.routes["/upload"] = {"status": 302, "headers": {"Location": "/other"}}
        (self.directory / "source").write_bytes(b"hello")
        self.run_shell("file-push.sh", "source", ok=False)
        self.assertFalse((self.directory / "manifest.txt").exists())
        self.assertEqual(len(self.server.requests), 1)

    def test_resume_redownloads_corrupt_cached_chunk(self):
        self.write_manifest(digest="0" * 32)
        (self.directory / "restored").write_bytes(b"previous")
        self.run_shell("file-pull.sh", ok=False)
        cached = list((self.directory / ".file-transfer").glob("shell-*/p_*"))
        self.assertEqual(len(cached), 1)
        cached[0].write_bytes(b"bad")
        self.run_shell("file-pull.sh", ok=False)
        self.assertEqual(self.server.route_calls["/data"], 2)
        self.assertEqual(cached[0].read_bytes(), b"hello")
        self.assertEqual((self.directory / "restored").read_bytes(), b"previous")

    def test_legacy_env_and_one_time_expiration(self):
        (self.directory / "source").write_bytes(b"hello")
        self.run_shell("file-push.sh", "source", "--expires", "0",
                       env_extra={"FILE_TRANSFER_URL": None, "FILE_TRANSFER_AUTH": None,
                                  "url": self.server.url + "/upload", "auth": "legacy-secret"})
        request = self.server.requests[0]
        self.assertEqual(request["headers"]["authorization"], "legacy-secret")
        self.assertNotIn("x-expiration-seconds", request["headers"])

    def test_download_lock_prevents_network_and_is_preserved(self):
        self.write_manifest(digest="0" * 32)
        self.run_shell("file-pull.sh", ok=False)
        state = next((self.directory / ".file-transfer").glob("shell-*"))
        lock = state / "lock"
        lock.mkdir()
        requests = len(self.server.requests)
        self.run_shell("file-pull.sh", ok=False)
        self.assertEqual(len(self.server.requests), requests)
        self.assertTrue(lock.is_dir())

    def test_same_origin_relative_redirect_preserves_auth(self):
        self.server.routes["/old"] = {"status": 307, "headers": {"Location": "/data"}}
        self.write_manifest(url=self.server.url + "/old")
        self.run_shell("file-pull.sh")
        self.assertEqual((self.directory / "restored").read_bytes(), b"hello")
        self.assertEqual(self.server.requests[-1]["headers"]["authorization"], "secret")

    def test_urls_with_literal_glob_characters_make_one_request(self):
        url = self.server.url + "/data?token={one,two}&range=[1-2]"
        self.write_manifest(url=url)
        self.server.routes["/data?token={one,two}&range=[1-2]"] = {"body": b"hello"}
        self.run_shell("file-pull.sh")
        self.assertEqual([r["path"] for r in self.server.requests],
                         ["/data?token={one,two}&range=[1-2]"])
        self.assertEqual((self.directory / "restored").read_bytes(), b"hello")


if __name__ == "__main__":
    unittest.main()
