import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from cloud import proposals, tasks, workspace


class FileProposalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.path = "notes.txt"
        self.file = self.root / self.path
        self.original = "项目说明\r\n旧内容"
        self.candidate = "项目说明\r\n更新 🟢\r\n"
        self.file.write_bytes(self.original.encode())
        self.revision = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                      "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        self.install()

    def install(self, body=None, inventory="print('[]')"):
        executable = self.bin / "codex"
        if body is None:
            body = f"""
assert Path.cwd() != Path({str(self.root)!r}) and not (Path.cwd() / '.git').exists()
assert '--ephemeral' in sys.argv and '--ignore-user-config' in sys.argv
assert sys.argv[sys.argv.index('--sandbox')+1] == 'read-only'
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
assert schema['required'] == ['content']
question = json.loads(sys.stdin.read().split('\u005cn', 1)[1])
assert question['original'] == {self.original!r} and question['path'] == {self.path!r}
out.write_text(json.dumps({{'content': {self.candidate!r}}}))
print(json.dumps({{'type': 'turn.completed'}}))
"""
        executable.write_text(f"#!{sys.executable}\nimport os, sys, json\nfrom pathlib import Path\n"
                              "if sys.argv[1:3] == ['mcp', 'list']:\n"
                              + textwrap.indent(inventory, "    ") + "\n    sys.exit(0)\n"
                              + "out = Path(sys.argv[sys.argv.index('-o') + 1])\n" + body)
        executable.chmod(0o700)

    def start(self, request="proposal", path=None, revision=None):
        return tasks.start_task(self.root, request, "proposal", "更新文案", path=path or self.path,
                                revision=revision or self.revision)["id"]

    def finished(self, task_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = tasks.events(self.root, task_id, 0)
            if result["task"]["status"] in ("succeeded", "failed"):
                return result
            time.sleep(0.02)
        self.fail("Timed out waiting for proposal")

    def pending(self):
        task_id = self.start()
        result = self.finished(task_id)
        self.assertEqual(result["task"]["status"], "succeeded", result)
        self.assertNotIn(self.original, str(result))
        self.assertNotIn(self.candidate, str(result))
        self.assertNotIn("更新 🟢", str(tasks.list_tasks(self.root)))
        return task_id

    def decide(self, task_id, action):
        return tasks.decide_proposal(self.root, task_id, action)

    def test_exact_preview_content_and_idempotent_apply(self):
        task_id = self.pending()
        self.assertEqual(self.file.read_bytes(), self.original.encode())
        preview = tasks.get_proposal(self.root, task_id)
        self.assertEqual(preview["decision"], "pending")
        self.assertIn("-旧内容\n\\ No newline at end of file\n+更新 🟢\r\n", preview["diff"])
        self.file.chmod(0o750)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.decide(task_id, "apply"), range(6)))
        self.assertTrue(all(item == {**preview, "decision": "applied"} for item in results))
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o750)
        self.assertEqual(self.file.read_bytes(), self.candidate.encode())
        self.file.write_text("later edit")
        self.assertEqual(self.decide(task_id, "apply")["decision"], "applied")
        self.assertEqual(self.file.read_text(), "later edit")
        self.assertEqual(self.start(), task_id)
        with self.assertRaises(ValueError):
            self.start(revision="a" * 64)
        with self.assertRaises(ValueError):
            self.start(path="other.txt")
        with self.assertRaisesRegex(workspace.WorkspaceError, "不能执行"):
            self.decide(task_id, "reject")

    def test_conflicts_and_rejection_leave_file_and_preview_intact(self):
        task_id = self.pending()
        before = tasks.get_proposal(self.root, task_id)
        # Even the exact candidate written by another actor conflicts before any approval.
        self.file.write_bytes(self.candidate.encode())
        with self.assertRaises(workspace.WorkspaceError) as error:
            self.decide(task_id, "apply")
        self.assertEqual(error.exception.code, "conflict")
        self.assertEqual(tasks.get_proposal(self.root, task_id), before)
        self.file.write_text("changed by someone else")
        for _ in range(2):
            self.assertEqual(self.decide(task_id, "reject"), {**before, "decision": "rejected"})
        with self.assertRaises(workspace.WorkspaceError):
            self.decide(task_id, "apply")
        self.assertEqual(self.file.read_text(), "changed by someone else")

    def test_restart_between_atomic_save_and_decision_commit(self):
        task_id = self.pending()
        original_save = workspace.save

        def interrupted(*args):
            original_save(*args)
            raise SystemExit("simulate worker exit after replace")

        with patch.object(proposals.workspace, "save", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.decide(task_id, "apply")
        with patch.object(proposals.workspace, "save", side_effect=AssertionError("double write")):
            self.assertEqual(tasks.get_proposal(self.root, task_id)["decision"], "applied")
            self.assertEqual(self.decide(task_id, "apply")["decision"], "applied")
        self.assertEqual(self.file.read_bytes(), self.candidate.encode())

    def test_parallel_apply_and_reject_agree_with_file(self):
        task_id = self.pending()
        def race(action):
            try:
                return self.decide(task_id, action)["decision"]
            except workspace.WorkspaceError as error:
                self.assertEqual(error.code, "proposal_decided")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(race, ["apply", "reject"] * 4))
        decision = tasks.get_proposal(self.root, task_id)["decision"]
        self.assertEqual({value for value in results if value}, {decision})
        expected = self.candidate if decision == "applied" else self.original
        self.assertEqual(self.file.read_bytes(), expected.encode())

    def test_malformed_failed_and_oversized_model_responses_never_change_file(self):
        for name, body in {
            "wrong": "out.write_text('private error instead of JSON')",
            "missing": "print(json.dumps({'content':'unauthorized stdout fallback'}))",
            "size": "out.write_text(json.dumps({'content':'🟢'*6200}))",
            "nul": "out.write_text(json.dumps({'content':chr(0)}))",
            "same": f"out.write_text(json.dumps({{'content':{self.original!r}}}))",
            "auth": "out.write_text(json.dumps({'content':'private candidate'}))\n"
                    "sys.exit('HTTP 401 private error')",
        }.items():
            with self.subTest(name=name):
                self.install(body + "\nprint(json.dumps({'type':'turn.completed'}))")
                task_id = self.start(request=name)
                result = self.finished(task_id)
                self.assertEqual(result["task"]["status"], "failed")
                if name == "auth":
                    self.assertIn("认证", result["events"][-1]["text"])
                self.assertNotIn("private", str(result))
                self.assertEqual(tasks.get_proposal(self.root, task_id)["decision"], "failed")
                with self.assertRaises(workspace.WorkspaceError):
                    self.decide(task_id, "apply")
        self.assertEqual(self.file.read_bytes(), self.original.encode())

    def test_only_existing_bounded_utf8_at_requested_revision_is_accepted(self):
        with self.assertRaises(workspace.WorkspaceError) as error:
            self.start(revision="a" * 64)
        self.assertEqual(error.exception.code, "conflict")
        outside = self.base / "outside"
        outside.write_text("private")
        self.file.unlink()
        self.file.symlink_to(outside)
        with self.assertRaises(OSError):
            self.start()
        self.file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.start()
        for content in (b"X" * (proposals.LIMIT + 1), b"x\0y", b"\x81"):
            self.file.write_bytes(content)
            with self.assertRaises((workspace.WorkspaceError, ValueError)):
                self.start(revision=hashlib.sha256(content).hexdigest())
        self.assertEqual(tasks.list_tasks(self.root), [])
        self.assertEqual(outside.read_text(), "private")

    def test_stalled_cli_times_out_without_writing(self):
        self.install("import time\ntime.sleep(20)")
        with tasks._database(self.root) as db, db:
            proposals.create(db, self.root, "timeout", self.path, self.revision)
            with patch.object(proposals, "_TIMEOUT", 0.1):
                success, message = proposals.generate(db, "timeout", "edit", tasks._error_hint)
            self.assertFalse(success)
            self.assertIn("超过", message)
            self.assertIsNone(db.execute("SELECT candidate FROM proposal").fetchone()[0])
        self.assertEqual(self.file.read_bytes(), self.original.encode())

    def test_system_mcp_is_disabled_for_proposal_without_mutating_login(self):
        user_home = self.base / "login"
        user_home.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(user_home)}):
            inventory = f"""
assert os.environ["CODEX_HOME"] != {str(user_home)!r}
overridden = 'mcp_servers={{"host.system"={{enabled=false}}}}' in sys.argv
print(json.dumps([{{"name":"host.system","enabled":not overridden,"transport":{{"env":"private config"}}}}]))
"""
            body = f"""
assert os.environ["CODEX_HOME"] == {str(user_home)!r}
assert 'mcp_servers={{"host.system"={{enabled=false}}}}' in sys.argv
out.write_text(json.dumps({{'content':'reviewed text'}}))
print(json.dumps({{'type':'turn.completed'}}))
"""
            self.install(body, inventory)
            result = self.finished(self.start())
        self.assertEqual(result["task"]["status"], "succeeded")
        self.assertNotIn("private config", str(result))
        self.assertEqual(self.file.read_bytes(), self.original.encode())

    def test_unverifiable_mcp_inventory_fails_before_sending_content_to_model(self):
        invoked = self.base / "model-called"
        for name, inventory in {
            "invalid": "print('private malformed config')",
            "unnamed": "print(json.dumps([{'enabled':True}]))",
            "name": "print(json.dumps([{'name':'x={enabled=true}', 'enabled':True}]))",
            "required": "print(json.dumps([{'name':'required', 'enabled':True}]))",
            "size": "print(' ' * 300000)",
            "exit": "sys.exit(2)",
        }.items():
            with self.subTest(name=name):
                self.install(f"Path({str(invoked)!r}).touch()", inventory)
                result = self.finished(self.start(request=name))
                self.assertEqual(result["task"]["status"], "failed")
                self.assertIn("外部工具", result["events"][-1]["text"])
                self.assertNotIn("private malformed config", str(result))
        self.assertFalse(invoked.exists())


if __name__ == "__main__":
    unittest.main()
