import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("git"), "requires Linux and Git")
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.root = self.directory / "project"
        self.env = {**os.environ, "CLOUD_WORKSPACE_ROOT": str(self.root),
                    "XDG_STATE_HOME": str(self.directory / "state"), "PYTHONDONTWRITEBYTECODE": "1"}

    def request(self, op, *, stdin=True, **parameters):
        body = json.dumps({"op": op, **parameters}, ensure_ascii=False).encode()
        arg = "--stdin" if stdin else base64.urlsafe_b64encode(body).decode().rstrip("=")
        run = subprocess.run([sys.executable, str(SCRIPT), arg], input=body if stdin else b"",
                             env=self.env, capture_output=True, check=True, timeout=15)
        self.assertEqual(run.stderr, b"")
        self.assertEqual(len(run.stdout.splitlines()), 1)
        return json.loads(run.stdout)

    def data(self, op, **parameters):
        response = self.request(op, **parameters)
        self.assertTrue(response["ok"], response)
        return response["data"]

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)

    def test_initialization_preserves_files_and_reads_do_not_seed(self):
        self.assertEqual(self.request("status")["error"]["code"], "not_found")
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        (self.root / "README.md").write_text("original", encoding="utf-8")
        self.assertFalse(self.data("init")["created"])
        self.assertEqual([p.name for p in self.root.iterdir()], ["README.md"])
        self.assertEqual(self.data("file", path="README.md", stdin=False)["content"], "original")

    def test_seed_status_and_clean_diff(self):
        self.assertTrue(self.data("init")["created"])
        self.assertFalse(self.data("init")["created"])
        status = self.data("status")
        self.assertEqual(status["branch"], "main")
        self.assertIn("check", status["providers"])
        self.assertEqual(self.data("diff"), {"diff": "", "truncated": False})
        self.assertEqual(self.data("file", path="count_lines.py")["kind"], "text")

    def test_file_revalidation_only_sends_changed_content(self):
        self.root.mkdir()
        cases = (("memo.txt", b"one", b"two"), ("image.gif", b"GIF89a1", b"GIF89a2"))
        for name, before, after in cases:
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(before)
                old = self.data("file", path=name)
                cached = self.data("file", path=name, ifRevision=old["revision"])
                self.assertEqual(cached, {"path": name, "revision": old["revision"], "unchanged": True})
                path.write_bytes(after)
                fresh = self.data("file", path=name, ifRevision=old["revision"])
                self.assertEqual(fresh, self.data("file", path=name))
                self.assertNotEqual(fresh["content"], old["content"])

    def test_paths_links_special_files_and_preview_limit(self):
        self.root.mkdir()
        (self.directory / "private").write_text("outside", encoding="utf-8")
        (self.root / "link").symlink_to(self.directory / "private")
        (self.root / "back").symlink_to(self.directory)
        for name in [".git", ".cloudwork"]:
            (self.root / name).mkdir()
        os.mkfifo(self.root / "pipe")
        for path in ["/etc/passwd", "../private", ".git/config", ".cloudwork/tasks", "back/private", "link", "pipe"]:
            with self.subTest(path=path):
                self.assertFalse(self.request("file", path=path)["ok"])
        self.assertEqual(self.data("tree")["entries"], [])
        (self.root / "large.txt").write_bytes(b"a" * (128 * 1024 + 1))
        self.assertEqual(self.request("file", path="large.txt")["error"]["code"], "too_large")
        png = b"\x89PNG\r\n\x1a\n\x00IS"
        (self.root / "test.png").write_bytes(png)
        image = self.data("file", path="test.png")
        self.assertEqual(image["mime"], "image/png")
        self.assertEqual(base64.b64decode(image["content"]), png)

    def test_diff_tracks_new_existing_and_staged_files_excluding_private(self):
        self.data("init")
        (self.root / "README.md").write_text("改动\n", encoding="utf-8")
        self.git("add", "README.md")
        (self.root / "new.txt").write_text("新文件\n", encoding="utf-8")
        nested = self.root / "folder" / ".cloudwork"
        nested.mkdir(parents=True)
        (nested / "private").write_text("private marker\n", encoding="utf-8")
        self.git("add", "-f", "folder/.cloudwork/private")
        patch = self.data("diff")
        self.assertFalse(patch["truncated"])
        self.assertIn("+改动", patch["diff"])
        self.assertIn("+新文件", patch["diff"])
        self.assertNotIn("private", patch["diff"])
        (self.root / "new.txt").write_bytes(b"x" * (80 * 1024))
        self.assertTrue(self.data("diff")["truncated"])

    def test_external_git_helpers_never_run(self):
        self.data("init")
        (self.root / ".gitattributes").write_text("README.md diff=custom filter=custom\n", encoding="utf-8")
        self.git("config", "diff.custom.command", "touch helper-ran")
        self.git("config", "filter.custom.clean", "touch helper-ran; cat")
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        self.assertIn("+changed", self.data("diff")["diff"])
        self.assertFalse((self.root / "helper-ran").exists())

    def test_concurrent_saves_conflict_without_overwriting(self):
        self.root.mkdir()
        target = self.root / "run.py"
        target.write_text("original\n", encoding="utf-8")
        target.chmod(0o750)
        revision = self.data("file", path="run.py")["revision"]
        def attempt(value):
            return self.request("save", path="run.py", revision=revision, content=value)
        with ThreadPoolExecutor(2) as executor:
            responses = list(executor.map(attempt, ["first\n", "second\n"]))
        self.assertEqual(sum(r["ok"] for r in responses), 1)
        self.assertEqual(next(r for r in responses if not r["ok"])["error"]["code"], "conflict")
        saved = next(r["data"] for r in responses if r["ok"])
        self.assertEqual(saved["revision"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)
        self.assertEqual([entry.name for entry in self.root.iterdir()], ["run.py"])
        self.assertEqual(self.request("save", path="run.py", revision=saved["revision"],
                                      content="a" * (128 * 1024 + 1))["error"]["code"], "too_large")
        self.assertFalse(self.request("save", path="../private", content="text", revision=revision)["ok"])

    def test_full_size_unicode_stdin_save_and_roundtrip(self):
        self.root.mkdir()
        (self.root / "payload").write_bytes(b"")
        content = "样" * (128 * 1024 // 3)
        self.data("save", path="payload", revision=hashlib.sha256(b"").hexdigest(), content=content)
        self.assertEqual(self.data("file", path="payload")["content"], content)
