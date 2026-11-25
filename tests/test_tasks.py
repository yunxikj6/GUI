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

    def executable(self, name, source, inventory="print('[]')"):
        path = self.bin / name
        if name == "git":
            os.environ["CLOUD_WORKSPACE_GIT"] = str(path)
        if name == "codex":
            source = ("import sys\nif sys.argv[1:3] == ['mcp', 'list']:\n"
                      + "    " + inventory + "\n    sys.exit(0)\n" + source)
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
        self.assertFalse(result["task"]["canContinue"])
        self.assertIn("sample answer", [event["text"] for event in result["events"]])
        self.executable("codex", """
import sys
print('HTTP 401 Authorization: secret-example', file=sys.stderr)
print('websocket connection failed', file=sys.stderr)
sys.exit(1)
""")
        task = tasks.start_task(self.root, "model-error", "codex", "read project")
        result = self.terminal(task["id"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertIn("认证", result["events"][-1]["text"])
        self.assertNotIn("secret-example", json.dumps(result))

    def test_codex_inventory_failure_never_starts_model(self):
        marker = self.base / "model-started"
        self.executable("codex", f"open({str(marker)!r}, 'w').close()",
                        inventory="print('synthetic-private-descriptor')")
        result = self.terminal(tasks.start_task(self.root, "inventory", "codex", "read project")["id"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertIn("外部工具", result["events"][-1]["text"])
        self.assertNotIn("synthetic-private-descriptor", str(result))
        self.assertFalse(marker.exists())

    def test_codex_continuation_is_scoped_serial_and_idempotent(self):
        thread_id = "11111111-2222-4333-8444-555555555555"
        self.executable("codex", f"""
import json, sys
prompt = sys.stdin.read().strip()
args = sys.argv[1:]
assert args[:6] == ['-a', 'never', 'exec', '--sandbox', 'read-only', '--json']
assert args[6:12] == ['--disable', 'apps', '--disable', 'plugins', '--disable', 'hooks']
assert args[12:] == (['-'] if prompt == 'first' else ['resume', {thread_id!r}, '-'])
print(json.dumps({{'type': 'thread.started', 'thread_id': {thread_id!r}}}))
print(json.dumps({{'type': 'item.completed', 'item': {{'type':'agent_message', 'text': prompt}}}}))
print(json.dumps({{'type': 'turn.completed'}}))
""")
        first = self.terminal(tasks.start_task(self.root, "a", "codex", "first")["id"])["task"]
        self.assertIs(first["canContinue"], True)
        self.assertIsNone(first["parentTaskId"])
        other_root = self.base / "other"
        other_root.mkdir()
        with self.assertRaisesRegex(ValueError, "会话"):
            tasks.start_task(other_root, "b", "codex", "second", first["id"])
        with self.assertRaises(ValueError):
            tasks.start_task(self.root, "b", "codex", "second", thread_id)
        with self.assertRaises(ValueError):
            tasks.start_task(self.root, "b", "claude", "second", first["id"])
        def create(request):
            try:
                return tasks.start_task(self.root, request, "codex", "second", first["id"])
            except ValueError as error:
                self.assertIn("已经有追问", str(error))

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create, ["b1", "b2"] * 3))
        winners = [result for result in results if result]
        self.assertEqual(len(winners), 3)
        self.assertEqual(len({task["id"] for task in winners}), 1)
        second = self.terminal(winners[0]["id"])["task"]
        self.assertIs(second["canContinue"], True)
        self.assertEqual(second["parentTaskId"], first["id"])
        self.assertNotIn(thread_id, json.dumps(second))
        with self.assertRaisesRegex(ValueError, "requestId"):
            tasks.start_task(self.root, second["requestId"], "codex", "second")
        third = tasks.start_task(self.root, "c", "codex", "third", second["id"])
        self.assertIs(self.terminal(third["id"])["task"]["canContinue"], True)
        history = tasks.list_tasks(self.root)
        self.assertEqual([row["nextTaskId"] for row in history], [None, third["id"], second["id"]])
        self.assertEqual([row["canContinue"] for row in history], [True, False, False])

    def test_unconfirmed_or_failed_codex_resume_keeps_its_transcript(self):
        thread_id = "11111111-2222-4333-8444-555555555555"
        # Simulate persisted successful tasks, including one from before native sessions.
        with tasks._database(self.root) as db, db:
            for key in ("legacy", "missing", "mismatch"):
                db.execute("""
                    INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                    VALUES (?, ?, 'codex', 'first', 'succeeded', ?)
                """, (key, key, key))
            db.executemany("INSERT INTO codex_turn (taskId, threadId) VALUES (?, ?)",
                           [(key, thread_id) for key in ("missing", "mismatch")])
        self.assertIs(tasks.events(self.root, "legacy", 0)["task"]["canContinue"], False)
        with self.assertRaises(ValueError):
            tasks.start_task(self.root, "b", "codex", "follow", "legacy")
        self.executable("codex", """
import json, sys
if sys.stdin.read().strip() == 'missing':
    sys.exit('thread/resume failed: no rollout found for thread id (private detail)')
print(json.dumps({'type': 'thread.started', 'thread_id': 'aaaaaaaa-2222-4333-8444-555555555555'}))
print(json.dumps({'type': 'turn.completed'}))
""")
        for key, hint in (("missing", "未找到"), ("mismatch", "未确认")):
            task = tasks.start_task(self.root, "resume-" + key, "codex", key, key)
            result = self.terminal(task["id"])
            self.assertEqual(result["task"]["status"], "failed")
            self.assertIs(result["task"]["canContinue"], False)
            self.assertIn(hint, result["events"][-1]["text"])
            self.assertNotIn("private detail", json.dumps(result))
            self.assertEqual(tasks.events(self.root, key, 0)["task"]["nextTaskId"], task["id"])
            self.assertEqual(tasks.start_task(self.root, "resume-" + key, "codex", key, key)["id"],
                             task["id"])

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
        self.assertFalse(result["task"]["canContinue"])
        joined = "".join(e["text"] for e in result["events"] if e["kind"] in ("output", "delta"))
        self.assertEqual(joined, "hello")

    def test_claude_tool_progress_streams_once_per_call_and_keeps_only_tool_names(self):
        gate = self.base / "continue-tools"
        def tool(name, key):
            return {"type": "tool_use", "name": name, "id": key, "input": {"path": "private/path"}}
        def started(block):
            return {"type": "stream_event",
                    "event": {"type": "content_block_start", "index": 1, "content_block": block}}
        def assistant(blocks):
            return {"type": "assistant", "message": {"content": blocks}}
        read = tool("Read", "first")
        first = [
            {"type": "stream_event", "event": {"index": 0, "type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "先读取。"}}},
            started(read),
        ]
        tail = [
            {"type": "stream_event", "event": {"type": "content_block_delta",
             "delta": {"type": "input_json_delta", "partial_json": "private-query"}}},
            assistant([{"type": "text", "text": "先读取。"}, read, {"type": "text", "text": "再查找。"},
                       tool("Glob", "find")]),
            assistant([read]), started(tool("Grep", "search")), started(tool("Read", "second")),
            started(tool("Bash", "unavailable")), assistant([tool("Read", None)]),
            {**assistant([tool("Grep", "unexecuted")]), "error": "runtime error"},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "private-file-body"}]}},
            {"type": "result", "subtype": "success", "result": "先读取。再查找。"},
        ]
        self.executable("claude", f"""
import json, time
from pathlib import Path
for frame in {first!r}: print(json.dumps(frame), flush=True)
end = time.monotonic() + 6
while not Path({str(gate)!r}).exists() and time.monotonic() < end: time.sleep(0.02)
for frame in {tail!r}: print(json.dumps(frame))
""")
        task_id = tasks.start_task(self.root, "tools", "claude", "explain")["id"]
        def live():
            page = tasks.events(self.root, task_id, 0)
            return page if any(e["text"] == "Claude 正在使用 Read 读取文件。" for e in page["events"]) else None
        try:
            prefix = self.wait(live)
            self.assertEqual(prefix["task"]["status"], "running")
        finally:
            gate.touch()
        page = self.terminal(task_id)
        self.assertEqual(page["task"]["status"], "succeeded")
        progress = [e["text"] for e in page["events"] if e["kind"] == "status" and "Claude 正在" in e["text"]]
        self.assertEqual(progress, ["Claude 正在使用 Read 读取文件。", "Claude 正在使用 Glob 查找文件。",
                                    "Claude 正在使用 Grep 搜索文本。", "Claude 正在使用 Read 读取文件。"])
        self.assertEqual("".join(e["text"] for e in page["events"] if e["kind"] in ("output", "delta")),
                         "先读取。再查找。")
        self.assertNotIn("private", str(page))
        self.assertEqual(prefix["events"] + tasks.events(self.root, task_id, prefix["cursor"])["events"],
                         page["events"])

    def test_claude_without_safe_mode_requires_upgrade(self):
        calls = self.base / "calls"
        self.executable("claude", f"""
import sys
with open({str(calls)!r}, 'a') as stream: print('called', file=stream)
assert '--safe-mode' in sys.argv
sys.exit("error: unknown option '--safe-mode'")
""")
        result = self.terminal(tasks.start_task(self.root, "old", "claude", "read")["id"])
        self.assertEqual(result["task"]["status"], "failed")
        self.assertIn("升级", result["events"][-1]["text"])
        self.assertIn("--safe-mode", result["events"][-1]["text"])
        self.assertEqual(calls.read_text().splitlines(), ["called"])

    def test_claude_session_is_private_scoped_and_has_a_single_successor(self):
        session = self.base / "session"
        self.executable("claude", f"""
import sys, json, uuid
from pathlib import Path
saved = Path({str(session)!r})
prompt = sys.stdin.read().strip()
assert '--no-session-persistence' not in sys.argv
if prompt == 'first':
    assert '--resume' not in sys.argv
    sid = sys.argv[sys.argv.index('--session-id') + 1]
    assert str(uuid.UUID(sid)) == sid and not saved.exists()
    saved.write_text(sid)
else:
    assert '--session-id' not in sys.argv
    sid = sys.argv[sys.argv.index('--resume') + 1]
    assert sid == saved.read_text()
print(json.dumps({{'type':'system','subtype':'init','session_id':sid}}))
print(json.dumps({{'type':'result','subtype':'success','session_id':sid,'result':prompt}}))
""")
        first = self.terminal(tasks.start_task(self.root, "a", "claude", "first")["id"])["task"]
        self.assertTrue(first["canContinue"])
        self.assertNotIn(session.read_text(), json.dumps(first))
        with tasks._database(self.root) as db:
            # The previous worker's validation trusts canContinue without a provider check.
            self.assertFalse(db.execute("SELECT canContinue FROM task_history WHERE id=?",
                                        (first["id"],)).fetchone()[0])
        with self.assertRaisesRegex(ValueError, "相同的模型"):
            tasks.start_task(self.root, "cross", "codex", "next", first["id"])
        with self.assertRaises(ValueError):
            tasks.start_task(self.root, "raw", "claude", "next", session.read_text())
        other = self.base / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            tasks.start_task(other, "other", "claude", "next", first["id"])

        def follow(request):
            try:
                return tasks.start_task(self.root, request, "claude", "next", first["id"])["id"]
            except ValueError as exc:
                self.assertIn("已经有追问", str(exc))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(follow, ["follow1", "follow2"] * 2))
        self.assertEqual(ids.count(None), 2)
        self.assertEqual(len(set(ids)), 2)
        second = self.terminal(next(i for i in ids if i))["task"]
        self.assertEqual(second["parentTaskId"], first["id"])
        self.assertTrue(second["canContinue"])
        self.assertEqual(tasks.start_task(self.root, second["requestId"], "claude", "next",
                                          first["id"])["id"], second["id"])
        self.assertEqual(tasks.list_tasks(self.root)[1]["nextTaskId"], second["id"])
        with self.assertRaisesRegex(ValueError, "requestId"):
            tasks.start_task(self.root, second["requestId"], "claude", "next")

    def test_claude_refuses_missing_or_unconfirmed_native_history(self):
        thread = "11111111-2222-4333-8444-555555555555"
        cases = (("missing", "未找到"), ("unconfirmed", "未确认"), ("mismatch", "标识"))
        with tasks._database(self.root) as db, db:
            for key in ["legacy", *(key for key, _ in cases)]:
                db.execute("""
                    INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                    VALUES (?, ?, 'claude', 'first', 'succeeded', ?)
                """, (key, key, key))
            db.executemany("INSERT INTO claude_turn (taskId, threadId) VALUES (?, ?)",
                           ((key, thread) for key, _ in cases))
        self.assertFalse(tasks.events(self.root, "legacy", 0)["task"]["canContinue"])
        with self.assertRaises(ValueError):
            tasks.start_task(self.root, "try", "claude", "follow", "legacy")
        self.executable("claude", """
import json, sys
prompt = sys.stdin.read().strip()
sid = sys.argv[sys.argv.index('--resume') + 1]
if prompt == 'missing':
    print(json.dumps({'type':'result','subtype':'error_during_execution','session_id':'other',
                      'errors':['No conversation found with session ID: private-detail']}))
    sys.exit(1)
if prompt == 'mismatch':
    print(json.dumps({'type':'system','subtype':'init','session_id':'other'}))
print(json.dumps({'type':'result','subtype':'success','session_id':sid,'result':'wrong-transcript'}))
""")
        for key, hint in cases:
            result = self.terminal(tasks.start_task(self.root, "resume-" + key, "claude", key, key)["id"])
            self.assertEqual(result["task"]["status"], "failed")
            self.assertFalse(result["task"]["canContinue"])
            self.assertIn(hint, result["events"][-1]["text"])
            self.assertNotIn("private-detail", str(result))
            self.assertNotIn("wrong-transcript", str(result))

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
