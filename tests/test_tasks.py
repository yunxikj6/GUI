import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cloud import tasks


class RemoteTaskTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.base = Path(sandbox.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.git = shutil.which("git")
        self.assertTrue(self.git, "Git is required for the project check tests")
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.base / "state"),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        env.start()
        self.addCleanup(env.stop)
        self.run_git("init", "--quiet")
        (self.root / "note.txt").write_text("initial\n")
        self.run_git("add", "note.txt")
        self.run_git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "commit", "--quiet", "-m", "Initial")

    def run_git(self, *args):
        subprocess.run([self.git, *args], cwd=self.root, capture_output=True, check=True)

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o700)

    def wait(self, callback):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            found = callback()
            if found:
                return found
            time.sleep(0.02)
        self.fail("Timed out waiting for task")

    def terminal(self, task_id):
        def finished():
            result = tasks.events(self.root, task_id, 0)
            return result if result["task"]["status"] in ("succeeded", "failed") else None
        return self.wait(finished)

    def test_concurrent_retry_and_replay_across_a_live_check(self):
        gate = self.base / "continue"
        waiting = self.base / "waiting"
        self.executable("git", f"""
import os, sys, time
from pathlib import Path
if 'ls-files' in sys.argv:
    Path({str(waiting)!r}).touch()
    end = time.monotonic() + 5
    while not Path({str(gate)!r}).exists() and time.monotonic() < end:
        time.sleep(0.02)
os.execv({self.git!r}, [{self.git!r}, *sys.argv[1:]])
""")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(tasks.start_task, self.root, "same", "check", "") for _ in range(8)]
            ids = {future.result()["id"] for future in futures}
        self.assertEqual(len(ids), 1)
        task_id = ids.pop()
        self.wait(waiting.exists)
        first = tasks.events(self.root, task_id, 0)
        self.assertEqual(first["task"]["status"], "running")
        self.assertTrue(first["events"])
        gate.touch()
        all_events = self.terminal(task_id)
        tail = tasks.events(self.root, task_id, first["cursor"])
        self.assertEqual(all_events["task"]["status"], "succeeded")
        self.assertEqual(all_events["events"], first["events"] + tail["events"])
        seqs = [event["seq"] for event in all_events["events"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(tasks.events(self.root, task_id, tail["cursor"])["events"], [])
        self.assertEqual(len(tasks.list_tasks(self.root)), 1)
        self.assertEqual(tasks.start_task(self.root, "same", "check", "")["id"], task_id)
        with self.assertRaises(ValueError):
            tasks.start_task(self.root, "same", "check", "different")
        self.assertEqual({p.name for p in self.root.iterdir()}, {".git", "note.txt"})

    def test_actual_git_failure(self):
        (self.root / "note.txt").write_text("trailing space \n")
        task = tasks.start_task(self.root, "bad", "check", "check workspace")
        result = self.terminal(task["id"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertTrue(any("git diff --check" in item["text"] for item in result["events"]))

    def test_large_logs_are_replayable_in_wire_sized_pages(self):
        message = ("行🟢\r\n" * 1450 + "\x01" * 20000 + "尾") * 12
        with tasks._database(self.root) as db, db:
            db.executemany("""
                INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                VALUES (?, ?, 'codex', ?, 'succeeded', ?)
            """, [(key, key, "\x03" * 32000, key) for key in ("chunked", "legacy")])
            tasks._append(db, "chunked", "output", message)
            longest = db.execute("SELECT max(length(text)) FROM event").fetchone()[0]
            self.assertLessEqual(longest, tasks._CHUNK_CHARS)
            db.execute("INSERT INTO event VALUES ('legacy', 1, 'output', ?)", (message,))
            for key in ("chunked", "legacy"):
                tasks._append(db, key, "status", "done")
        for task_id in ("chunked", "legacy"):
            with self.subTest(task_id=task_id):
                cursor = 0
                texts = []
                pages = 0
                while True:
                    page = tasks.events(self.root, task_id, cursor)
                    self.assertLessEqual(tasks._wire_size(page), tasks._PAGE_BYTES)
                    self.assertEqual(page["task"]["status"], "succeeded")
                    self.assertGreater(page["cursor"], cursor)
                    replay = tasks.events(self.root, task_id, cursor)
                    self.assertEqual(replay, page)
                    cursor = page["cursor"]
                    texts.extend(e["text"] for e in page["events"] if e["kind"] == "delta")
                    pages += 1
                    if not page["hasMore"]:
                        self.assertEqual(page["events"][-1]["text"], "done")
                        break
                self.assertGreater(pages, 1)
                self.assertEqual("".join(texts), message)
                self.assertFalse(tasks.events(self.root, task_id, cursor)["events"])
        # A client that had already consumed the original huge event skips it.
        self.assertEqual([e["text"] for e in tasks.events(self.root, "legacy", 1)["events"]], ["done"])

    def test_recent_list_uses_bounded_prompt_summaries(self):
        with tasks._database(self.root) as db, db:
            db.executemany("""
                INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                VALUES (?, ?, 'codex', ?, 'succeeded', ?)
            """, ((str(i), str(i), "文" * 10000, f"{i:03d}") for i in range(120)))
        recent = tasks.list_tasks(self.root)
        self.assertEqual(len(recent), 80)
        self.assertEqual(recent[0]["id"], "119")
        self.assertEqual(len(recent[0]["prompt"]), 160)
        self.assertEqual(len(tasks.events(self.root, "0", 0)["task"]["prompt"]), 10000)

    def test_codex_json_events_and_failed_auth_redaction(self):
        self.executable("codex", """
import json, sys
assert sys.stdin.read().strip() == 'read project'
assert '--sandbox' in sys.argv and 'read-only' in sys.argv
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'sample answer'}}))
print(json.dumps({'type':'turn.completed'}))
""")
        task = tasks.start_task(self.root, "model", "codex", "read project")
        result = self.terminal(task["id"])
        self.assertEqual(result["task"]["status"], "succeeded")
        self.assertIn("sample answer", [event["text"] for event in result["events"]])
        self.executable("codex", """
import sys
print('HTTP 401 Authorization: secret-example', file=sys.stderr)
sys.exit(1)
""")
        task = tasks.start_task(self.root, "model-error", "codex", "read project")
        result = self.terminal(task["id"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertIn("认证", result["events"][-1]["text"])
        self.assertNotIn("secret-example", json.dumps(result))

    def test_claude_stream_does_not_repeat_messages(self):
        responses = [
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "hel"}}},
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "lo"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "hello"},
        ]
        self.executable("claude", "import json, sys\nassert '--strict-mcp-config' in sys.argv\n"
                        + f"for event in {responses!r}: print(json.dumps(event))\n")
        task = tasks.start_task(self.root, "claude", "claude", "read project")
        result = self.terminal(task["id"])
        self.assertEqual(result["task"]["status"], "succeeded")
        joined = "".join(e["text"] for e in result["events"] if e["kind"] in ("output", "delta"))
        self.assertEqual(joined, "hello")

    def test_stale_process_does_not_reexecute(self):
        # Sleep inside this test's provider subprocess; the worker leads its private group.
        self.executable("codex", "import time\ntime.sleep(20)\n")
        task = tasks.start_task(self.root, "stale", "codex", "read project")
        self.wait(lambda: tasks.events(self.root, task["id"], 0)["task"]["status"] == "running")
        with tasks._database(self.root) as db:
            pid = db.execute("SELECT pid FROM task WHERE id=?", (task["id"],)).fetchone()[0]
        os.killpg(pid, signal.SIGKILL)
        result = self.terminal(task["id"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertEqual(tasks.start_task(self.root, "stale", "codex", "read project")["id"], task["id"])
        self.assertEqual(tasks.events(self.root, task["id"], result["cursor"])["events"], [])


if __name__ == "__main__":
    unittest.main()
