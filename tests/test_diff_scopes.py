"""Compare the worker's staged and unstaged views with native Git in real checkouts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"
GIT = shutil.which("git", path=os.defpath)
LIMIT = 64 * 1024


@unittest.skipUnless(sys.platform.startswith("linux") and GIT, "requires Linux and Git")
class DiffScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "main"
        self.root.mkdir()
        self.env = {
            "PATH": os.defpath, "LC_ALL": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1", "CLOUD_WORKSPACE_GIT": GIT, "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.git("init", "-q", "--initial-branch=main", "--template=")

    def git(self, *args, root=None):
        return subprocess.run([GIT, "-c", "core.quotePath=false", *args], cwd=root or self.root,
                              env=self.env, capture_output=True, check=True, timeout=12).stdout

    def write(self, name, text, root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self):
        self.git("-c", "user.name=Diff Acceptance", "-c", "user.email=diff@localhost",
                 "-c", "commit.gpgSign=false", "commit", "-qm", "fixture baseline")

    def rpc(self, root=None, op="diff", **fields):
        result = subprocess.run([sys.executable, str(WORKER), "--stdin"], input=json.dumps({"op": op, **fields}).encode(),
                                cwd=self.base, env={**self.env, "CLOUD_WORKSPACE_ROOT": str(root or self.root)},
                                capture_output=True, check=True, timeout=15)
        return json.loads(result.stdout)

    def data(self, root=None, **fields):
        response = self.rpc(root, **fields)
        self.assertTrue(response["ok"], response.get("error"))
        return response["data"]

    def native(self, *revisions, paths=(".",), root=None):
        return self.git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                        "--ignore-submodules=all", *revisions, "--", *paths, root=root)

    def test_cancelled_net_change_and_partial_staging_match_both_native_patches(self):
        for name in ("cancel.txt", "partial.txt", "index.txt", "working.txt"):
            self.write(name, "base\n")
        self.git("add", "--", ".")
        self.commit()
        self.write("cancel.txt", "only in the index\n")
        self.write("partial.txt", "staged line\n")
        self.write("index.txt", "staged separately\n")
        self.git("add", "--", "cancel.txt", "partial.txt", "index.txt")
        self.write("cancel.txt", "base\n")
        self.write("partial.txt", "staged line\nextra working line\n")
        self.write("working.txt", "unstaged separately\n")
        self.assertEqual(self.git("status", "--porcelain=v1", "--", "cancel.txt"), b"MM cancel.txt\n")

        staged = self.data(scope="staged")
        unstaged = self.data(scope="unstaged")
        self.assertEqual(staged, {"scope": "staged", "diff": self.native("--cached").decode(), "truncated": False})
        self.assertEqual(unstaged, {"scope": "unstaged", "diff": self.native().decode(), "truncated": False})
        self.assertIn("+only in the index", staged["diff"])
        self.assertIn("-only in the index", unstaged["diff"])
        legacy = self.data()
        self.assertEqual(legacy, {"diff": self.native("HEAD").decode(), "truncated": False})
        self.assertNotIn("cancel.txt", legacy["diff"])

    def test_unborn_repository_and_untracked_empty_file(self):
        self.write("first.txt", "initial index\n")
        self.git("add", "--", "first.txt")
        self.write("first.txt", "edited before first commit\n")
        self.write("empty.txt", "")
        self.write("loose.txt", "未跟踪文本\n")
        staged = self.data(scope="staged")
        self.assertEqual(staged, {"scope": "staged", "diff": self.native("--cached").decode(), "truncated": False})
        unstaged = self.data(scope="unstaged")
        self.assertFalse(unstaged["truncated"])
        self.assertTrue(unstaged["diff"].startswith(self.native().decode()))
        self.assertIn("diff --git a/empty.txt b/empty.txt\nnew file mode", unstaged["diff"])
        self.assertIn("+未跟踪文本", unstaged["diff"])
        self.assertNotIn("loose.txt", staged["diff"])
        legacy = self.data()
        self.assertIn("+edited before first commit", legacy["diff"])
        self.assertIn("diff --git a/empty.txt b/empty.txt", legacy["diff"])
        self.assertIn("+未跟踪文本", legacy["diff"])

    def test_linked_checkout_uses_its_index_and_excludes_private_paths_in_each_scope(self):
        self.write("visible.txt", "committed\n")
        self.git("add", "--", ".")
        self.commit()
        linked = self.base / "linked tree"
        self.git("worktree", "add", "-q", "-b", "review", str(linked))
        for root, marker in ((self.root, "main"), (linked, "linked")):
            self.write("visible.txt", marker + " index\n", root)
            self.write("sub/.cloudwork/private.txt", "private stage\n", root)
            self.git("add", "-f", "--", "visible.txt", "sub/.cloudwork/private.txt", root=root)
            self.write("visible.txt", marker + " working\n", root)
            self.write("sub/.cloudwork/private.txt", "private working\n", root)
            self.write(".cloudwork-save-pending", "private untracked\n", root)
        for scope, revisions in (("staged", ("--cached",)), ("unstaged", ())):
            with self.subTest(scope=scope):
                patch = self.data(linked, scope=scope)
                self.assertFalse(patch["truncated"])
                self.assertEqual(patch["diff"], self.native(*revisions, paths=("visible.txt",), root=linked).decode())
        legacy = self.data(linked)
        self.assertEqual(legacy["diff"], self.native("HEAD", paths=("visible.txt",), root=linked).decode())

    def test_each_scope_keeps_its_own_utf8_byte_budget(self):
        self.write("large.txt", "before\n")
        self.write("small.txt", "before\n")
        self.git("add", "--", ".")
        self.commit()
        # Put the byte limit inside a UTF-8 code point for this Git's actual header length.
        for padding in ("", "x", "xx"):
            self.write("large.txt", padding + "界" * 25000 + "\n")
            self.git("add", "--", "large.txt")
            native = self.native("--cached")
            try:
                native[:LIMIT].decode("utf-8")
            except UnicodeDecodeError:
                break
        else:
            self.fail("fixture did not split a UTF-8 character at the transport boundary")
        self.write("small.txt", "small working change\n")
        large = self.data(scope="staged")
        self.assertTrue(large["truncated"])
        self.assertEqual(large["diff"], native[:LIMIT].decode("utf-8", "ignore"))
        self.assertEqual(self.data(scope="unstaged"),
                         {"scope": "unstaged", "truncated": False, "diff": self.native().decode()})

        self.commit()
        self.git("add", "--", "small.txt")
        # These invalid bytes are text to Git; substitution expands them in the UTF-8 response.
        self.write("large.txt", "short base\n")
        self.git("add", "--", "large.txt")
        self.commit()
        self.write("small.txt", "small staged change\n")
        self.git("add", "--", "small.txt")
        (self.root / "large.txt").write_bytes(b"replacement chars: " + b"\xff" * 25000 + b"\n")
        native = self.native()
        self.assertLess(len(native), LIMIT)
        self.assertGreater(len(native.decode("utf-8", "replace").encode()), LIMIT)
        large = self.data(scope="unstaged")
        self.assertTrue(large["truncated"])
        self.assertIn("\ufffd", large["diff"])
        self.assertLessEqual(len(large["diff"].encode()), LIMIT)
        self.assertTrue(native.decode("utf-8", "replace").startswith(large["diff"]))
        self.assertEqual(self.data(scope="staged"),
                         {"scope": "staged", "truncated": False, "diff": self.native("--cached").decode()})

    def test_raw_byte_names_remain_distinct_in_tracked_patches(self):
        names = (os.fsdecode(b"name-\xff"), "name-\ufffd")
        for name in names:
            self.write(name, "original\n")
        self.git("add", "--", ".")
        self.commit()
        for name in names:
            self.write(name, "index\n")
        self.git("add", "--", ".")
        for name in names:
            self.write(name, "working\n")
        for scope, command in (("staged", ["--cached"]), ("unstaged", [])):
            patch = self.data(scope=scope)["diff"]
            self.assertEqual(patch.encode(), self.git("-c", "core.quotePath=true", "diff", *command))
            self.assertIn(r'name-\377', patch)
            self.assertIn(r'name-\357\277\275', patch)

    def test_non_repository_and_invalid_scopes(self):
        plain = self.base / "plain"
        plain.mkdir()
        damaged = self.base / "damaged"
        self.write(".git", "invalid metadata\n", damaged)
        for root, expected_state in ((self.root, "ready"), (plain, "none"), (damaged, "error")):
            with self.subTest(state=expected_state):
                status = self.data(root, op="status")
                self.assertEqual(status["gitState"], expected_state)
                self.assertEqual(status["diffScopes"], ["staged", "unstaged"])
        for scope in ("staged", "unstaged"):
            self.assertEqual(self.data(plain, scope=scope),
                             {"scope": scope, "diff": "", "truncated": False, "gitState": "none"})
            self.assertEqual(self.rpc(damaged, scope=scope)["error"]["code"], "git_error")
        self.assertEqual(self.data(plain), {"diff": "", "truncated": False, "gitState": "none"})
        for invalid in ("", "combined", None, True, [], {}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.rpc(scope=invalid)["error"]["code"], "invalid_request")
