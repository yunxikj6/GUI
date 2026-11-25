import concurrent.futures
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from cloud import tasks, workspace


class TaskCancellationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                      "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        self.file = self.root / "note.txt"
        self.file.write_text("original\n", encoding="utf-8")

    def cli(self, name, body, inventory="print('[]')"):
        file = self.bin / name
        file.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport os, sys, json, signal, subprocess, time\n"
                        "if sys.argv[1:3] == ['mcp', 'list']:\n" + textwrap.indent(inventory, "    ")
                        + "\n    sys.exit(0)\n" + body)
        file.chmod(0o700)

    def bootstrap_worker(self, setup):
        worker_dir = str(Path(tasks.__file__).resolve().parent)
        bootstrap = (f"import sys; from pathlib import Path; sys.path.insert(0,{worker_dir!r})\n"
                     f"import tasks\n{setup}\ntasks._worker(Path(sys.argv[1]),sys.argv[2])")
        launch = subprocess.Popen
        def custom(args, **kwargs):
            return launch([sys.executable, "-c", bootstrap, *args[-2:]], **kwargs)
        return patch.object(tasks.subprocess, "Popen", side_effect=custom)

    def wait(self, fn):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(0.025)
        self.fail("Timed out waiting for process or task state")

    def terminal(self, task_id):
        def page():
            data = tasks.events(self.root, task_id, 0)
            return data if data["task"]["finishedAt"] else None
        return self.wait(page)

    def helper(self, marker):
        # Leave the task's process group but deliberately keep its stdout/stderr open.
        helper = ("import os, signal, time; from pathlib import Path; "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)")
        return f"""
subprocess.Popen([sys.executable, '-c', {helper!r}], start_new_session=True)
marker = Path({str(marker)!r})
while not marker.exists(): time.sleep(0.01)
"""

    def test_cancel_all_providers_reaps_stubborn_descendants_and_keeps_history(self):
        for provider, binary in (("check", "git"), ("codex", "codex"), ("claude", "claude"),
                                 ("proposal", "codex")):
            with self.subTest(provider=provider):
                marker = self.base / provider
                self.cli(binary, self.helper(marker) +
                         "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'before'}}), flush=True)\n"
                         "time.sleep(30)")
                target = ({"path": self.file.name, "revision": hashlib.sha256(self.file.read_bytes()).hexdigest()}
                          if provider == "proposal" else {})
                task = tasks.start_task(self.root, provider, provider, "run", **target)
                self.wait(lambda: marker.exists() and marker.stat().st_size)
                pid = int(marker.read_text())
                page = tasks.events(self.root, task["id"], 0)
                self.assertTrue(page["task"]["canCancel"])
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    responses = list(pool.map(lambda _: tasks.cancel_task(self.root, task["id"]), range(6)))
                self.assertTrue(all(t["status"] in ("cancelling", "cancelled") for t in responses))
                final = self.terminal(task["id"])
                self.assertEqual(final["task"]["status"], "cancelled")
                self.assertIs(final["task"]["canCancel"], False)
                self.assertFalse(Path(f"/proc/{pid}").exists(), "detached helper survived cancellation")
                self.assertEqual(final["events"][:len(page["events"])], page["events"])
                self.assertEqual(sum("已请求取消" in e["text"] for e in final["events"]), 1)
                self.assertEqual(final["events"][-1]["text"], "云端任务已取消。")
                self.assertEqual(tasks.cancel_task(self.root, task["id"]), final["task"])
                self.assertEqual(tasks.events(self.root, task["id"], 0), final)
                self.assertEqual(tasks.start_task(self.root, provider, provider, "run", **target), final["task"])
                if provider == "proposal":
                    self.assertEqual(tasks.get_proposal(self.root, task["id"])["decision"], "cancelled")
                    with self.assertRaises(workspace.WorkspaceError):
                        tasks.decide_proposal(self.root, task["id"], "apply")
                    self.assertEqual(self.file.read_text(), "original\n")

    def test_normal_completion_also_reaps_background_helpers(self):
        marker = self.base / "detached"
        self.cli("codex", self.helper(marker) + """
answer = '🟢回复' * 8192
line = json.dumps({'type':'item.completed','item':{'type':'agent_message','text':answer}}, ensure_ascii=False).encode()
cut = line.index('🟢'.encode()) + 1
os.write(1, line[:cut])
time.sleep(0.03)
sys.stdout.buffer.write(line[cut:] + b'\\r\\n{"type":"turn.completed"}')
""")
        task = tasks.start_task(self.root, "quick", "codex", "read")
        page = self.terminal(task["id"])
        self.assertEqual(page["task"]["status"], "succeeded", page["events"])
        self.assertEqual("".join(e["text"] for e in page["events"] if e["kind"] == "delta"), "🟢回复" * 8192)
        self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())
        self.assertEqual(tasks.cancel_task(self.root, task["id"]), page["task"])

    def test_exit_without_terminal_event_keeps_trailing_diagnostic(self):
        self.cli("codex", self.helper(self.base / "diagnostic-child") +
                 "sys.stderr.write('HTTP 401 unauthorized private-detail')")
        page = self.terminal(tasks.start_task(self.root, "missing", "codex", "run")["id"])
        self.assertEqual(page["task"]["status"], "failed")
        self.assertIn("认证", page["events"][-1]["text"])
        self.assertNotIn("private-detail", str(page))

    def test_git_and_proposal_finish_despite_inherited_output_pipes(self):
        git_child = self.base / "git-child"
        self.cli("git", "if 'ls-files' not in sys.argv: sys.exit(0)\n" + self.helper(git_child)
                 + "sys.stdout.buffer.write(b'a\\0b\\0' * 20000)")
        page = self.terminal(tasks.start_task(self.root, "files", "check", "")["id"])
        self.assertEqual(page["task"]["status"], "succeeded")
        self.assertIn("Git 索引包含 40000 个文件条目。", [e["text"] for e in page["events"]])

        inventory_child, model_child = self.base / "inventory-child", self.base / "model-child"
        self.cli("codex", self.helper(model_child) +
                 "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'content':'edited'}))\n"
                 "sys.stdout.write(json.dumps({'type':'turn.completed'}))",
                 inventory=self.helper(inventory_child) + "sys.stdout.write('[]')")
        task = tasks.start_task(self.root, "edit", "proposal", "edit", path=self.file.name,
                                revision=hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(self.terminal(task["id"])["task"]["status"], "succeeded")
        self.assertEqual(tasks.get_proposal(self.root, task["id"])["decision"], "pending")
        for marker in (git_child, inventory_child, model_child):
            self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())

    def test_queued_cancel_never_starts_provider(self):
        # Pause the supervisor before its first SQLite call to exercise a real queued task.
        marker = self.base / "provider-started"
        self.cli("git", f"Path({str(marker)!r}).touch()\n")
        gate = self.base / "allow-supervisor"
        script = ("import os, sys, time; from pathlib import Path; "
                  f"g=Path({str(gate)!r}); "
                  "exec('while not g.exists(): time.sleep(0.01)'); "
                  "os.execv(sys.argv[1],sys.argv[1:])")
        launch = subprocess.Popen
        def gated(args, **kwargs):
            return launch([sys.executable, "-c", script, *args], **kwargs)
        with patch.object(tasks.subprocess, "Popen", side_effect=gated):
            task = tasks.start_task(self.root, "queued", "check", "")
        try:
            self.assertEqual(task["status"], "queued")
            self.assertEqual(tasks.cancel_task(self.root, task["id"])["status"], "cancelling")
        finally:
            gate.touch()
        self.assertEqual(self.terminal(task["id"])["task"]["status"], "cancelled")
        self.assertFalse(marker.exists())

    def test_legacy_and_reused_or_missing_supervisor_are_never_signalled(self):
        with tasks._database(self.root) as db, db:
            for key, status, identity in (("legacy", "running", tasks._process_key(os.getpid())),
                                          ("lost", "cancelling", "different-process-start")):
                db.execute("""
                    INSERT INTO task(id,requestId,provider,prompt,status,createdAt,pid,processKey)
                    VALUES (?,?, 'check','',?, 'now',?,?)
                """, (key, key, status, os.getpid(), identity))
            db.execute("INSERT INTO task_control(taskId) VALUES ('lost')")
        with patch.object(tasks.os, "kill", side_effect=AssertionError("RPC must not send process signals")):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                tasks.cancel_task(self.root, "legacy")
            self.assertEqual(caught.exception.code, "cancel_unsupported")
            self.assertFalse(tasks.events(self.root, "legacy", 0)["task"]["canCancel"])
            self.assertEqual(tasks.cancel_task(self.root, "lost")["status"], "failed")
            self.assertIn("无法确认", tasks.events(self.root, "lost", 0)["events"][-1]["text"])

    def test_host_without_subreaper_keeps_original_execution_path(self):
        gate = self.base / "finish"
        self.cli("git", f"while not Path({str(gate)!r}).exists(): time.sleep(0.02)\n")
        with self.bootstrap_worker("tasks._enable_subreaper = lambda: False"):
            task = tasks.start_task(self.root, "unsupported", "check", "")
        try:
            self.wait(lambda: tasks.events(self.root, task["id"], 0)["task"]["status"] == "running")
            self.assertFalse(tasks.events(self.root, task["id"], 0)["task"]["canCancel"])
            with self.assertRaises(workspace.WorkspaceError) as error:
                tasks.cancel_task(self.root, task["id"])
            self.assertEqual(error.exception.code, "cancel_unsupported")
        finally:
            gate.touch()
        self.assertEqual(self.terminal(task["id"])["task"]["status"], "succeeded")

    def test_forked_executor_starts_without_sqlite_file_descriptors(self):
        marker = self.base / "fork-state"
        setup = f"""
execute = tasks._execute
def observed(root, task_id, supervised):
    assert supervised
    paths = [fd.resolve() for fd in Path('/proc/self/fd').iterdir()]
    Path({str(marker)!r}).write_text(str(any('tasks.sqlite' in path.name for path in paths)))
    execute(root, task_id, supervised)
tasks._execute = observed
"""
        self.cli("git", "sys.exit(0)")
        with self.bootstrap_worker(setup):
            task = tasks.start_task(self.root, "fork", "check", "")
        self.assertEqual(self.terminal(task["id"])["task"]["status"], "succeeded")
        self.assertEqual(marker.read_text(), "False")


if __name__ == "__main__":
    unittest.main()
