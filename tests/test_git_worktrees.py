"""Git root boundaries and linked working trees through the deployed worker protocol."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("git"), "requires Linux and Git")
class GitWorktreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.main = self.base / "主仓"
        self.main.mkdir()
        self.env = {
            "PATH": os.defpath, "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "XDG_STATE_HOME": str(self.base / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.git(self.main, "init", "-q", "--initial-branch=main", "--template=")
        (self.main / "work.txt").write_text("committed\n", encoding="utf-8")
        (self.main / "sub").mkdir()
        (self.main / "sub" / "file.txt").write_text("nested baseline\n", encoding="utf-8")
        self.git(self.main, "add", "--", ".")
        self.git(self.main, "-c", "user.name=Worktree Acceptance", "-c", "user.email=fixture@localhost",
                 "-c", "commit.gpgSign=false", "commit", "-qm", "common base")

    def git(self, folder, *args, binary="git"):
        return subprocess.run([binary, *args], cwd=folder, env=self.env, capture_output=True,
                              check=True, timeout=12).stdout

    def request(self, root, op, **fields):
        body = json.dumps({"op": op, **fields}, ensure_ascii=False).encode()
        process = subprocess.run([sys.executable, str(WORKER), "--stdin"], input=body, cwd=self.base,
                                 env={**self.env, "CLOUD_WORKSPACE_ROOT": str(root)}, capture_output=True,
                                 check=True, timeout=15)
        return json.loads(process.stdout)

    def data(self, root, op, **fields):
        reply = self.request(root, op, **fields)
        self.assertTrue(reply["ok"], reply)
        return reply["data"]

    def check(self, root, expected):
        task = self.data(root, "start", requestId=uuid.uuid4().hex, provider="check", prompt="")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            record = self.data(root, "events", taskId=task["id"], after=0)
            if record["task"]["status"] not in ("queued", "running"):
                self.assertEqual(record["task"]["status"], expected, record["events"])
                return "\n".join(event["text"] for event in record["events"])
            time.sleep(.04)
        self.fail("Git check did not finish")

    def assert_branch(self, root, branch):
        status = self.data(root, "status")
        self.assertEqual((status["gitState"], status["branch"]), ("ready", branch))
        self.assertTrue(status["providers"]["check"]["available"])

    def test_main_linked_and_detached_checkout_only_use_their_own_index(self):
        linked = self.base / "功能 分支"
        detached = self.base / "分离 HEAD"
        self.git(self.main, "worktree", "add", "-q", "-b", "feature", str(linked))
        self.git(linked, "worktree", "add", "-q", "--detach", str(detached))
        cases = ((self.main, "main", "main-only"), (linked, "feature", "feature-only"),
                 (detached, self.git(detached, "rev-parse", "--short", "HEAD").decode().strip(), "detached-only"))
        for count, (root, branch, tag) in enumerate(cases, 1):
            # The three indexes now contain different numbers of visible tracked files.
            for n in range(count):
                name = f"index-{n}.txt"
                (root / name).write_text(f"staged {tag}\n", encoding="utf-8")
                self.git(root, "add", "--", name)
            (root / "work.txt").write_text(f"unstaged {tag}\n", encoding="utf-8")
            (root / "untracked.txt").write_text(f"untracked {tag}\n", encoding="utf-8")
            private = root / "sub" / ".cloudwork"
            private.mkdir()
            (private / "state.txt").write_text("private text with trailing spaces   \n", encoding="utf-8")
            self.git(root, "add", "-f", "--", "sub/.cloudwork/state.txt")

        for count, (root, branch, tag) in enumerate(cases, 1):
            with self.subTest(root=root.name):
                self.assert_branch(root, branch)
                patch = self.data(root, "diff")
                self.assertFalse(patch["truncated"])
                for prefix in ("staged", "unstaged", "untracked"):
                    self.assertIn(f"+{prefix} {tag}", patch["diff"])
                self.assertNotIn("private text", patch["diff"])
                for _, _, other in cases:
                    if other != tag:
                        self.assertNotIn(other, patch["diff"])
                self.assertNotIn(".git", [entry["name"] for entry in self.data(root, "tree", path="")["entries"]])
                self.assertEqual(self.request(root, "file", path=".git")["error"]["code"], "invalid_path")
                self.assertEqual(self.request(root, "save", path=".git", revision="0" * 64,
                                              content="fixture")["error"]["code"], "invalid_path")
                log = self.check(root, "succeeded")
                self.assertIn(f"{2 + count} 个文件条目", log)

        # A staged whitespace error in one checkout and an unstaged error in the other
        # must report different failing sides while the main checkout stays healthy.
        (linked / "index-0.txt").write_text("staged error   \n", encoding="utf-8")
        self.git(linked, "add", "--", "index-0.txt")
        (detached / "work.txt").write_text("unstaged error   \n", encoding="utf-8")
        staged = self.check(linked, "failed")
        self.assertIn("暂存区检查未通过", staged)
        self.assertIn("工作区检查通过", staged)
        unstaged = self.check(detached, "failed")
        self.assertIn("工作区检查未通过", unstaged)
        self.assertIn("暂存区检查通过", unstaged)
        self.check(self.main, "succeeded")

    def test_worktree_from_bare_repository(self):
        bare = self.base / "storage.git"
        linked = self.base / "bare checkout"
        self.git(self.main, "clone", "-q", "--bare", str(self.main), str(bare))
        self.git(bare, "worktree", "add", "-q", "-b", "from-bare", str(linked))
        self.assert_branch(linked, "from-bare")
        (linked / "work.txt").write_text("change from bare checkout\n", encoding="utf-8")
        self.assertIn("+change from bare checkout", self.data(linked, "diff")["diff"])
        self.check(linked, "succeeded")

    def test_native_relative_worktree_with_configured_git_on_narrow_path(self):
        system = shutil.which("git", path=os.defpath)
        candidates = dict.fromkeys([os.environ.get("CLOUD_WORKSPACE_GIT"),
                                    shutil.which("git", path="/usr/local/bin"), system])
        supported = []
        for candidate in candidates:
            if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
                help_text = subprocess.run([candidate, "worktree", "add", "-h"], cwd=self.main, env=self.env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8).stdout
                if b"relative-paths" in help_text:
                    supported.append(candidate)
        if not supported:
            self.skipTest("no installed Git supports worktree add --relative-paths")

        native = supported[0]
        linked = self.base / "relative checkout"
        self.git(self.main, "worktree", "add", "--relative-paths", "-q", "-b", "relative", str(linked), binary=native)
        self.assertFalse(os.path.isabs((linked / ".git").read_bytes().strip().removeprefix(b"gitdir: ")))
        (linked / "work.txt").write_text("relative working change\n", encoding="utf-8")
        trace = self.base / "selected-git.log"
        selected = self.base / "selected-git"
        selected.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"with open({str(trace)!r}, 'a', encoding='utf-8') as log: print(' '.join(sys.argv[1:]), file=log)\n"
            f"os.execv({native!r}, [{native!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        selected.chmod(0o700)
        self.env["CLOUD_WORKSPACE_GIT"] = str(selected)
        self.env["PATH"] = str(self.base / "no-git-on-this-path")
        self.assert_branch(linked, "relative")
        self.assertTrue(trace.stat().st_size)
        trace.write_text("")
        self.assertIn("+relative working change", self.data(linked, "diff")["diff"])
        self.assertTrue(trace.stat().st_size)
        trace.write_text("")
        self.check(linked, "succeeded")
        commands = trace.read_text(encoding="utf-8")
        self.assertIn("ls-files", commands)
        self.assertIn("--check", commands)

        if system and system not in supported:
            self.env["CLOUD_WORKSPACE_GIT"] = system
            self.assert_git_error(linked)
            self.assertIn("升级", self.data(linked, "status")["gitMessage"])

    def test_git_move_and_manual_move_then_git_repair(self):
        linked = self.base / "original"
        moved = self.base / "registered move"
        unregistered = self.base / "manual move"
        self.git(self.main, "worktree", "add", "-q", "-b", "relocate", str(linked))
        (linked / "work.txt").write_text("working copy moved with its changes\n", encoding="utf-8")
        self.git(self.main, "worktree", "move", str(linked), str(moved))
        self.assert_branch(moved, "relocate")
        self.assertIn("+working copy moved", self.data(moved, "diff")["diff"])
        moved.rename(unregistered)
        self.assert_git_error(unregistered)
        self.git(self.main, "worktree", "repair", str(unregistered))
        self.assert_branch(unregistered, "relocate")
        self.assertIn("+working copy moved", self.data(unregistered, "diff")["diff"])
        self.check(unregistered, "succeeded")

    def assert_git_error(self, root):
        status = self.data(root, "status")
        self.assertEqual((status["gitState"], status["branch"]), ("error", ""))
        self.assertTrue(status["gitMessage"])
        self.assertFalse(status["providers"]["check"]["available"])
        self.assertEqual(self.request(root, "diff")["error"]["code"], "git_error")
        self.assertEqual(self.data(root, "file", path="work.txt")["kind"], "text")
        self.check(root, "failed")

    def test_no_marker_and_damaged_metadata_keep_files_available(self):
        standalone = self.base / "plain"
        standalone.mkdir()
        (standalone / "file.txt").write_text("standalone baseline\n", encoding="utf-8")
        for root in (self.main / "sub", standalone):
            with self.subTest(root=root.name):
                status = self.data(root, "status")
                self.assertEqual((status["gitState"], status["branch"]), ("none", ""))
                self.assertFalse(status["providers"]["check"]["available"])
                self.assertEqual(self.data(root, "diff")["diff"], "")
                self.assertIn("baseline", self.data(root, "file", path="file.txt")["content"])
                self.check(root, "failed")

        self.git(standalone, "init", "-q", "--initial-branch=unborn")
        self.git(standalone, "add", "--", "file.txt")
        self.assert_branch(standalone, "unborn")
        self.assertIn("+standalone baseline", self.data(standalone, "diff")["diff"])
        self.check(standalone, "succeeded")

        damaged = self.base / "damaged pointer"
        damaged.mkdir()
        (damaged / "work.txt").write_text("browse this file\n", encoding="utf-8")
        (damaged / ".git").write_text("gitdir: no-such-metadata\n", encoding="utf-8")
        self.assert_git_error(damaged)
        # A valid symbolic branch name can outlive the commit object it references.
        oid = self.git(self.main, "rev-parse", "HEAD").decode().strip()
        (self.main / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
        self.assert_git_error(self.main)
        # Real .git directory is still present, but no longer identifies a usable repository.
        (self.main / ".git" / "HEAD").unlink()
        self.assert_git_error(self.main)
