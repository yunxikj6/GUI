"""Real Claude Code CLI with synthetic auth and a local Anthropic Messages endpoint."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
import uuid

WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"
CLAUDE = shutil.which("claude")


class MessagesHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if urlsplit(self.path).path != "/v1/messages":
            self.send_error(404)
            return
        if hasattr(self.server, "expected_key") and self.headers.get("x-api-key") != self.server.expected_key:
            self.send_error(401)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.requests.append(request)
            if not self.server.answers:
                self.send_error(400, "Unexpected extra model request")
                return
            number = len(self.server.requests)
            reply = self.server.answers.pop(0)
        if isinstance(reply, str):
            block = {"type": "text", "text": reply}
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": reply}
            stop = "end_turn"
        else:
            block = reply
            start = dict(block, input={})
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            stop = "tool_use"
        message = {"id": f"msg_local_{number}", "type": "message", "role": "assistant",
                   "model": request.get("model", "fixture"), "content": [block],
                   "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}
        if request.get("stream"):
            events = [
                {"type": "message_start", "message": dict(message, content=[], stop_reason=None)},
                {"type": "content_block_start", "index": 0, "content_block": start},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                 "usage": {"output_tokens": 5}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            mime = "text/event-stream"
        else:
            body = json.dumps(message).encode()
            mime = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(CLAUDE, "Native Claude Code CLI unavailable")
class NativeClaudeFixture(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        base = Path(work.name)
        self.root, self.config = base / "project", base / "claude-profile"
        test_home, bindir = base / "home", base / "bin"
        for directory in (self.config, test_home, bindir):
            directory.mkdir()
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), MessagesHandler)
        self.model.lock = threading.Lock()
        self.model.requests, self.model.answers = [], []
        thread = threading.Thread(target=self.model.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.model.shutdown)

        # Observe the adapter's *explicit* UUID choices without changing the CLI arguments.
        self.invocations = base / "turns.jsonl"
        shim = bindir / "claude"
        shim.write_text(f"#!{sys.executable}\nimport json, os, sys\nargs = sys.argv[1:]\n"
                        "turn = {flag: args[args.index(flag)+1] for flag in ('--session-id','--resume') if flag in args}\n"
                        "if '-p' in args or '--print' in args:\n"
                        f"    with open({str(self.invocations)!r}, 'a') as seen: seen.write(json.dumps(turn)+'\\n')\n"
                        f"os.execv({CLAUDE!r}, [{CLAUDE!r}, *args])\n")
        shim.chmod(0o700)
        self.env = {"PATH": str(bindir) + os.pathsep + os.environ["PATH"], "HOME": str(test_home),
                    "CLAUDE_CONFIG_DIR": str(self.config), "XDG_STATE_HOME": str(base / "state"),
                    "CLOUD_WORKSPACE_ROOT": str(self.root), "LANG": "C.UTF-8",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.model.server_port}",
                    "ANTHROPIC_API_KEY": "synthetic-loopback-only", "API_TIMEOUT_MS": "6000",
                    "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "127.0.0.1"}
        self.rpc("init")

    def rpc(self, op, **fields):
        proc = subprocess.run([sys.executable, str(WORKER), "--stdin"], env=self.env,
                              input=json.dumps({"op": op, **fields}).encode(), capture_output=True, check=True, timeout=10)
        envelope = json.loads(proc.stdout)
        self.assertTrue(envelope["ok"], envelope)
        return envelope["data"]

    def cli(self, args, prompt):
        return subprocess.run([CLAUDE, *args], cwd=self.root, env=self.env, input=prompt.encode(),
                              capture_output=True, timeout=20)

    def finished(self, task):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            page = self.rpc("events", taskId=task["id"])
            if page["task"]["status"] not in ("queued", "running", "cancelling"):
                self.assertEqual(page["task"]["status"], "succeeded", page["events"])
                return page
            time.sleep(.08)
        self.fail("Claude did not finish its loopback request")


class NativeClaudeTests(NativeClaudeFixture):
    def assert_context_and_tools(self, first_question, first_answer, second_question):
        self.assertEqual(len(self.model.requests), 2)
        for request in self.model.requests:
            self.assertEqual({tool["name"] for tool in request.get("tools", [])}, {"Read", "Glob", "Grep"})
        conversation = [message for message in self.model.requests[1]["messages"] if message["role"] != "system"]
        for role, marker in (("user", first_question), ("assistant", first_answer), ("user", second_question)):
            texts = [json.dumps(message["content"]) for message in conversation if message["role"] == role]
            self.assertEqual(sum(value.count(marker) for value in texts), 1, marker)

    def test_native_cli_emits_requested_session_id_and_fails_closed_on_missing_resume(self):
        session = str(uuid.uuid4())
        args = ["-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
                "--permission-mode", "dontAsk", "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                "--safe-mode", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        self.model.answers.extend(["native-answer-alpha", "native-answer-beta"])
        for flag, prompt, answer in (("--session-id", "native-question-alpha", "native-answer-alpha"),
                                     ("--resume", "native-question-beta", "native-answer-beta")):
            result = self.cli([*args, flag, session], prompt)
            self.assertEqual(result.returncode, 0)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            init, = [r for r in records if r["type"] == "system" and r["subtype"] == "init"]
            end, = [r for r in records if r["type"] == "result"]
            self.assertEqual(init["session_id"], session)
            self.assertEqual((end["session_id"], end["subtype"], end["is_error"], end["result"]),
                             (session, "success", False, answer))
        missing = self.cli([*args, "--resume", str(uuid.uuid4())], "must-not-be-sent")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(b"No conversation found with session ID", missing.stdout + missing.stderr)
        self.assert_context_and_tools("native-question-alpha", "native-answer-alpha", "native-question-beta")

    def test_workbench_resumes_once_and_replays_streamed_reply(self):
        self.model.answers.extend(["workbench-answer-one", "workbench-answer-two"])
        initial = dict(provider="claude", requestId="claude-first", prompt="workbench-question-one")
        first = self.rpc("start", **initial)
        first_page = self.finished(first)
        self.assertTrue(first_page["task"]["canContinue"], first_page["task"])
        follow = dict(provider="claude", requestId="claude-second", prompt="workbench-question-two",
                      parentTaskId=first["id"])
        second = self.rpc("start", **follow)
        self.assertEqual(self.rpc("start", **follow)["id"], second["id"])
        second_page = self.finished(second)
        self.assertTrue(second_page["task"]["canContinue"])
        self.assertEqual(second_page["task"]["parentTaskId"], first["id"])
        self.assertEqual(self.rpc("start", **initial)["nextTaskId"], second["id"])
        self.assertEqual(self.rpc("start", **follow)["id"], second["id"])
        for page, expected in ((first_page, "workbench-answer-one"), (second_page, "workbench-answer-two")):
            entries = page["events"]
            answer = "".join(e["text"] for e in entries if e["kind"] in ("output", "delta"))
            self.assertEqual(answer, expected)
            self.assertFalse(page["hasMore"])
            replay = self.rpc("events", taskId=page["task"]["id"])
            self.assertEqual(replay["events"], entries)
            self.assertEqual(replay["cursor"], page["cursor"])
            self.assertEqual(self.rpc("events", taskId=page["task"]["id"], after=page["cursor"])["events"], [])

        turns = [json.loads(line) for line in self.invocations.read_text().splitlines()]
        self.assertEqual(len(turns), 2)
        session = str(uuid.UUID(turns[0]["--session-id"]))
        self.assertEqual(turns[1], {"--resume": session})
        self.assert_context_and_tools("workbench-question-one", "workbench-answer-one", "workbench-question-two")

    def test_follow_up_reads_project_file_and_replays_one_tool_notice(self):
        path = self.root / "read-acceptance.txt"
        marker = uuid.uuid4().hex
        path.write_text(f"File-only acceptance marker: {marker}\n", encoding="utf-8")
        read = {"type": "tool_use", "id": "toolu_local_read_one", "name": "Read", "input": {"file_path": str(path)}}
        first_answer = "Ready to inspect the project."
        final_answer = "The project file has been read."
        self.model.answers.extend([first_answer, read, final_answer])
        initial = self.rpc("start", provider="claude", requestId="read-initial", prompt="Prepare a read-only review.")
        first_page = self.finished(initial)
        follow = dict(provider="claude", requestId="read-follow", parentTaskId=initial["id"],
                      prompt=f"Read {path.name} and confirm completion.")
        second = self.rpc("start", **follow)
        page = self.finished(second)
        self.assertEqual(self.rpc("start", **follow)["id"], second["id"])
        self.assertEqual(page["task"]["parentTaskId"], initial["id"])
        self.assertTrue(page["task"]["canContinue"])
        self.assertEqual(len(self.model.requests), 3)
        self.assertNotIn(marker, json.dumps(self.model.requests[:2]))
        messages = self.model.requests[2]["messages"]
        self.assertIn(first_answer, json.dumps(messages))
        self.assertIn(follow["prompt"], json.dumps(messages))
        results = [block for msg in messages if msg["role"] == "user" and isinstance(msg["content"], list)
                   for block in msg["content"] if block.get("type") == "tool_result" and block["tool_use_id"] == read["id"]]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].get("is_error", False))
        self.assertIn(marker, json.dumps(results[0]["content"]))

        entries = page["events"]
        notices = [e for e in entries if e["kind"] == "status" and e["text"] == "Claude 正在使用 Read 读取文件。"]
        self.assertEqual(len(notices), 1)
        self.assertEqual("".join(e["text"] for e in entries if e["kind"] in ("delta", "output")), final_answer)
        self.assertNotIn(marker, json.dumps(entries))
        self.assertNotIn(str(path), json.dumps(entries))
        self.assertEqual(self.rpc("events", taskId=initial["id"])["events"], first_page["events"])
        self.assertEqual(self.rpc("events", taskId=second["id"]), page)
        tail = self.rpc("events", taskId=second["id"], after=notices[0]["seq"])
        self.assertEqual(tail["events"], [e for e in entries if e["seq"] > notices[0]["seq"]])


if __name__ == "__main__":
    unittest.main()
