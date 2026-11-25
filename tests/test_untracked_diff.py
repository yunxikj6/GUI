"""Round-trip ordinary untracked files through the real worker and Git's patch reader."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"
GIT = shutil.which("git", path=os.defpath)


@unittest.skipUnless(sys.platform.startswith("linux") and GIT, "requires Linux and Git")
class UntrackedDiffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
                    "CLOUD_WORKSPACE_GIT": GIT, "CLOUD_WORKSPACE_ROOT": str(self.root),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "-q", "--template=")
        (self.root / ".gitattributes").write_text("*.txt filter=notice\n")
        self.git("add", "--", ".gitattributes")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                 "commit", "-qm", "attributes")
        self.git("config", "filter.notice.clean", f"touch {self.base / 'filter-ran'} && cat")

    def git(self, *args, cwd=None, data=None):
        return subprocess.run([GIT, *args], cwd=cwd or self.root, env=self.env, input=data,
                              capture_output=True, check=True, timeout=10)

    def diff(self, **fields):
        request = json.dumps({"op": "diff", **fields}).encode()
        reply = subprocess.run([sys.executable, str(WORKER), "--stdin"], cwd=self.base, env=self.env,
                               input=request, capture_output=True, check=True, timeout=15)
        envelope = json.loads(reply.stdout)
        self.assertTrue(envelope["ok"], envelope)
        return envelope["data"]

    def test_names_contents_and_execute_bits_survive_git_apply(self):
        files = {
            "lf.txt": (b"one\ntwo\n\n\n", 0o644),
            "crlf.txt": (b"one\r\ntwo\r\n", 0o444),
            "no-eol.txt": (b"one\ntwo", 0o644),
            "separators.txt": ("one\rtwo\vthree\u0085four\u2028five\u2029six\n".encode(), 0o644),
            "run.sh": (b"#!/bin/sh\ntrue\n", 0o555),
            "empty.exec": (b"", 0o555),
            "empty file": (b"", 0o644),
            'tab\t newline\n quote" slash\\ DEL\x7f': (b"control path\n", 0o644),
            "中文 ": (b"space at end of path\n", 0o644),
            os.fsdecode(b"raw-\xff.txt"): (b"raw filename\n", 0o644),
            "raw-?.txt": (b"question mark filename\n", 0o644),
        }
        for name, (content, mode) in files.items():
            path = self.root / name
            path.write_bytes(content)
            path.chmod(mode)
        index = self.root / ".git" / "index"
        before = index.read_bytes()
        unstaged = self.diff(scope="unstaged")
        patch = unstaged["diff"]
        self.assertFalse(unstaged["truncated"])
        self.assertEqual(self.diff(), {"diff": patch, "truncated": False})
        self.assertEqual(self.diff(scope="staged")["diff"], "")
        self.assertIn("+two\n\\ No newline at end of file\n", patch)
        self.assertIn('+++ "b/raw-\\377.txt"', patch)
        self.assertIn("+++ b/中文 \t\n", patch)
        self.assertFalse((self.base / "filter-ran").exists())
        self.assertEqual(index.read_bytes(), before)

        restored = self.base / "restored"
        restored.mkdir()
        self.git("apply", "--whitespace=nowarn", "-", cwd=restored, data=patch.encode())
        self.assertEqual(set(os.listdir(restored)), set(files))
        for name, (content, mode) in files.items():
            with self.subTest(name=name):
                path = restored / name
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(path.stat().st_mode & stat.S_IXUSR, mode & stat.S_IXUSR)

    def test_unavailable_files_and_response_budget(self):
        outside = self.base / "outside"
        outside.write_text("outside sentinel")
        (self.root / 'outside"\tlink').symlink_to(outside)
        (self.root / "binary").write_bytes(b"\x00")
        (self.root / "invalid").write_bytes(b"\xff")
        (self.root / "oversize").write_bytes(b"z" * (128 * 1024 + 1))
        private = self.root / ".cloudwork"
        private.mkdir()
        (private / "state").write_text("private sentinel")
        os.mkfifo(self.root / "pipe")
        patch = self.diff(scope="unstaged")["diff"]
        self.assertIn('Untracked file: "outside\\"\\tlink" (preview unavailable)', patch)
        for name in ("binary", "invalid", "oversize"):
            self.assertIn(f"Untracked file: {name} (preview unavailable)", patch)
        self.assertNotIn("sentinel", patch)
        self.assertNotIn(".cloudwork", patch)
        self.assertNotIn("new file mode", patch)

        (self.root / "many-lines").write_bytes(b"\n" * (128 * 1024))
        limited = self.diff(scope="unstaged")
        self.assertTrue(limited["truncated"])
        self.assertLessEqual(len(limited["diff"].encode()), 64 * 1024)
        self.assertIn("@@ -0,0 +1,131072 @@", limited["diff"])


if __name__ == "__main__":
    unittest.main()
