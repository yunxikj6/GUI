"""Exercise installed Codex, the workbench RPC and SQLite against a local model fixture."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"
CODEX = shutil.which("codex")


class ModelResponse(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        if hasattr(self.server, "fixture_token") and self.headers.get("Authorization") != "Bearer " + self.server.fixture_token:
            self.send_error(401, "Fixture credential required")
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        if not self.server.answers:
            self.send_error(400, "Unexpected extra model request")
            return
        text = self.server.answers.pop(0)
        index = len(self.server.requests)
        message = {"type": "message", "role": "assistant", "id": f"msg_{index}", "status": "completed",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]}
        response = {"id": f"resp_{index}", "object": "response", "status": "completed", "output": [message],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
        events = [
            {"type": "response.created", "response": dict(response, status="in_progress", output=[])},
            {"type": "response.output_item.added", "output_index": 0, "item": dict(message, status="in_progress", content=[])},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": message["id"], "delta": text},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": response},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if index == 1 and hasattr(self.server, "release_first"):
            # The CLI has received an actual response item, but the model turn is still open.
            prefix, completed, tail = body.partition(b"event: response.completed\n")
            self.wfile.write(prefix)
            self.wfile.flush()
            self.server.first_open.set()
            if not self.server.release_first.wait(20):
                return
            try:
                self.wfile.write(completed + tail)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.wfile.write(body)


@unittest.skipUnless(CODEX, "Native Codex CLI unavailable")
class NativeCodexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.root = base / "project"
        self.cli_home = base / "codex-state"
        self.cli_home.mkdir()
        test_home = base / "home"
        test_home.mkdir()

        self.model = HTTPServer(("127.0.0.1", 0), ModelResponse)
        self.model.requests = []
        self.model.answers = []
        thread = threading.Thread(target=self.model.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.model.shutdown)

        # Keep the endpoint pinned even for CLI modes that intentionally ignore user config.
        # This shim adds only provider arguments. The installed binary handles every turn.
        provider = ('{ name="Fixture", wire_api="responses", request_max_retries=0, stream_max_retries=0, '
                    f'base_url="http://127.0.0.1:{self.model.server_port}/v1", supports_websockets=false }}')
        provider_args = ["-c", 'model="gpt-5.4"', "-c", 'model_provider="fixture"',
                         "-c", "model_providers.fixture=" + provider]
        bindir = base / "bin"
        bindir.mkdir()
        shim = bindir / "codex"
        self.exec_pids = base / "last-exec-pids"
        # Codex 0.160 ignores global provider overrides before exec with --ignore-user-config.
        shim.write_text(f"#!{sys.executable}\nimport os, sys\n"
                        f"args = [{CODEX!r}, *sys.argv[1:]]\n"
                        "if 'exec' in args:\n"
                        f"    with open({str(self.exec_pids)!r}, 'w') as trace:\n"
                        "        trace.write(f'{os.getsid(0)} {os.getppid()} {os.getpid()}')\n"
                        f"    at = args.index('exec') + 1\n    args[at:at] = {provider_args!r}\n"
                        "os.execv(args[0], args)\n")
        shim.chmod(0o700)
        self.env = {"PATH": str(bindir) + os.pathsep + os.environ["PATH"], "HOME": str(test_home),
                    "CODEX_HOME": str(self.cli_home), "XDG_STATE_HOME": str(base / "state"),
                    "CLOUD_WORKSPACE_ROOT": str(self.root), "LANG": "C.UTF-8", "OTEL_SDK_DISABLED": "true",
                    "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "127.0.0.1"}
        self.rpc("init")

    def rpc(self, op, **kwargs):
        proc = subprocess.run([sys.executable, str(WORKER), "--stdin"], env=self.env,
                              input=json.dumps({"op": op, **kwargs}).encode(), capture_output=True,
                              check=True, timeout=10)
        envelope = json.loads(proc.stdout)
        self.assertTrue(envelope["ok"], envelope)
        return envelope["data"]

    def finished(self, task, expected="succeeded"):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            page = self.rpc("events", taskId=task["id"])
            if page["task"]["status"] not in ("queued", "running", "cancelling"):
                self.assertEqual(page["task"]["status"], expected, page["events"])
                return page
            time.sleep(0.05)
        self.fail("Codex did not finish the local fixture request")

    def test_cancel_running_native_turn_then_start_new_task(self):
        self.model.first_open = threading.Event()
        self.model.release_first = threading.Event()
        self.addCleanup(self.model.release_first.set)
        self.model.answers.extend(["open-response-item", "fresh-task-answer"])
        params = dict(provider="codex", requestId="cancel-once", prompt="waiting-for-fixture")
        task = self.rpc("start", **params)
        self.assertTrue(self.model.first_open.wait(10), "Codex did not open the fixture response")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            before = self.rpc("events", taskId=task["id"])
            if any(e["text"] == "open-response-item" for e in before["events"]):
                break
            time.sleep(.05)
        else:
            self.fail("The CLI did not persist the partial reply before cancellation")
        self.assertEqual(before["task"]["status"], "running")

        def live_identity(pid):
            try:
                stat = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                return None
            return None if stat[0] in (b"Z", b"X") else stat[19]

        # Compare process start times as well as PIDs, independently of the task status.
        supervisor, executor, cli = map(int, self.exec_pids.read_text().split())
        live = {pid: live_identity(pid) for pid in (supervisor, executor, cli)}
        self.assertNotIn(None, live.values())
        reply = self.rpc("cancel", taskId=task["id"])
        self.assertIn(reply["status"], ("cancelling", "cancelled"))
        page = self.finished(task, "cancelled")
        deadline = time.monotonic() + 5
        while any(live_identity(pid) == key for pid, key in live.items()):
            if time.monotonic() >= deadline:
                self.fail("Cancellation left the worker or native CLI running")
            time.sleep(.05)
        self.model.release_first.set()
        self.assertFalse(page["task"]["canContinue"])
        self.assertEqual(page["events"][:len(before["events"])], before["events"])
        self.assertGreater(len(page["events"]), len(before["events"]))
        self.assertEqual(self.rpc("cancel", taskId=task["id"]), page["task"])
        self.assertEqual(self.rpc("start", **params), page["task"])
        self.assertEqual(self.rpc("events", taskId=task["id"]), page)
        self.assertEqual(len(self.model.requests), 1)

        fresh = self.rpc("start", provider="codex", requestId="fresh", prompt="fresh-task-question")
        self.assertNotEqual(fresh["id"], task["id"])
        self.assertIn("fresh-task-answer", [e["text"] for e in self.finished(fresh)["events"]])
        self.assertEqual(len(self.model.requests), 2)
        inputs = json.dumps(self.model.requests[1]["input"])
        self.assertIn("fresh-task-question", inputs)
        self.assertNotIn("waiting-for-fixture", inputs)

    def test_resume_preserves_transcript_permissions_and_request_identity(self):
        self.model.answers.extend(["first-answer-marker", "second-answer-marker"])
        first = self.rpc("start", provider="codex", requestId="one", prompt="first-question-marker")
        page = self.finished(first)
        self.assertTrue(page["task"]["canContinue"])
        self.assertIn("first-answer-marker", [e["text"] for e in page["events"]])
        follow = {"provider": "codex", "requestId": "two", "prompt": "second-question-marker", "parentTaskId": first["id"]}
        second = self.rpc("start", **follow)
        self.assertEqual(self.rpc("start", **follow)["id"], second["id"])
        page = self.finished(second)
        self.assertEqual(page["task"]["parentTaskId"], first["id"])
        self.assertIn("second-answer-marker", [e["text"] for e in page["events"]])
        self.assertEqual(self.rpc("start", **follow)["id"], second["id"])
        self.assertEqual(len(self.model.requests), 2)
        for request in self.model.requests:
            catalog = json.dumps(request.get("tools", []))
            self.assertTrue('"name": "exec_command"' in catalog, "Missing sandboxed project shell")
            self.assertFalse('"name": "mcp__' in catalog, "External MCP tools advertised to the model")
        context = json.dumps(self.model.requests[1]["input"])
        for marker in ("first-question-marker", "first-answer-marker", "second-question-marker"):
            self.assertIn(marker, context)

        turns = []
        for path in (self.cli_home / "sessions").rglob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record["type"] == "turn_context":
                    turns.append(record["payload"])
        self.assertEqual(len(turns), 2)
        for turn in turns:
            self.assertEqual(turn["sandbox_policy"]["type"], "read-only")
            self.assertEqual(turn["approval_policy"], "never")

    def test_proposal_requires_review_before_writing(self):
        # A configured MCP command would run outside the CLI filesystem sandbox.
        marker = self.cli_home / "unexpected-mcp-start"
        sentinel = f"from pathlib import Path; Path({str(marker)!r}).touch()"
        (self.cli_home / "config.toml").write_text(
            f"[mcp_servers.sentinel]\ncommand = {json.dumps(sys.executable)}\n"
            f"args = {json.dumps(['-c', sentinel])}\n", encoding="utf-8")
        path = "README.md"
        snapshot = self.rpc("file", path=path)
        replacement = "Reviewed replacement marker.\n"
        self.model.answers.append(json.dumps({"content": replacement}))
        task = self.rpc("start", requestId="change", provider="proposal", prompt="Replace this file.",
                        path=path, revision=snapshot["revision"])
        self.finished(task)
        self.assertEqual(len(self.model.requests), 1)
        self.assertFalse(marker.exists())
        self.assertEqual(self.model.requests[0].get("tools", []), [])
        schema = self.model.requests[0]["text"]["format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["required"], ["content"])
        review = self.rpc("proposal", taskId=task["id"])
        self.assertEqual(review["path"], path)
        self.assertIn("+" + replacement.rstrip(), review["diff"])
        self.assertEqual(self.rpc("file", path=path), snapshot)
        self.rpc("decideProposal", taskId=task["id"], decision="apply")
        self.assertEqual(self.rpc("file", path=path)["content"], replacement)
        self.assertIn("+" + replacement.rstrip(), self.rpc("diff")["diff"])

    def mount_system_mcp(self):
        # Mount a fixture over system configuration in the child namespace only.
        bwrap = shutil.which("bwrap")
        if not bwrap:
            self.skipTest("Bubblewrap is unavailable for the system MCP fixture")
        base = self.root.parent
        marker = base / "system-mcp-started"
        config = base / "system-config.toml"
        sentinel = f"from pathlib import Path; Path({str(marker)!r}).touch()"
        config.write_text(f"[mcp_servers.system_fixture]\ncommand = {json.dumps(sys.executable)}\n"
                          f"args = {json.dumps(['-c', sentinel])}\n", encoding="utf-8")
        mount = [bwrap, "--unshare-user", "--die-with-parent", "--ro-bind", "/", "/",
                 "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--bind", str(base), str(base),
                 "--ro-bind", str(config), "/etc/codex/config.toml"]
        if subprocess.run(mount + ["true"], env=self.env, capture_output=True, timeout=5).returncode:
            self.skipTest("A child mount namespace is unavailable for the system MCP fixture")
        self.env["TMPDIR"] = str(base)
        wrapper = base / "namespaced-bin"
        wrapper.mkdir()
        inner = str(base / "bin" / "codex")
        outer = wrapper / "codex"
        outer.write_text(f"#!{sys.executable}\nimport os, sys\n"
                         f"os.execv({bwrap!r}, {mount!r} + [{inner!r}, *sys.argv[1:]])\n")
        outer.chmod(0o700)
        self.env["PATH"] = str(wrapper) + os.pathsep + self.env["PATH"]
        listing = subprocess.run([str(outer), "mcp", "list", "--json"], env=self.env, cwd=base,
                                 capture_output=True, timeout=10, check=True)
        self.assertIn("system_fixture", [row["name"] for row in json.loads(listing.stdout)])
        return marker

    def test_proposal_excludes_system_mcp_before_starting_codex(self):
        marker = self.mount_system_mcp()
        self.model.answers.append(json.dumps({"content": "fixture replacement"}))
        original = self.rpc("file", path="README.md")
        task = self.rpc("start", requestId="system-test", provider="proposal", prompt="Edit",
                        path="README.md", revision=original["revision"])
        self.finished(task)
        self.assertEqual(len(self.model.requests), 1)
        self.assertFalse(marker.exists())
        self.assertEqual(self.model.requests[0].get("tools", []), [])

    def test_chat_keeps_custom_provider_and_excludes_mcp_on_new_and_legacy_turns(self):
        # Use only provider settings from the user's fixture config, including auth.
        shim = self.root.parent / "bin" / "codex"
        shim.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv({CODEX!r}, [{CODEX!r}, *sys.argv[1:]])\n")
        self.model.fixture_token = "synthetic-loopback-token"
        self.env["WORKBENCH_FIXTURE_TOKEN"] = self.model.fixture_token
        user_marker = self.cli_home / "user-mcp-started"
        sentinel = f"from pathlib import Path; Path({str(user_marker)!r}).touch()"
        (self.cli_home / "config.toml").write_text(
            'model = "gpt-5.4-mini"\nmodel_provider = "custom"\n'
            '[model_providers.custom]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{self.model.server_port}/v1"\n'
            'env_key = "WORKBENCH_FIXTURE_TOKEN"\nsupports_websockets = false\n'
            '[mcp_servers.user_fixture]\n'
            f"command = {json.dumps(sys.executable)}\nargs = {json.dumps(['-c', sentinel])}\n",
            encoding="utf-8")
        system_marker = self.mount_system_mcp()
        self.model.answers.extend(["previous-answer", "fresh-answer", "continued-answer"])
        # Create an authentic saved conversation with the previous read-only CLI arguments.
        old = subprocess.run(["codex", "-a", "never", "exec", "--sandbox", "read-only", "--json", "-"],
                             cwd=self.root, env=self.env, input=b"previous-question\n", capture_output=True,
                             check=True, timeout=30)
        thread = next(e["thread_id"] for line in old.stdout.splitlines()
                      if (e := json.loads(line))["type"] == "thread.started")
        for marker in (user_marker, system_marker):
            self.assertTrue(marker.is_file())
            marker.unlink()

        current = self.rpc("start", provider="codex", prompt="fresh-question", requestId="fresh-chat")
        self.finished(current)
        database = next(Path(self.env["XDG_STATE_HOME"]).rglob("tasks.sqlite"))
        with sqlite3.connect(database) as db:
            db.execute("""INSERT INTO task (id, requestId, provider, prompt, status, createdAt, finishedAt)
                          VALUES ('legacy','legacy','codex','previous-question','succeeded','2025','2025')""")
            db.execute("INSERT INTO codex_turn (taskId, threadId) VALUES ('legacy', ?)", (thread,))
        resumed = self.rpc("start", provider="codex", prompt="next-question", requestId="next-chat", parentTaskId="legacy")
        result = self.finished(resumed)
        self.assertIn("continued-answer", [e["text"] for e in result["events"]])
        self.assertIn("previous-answer", json.dumps(self.model.requests[-1]["input"]))
        self.assertEqual(len(self.model.requests), 3)
        for req in self.model.requests[1:]:
            self.assertEqual(req["model"], "gpt-5.4-mini")
            catalog = json.dumps(req["tools"])
            self.assertIn("exec_command", catalog)
            self.assertNotIn("user_fixture", catalog)
            self.assertNotIn("system_fixture", catalog)
        self.assertFalse(user_marker.exists())
        self.assertFalse(system_marker.exists())


if __name__ == "__main__":
    unittest.main()
