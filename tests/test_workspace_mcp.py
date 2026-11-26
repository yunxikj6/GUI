"""Negotiate over real stdio with the upstream MCP SDK; run the cloud worker in an isolated fixture."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MACHINE = "m_fixture"
PROJECT = "/data/团队 项目"
KEY = "synthetic-test-key"


class ConnectionTemplates(unittest.TestCase):
    def test_unknown_cli_option_and_abbreviated_permission_are_rejected(self):
        for option in ("--allow-di", "--ref"):
            result = subprocess.run([sys.executable, str(ROOT / "workspace_mcp.py"), "--machine", MACHINE,
                                     "--project", PROJECT, "--print-config", option], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertIn(option.encode(), result.stderr)

    def test_readonly_profiles_recognize_only_exact_base_or_diff_templates(self):
        import workspace_mcp
        for host in ("claude", "codex"):
            def config(project, **options):
                return workspace_mcp.client_server_config(sys.executable, MACHINE, project, host=host, **options)

            before = config("/data/before")
            before_diff = config("/data/before", allow_diff=True)
            relative = dict(before_diff, command="python3")
            self.assertFalse(workspace_mcp.is_previous_readonly_server(relative, relative, host=host))
            self.assertFalse(workspace_mcp.is_previous_readonly_server(before, before, host="other"))
            for new in (config("/data/new"), config("/data/new", allow_diff=True)):
                for prior in (before, before_diff):
                    self.assertTrue(workspace_mcp.is_previous_readonly_server(prior, new, host=host))
                for prior in (config("/data/before", allow_save=True, allow_diff=True),
                              config("/data/before", allow_diff=True, credential_source="environment"),
                              dict(before_diff, env={"EXTRA": "value"}),
                              dict(before, args=[*before["args"], "--allow-save"]),
                              dict(before_diff, args=[*before_diff["args"], "--allow-diff"]),
                              dict(before_diff, args=["/other/adapter", *before_diff["args"][1:]])):
                    with self.subTest(host=host, prior=prior):
                        self.assertFalse(workspace_mcp.is_previous_readonly_server(prior, new, host=host))

            for flags in ([], ["--allow-diff"], ["--allow-save", "--allow-diff"]):
                printed = subprocess.check_output([sys.executable, str(ROOT / "workspace_mcp.py"),
                                                   "--machine", MACHINE, "--project", PROJECT, "--host", host,
                                                   *flags, "--print-config"], text=True)
                if host == "claude":
                    recovered = json.loads(printed)["mcpServers"]["instabox-workspace"]
                    self.assertEqual(set(recovered), {"command", "args"})
                    actual = recovered["args"]
                else:
                    actual = json.loads(next(line.partition(" = ")[2] for line in printed.splitlines()
                                             if line.startswith("args = ")))
                    self.assertEqual("tools.save_workspace_text.approval_mode" in printed, "--allow-save" in flags)
                self.assertEqual(actual, [str(ROOT / "workspace_mcp.py"), "--machine", MACHINE, "--project", PROJECT,
                                          "--credential-source", "system", *flags])


@unittest.skipUnless(sys.platform.startswith("linux"), "requires a Linux user bus")
class LinuxCredentials(unittest.TestCase):
    def test_filtered_bus_requires_a_private_owned_runtime_and_no_plaintext_fallback(self):
        import workspace_mcp
        backend = Mock()
        backend.Keyring.return_value.get_password.return_value = "synthetic-vault-value"
        with tempfile.TemporaryDirectory() as tmp, socket.socket(socket.AF_UNIX) as bus:
            root = Path(tmp)
            bus.bind(str(root / "bus"))
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp, "INSTABOX_API_KEY": "unrelated"}, clear=True), \
                    patch.dict(sys.modules, {"keyring.backends.SecretService": backend}):
                self.assertEqual(workspace_mcp.read_secret(MACHINE, "system"), "synthetic-vault-value")
                self.assertEqual(os.environ.pop("DBUS_SESSION_BUS_ADDRESS"), "unix:path=" + tmp + "/bus")
                root.chmod(0o755)
                with self.assertRaises(workspace_mcp.BridgeError) as error:
                    workspace_mcp.read_secret(MACHINE, "system")
                self.assertEqual(error.exception.code, "credential_store")
                backend.Keyring.assert_called_once()


@unittest.skipUnless(importlib.util.find_spec("mcp") and sys.platform.startswith("linux"),
                     "requires the optional MCP SDK and a Linux worker")
class WorkspaceMCP(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.seen = []
        self.get_count = 0
        self.suspended = False
        case = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                case.get_count += 1
                if self.headers.get("X-API-Key") != KEY or self.path != "/v1/machines":
                    return self.reply(403, {})
                self.reply(200, {"machines": [{"id": MACHINE}]})

            def do_POST(self):
                if self.headers.get("X-API-Key") != KEY or self.path != "/v1/machines/" + MACHINE + "/exec":
                    return self.reply(403, {})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not body["noHistory"] or body["envs"] != {"CLOUD_WORKSPACE_ROOT": PROJECT}:
                    return self.reply(400, {})
                req = json.loads(body["stdin"])
                case.seen.append(req)
                if case.suspended:
                    return self.reply(409, {"error": {"code": "machine_suspended", "message": "provider detail"}})
                args = body["argv"]
                if args != ["python3", "-I", "-B", "-c", (ROOT / "cloud/workspace.py").read_text(), "--stdin"]:
                    return self.reply(400, {})
                env = {"PATH": os.defpath, "CLOUD_WORKSPACE_ROOT": str(case.project)}
                cmd = subprocess.run([sys.executable, *args[1:]], input=body["stdin"].encode(), env=env,
                                     capture_output=True, timeout=10)
                self.reply(200, {"exitCode": cmd.returncode, "stdout": cmd.stdout.decode(), "truncated": False})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.provider = "http://127.0.0.1:" + str(server.server_port)

    @asynccontextmanager
    async def client(self, key=KEY, *, allow_save=False, allow_diff=False):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        # Replace only the official API origin in this test child. The adapter has no URL flag.
        shim = ("import sys, instabox, workspace_mcp; instabox.InstaBox.base=instabox.PROVIDER_ORIGIN=sys.argv.pop(1); "
                "sys.exit(workspace_mcp.main())")
        args = ["-c", shim, self.provider, "--machine", MACHINE, "--project", PROJECT,
                "--credential-source", "environment"]
        if allow_save:
            args.append("--allow-save")
        if allow_diff:
            args.append("--allow-diff")
        params = StdioServerParameters(command=sys.executable, args=args, cwd=ROOT,
                                       env={"INSTABOX_API_KEY": key, "NO_PROXY": "127.0.0.1"})
        async with stdio_client(params) as pipes:
            async with ClientSession(*pipes) as session:
                yield session

    async def test_negotiation_tools_pagination_unicode_and_read_only_boundary(self):
        expected = "前一行\r\n下一行🙂\r结束"
        target = self.project / "笔记.txt"
        target.write_bytes(expected.encode())
        (self.project / "data.bin").write_bytes(b"\0B")
        (self.project / "outside").symlink_to(ROOT)
        (self.project / ".git").mkdir()
        for i in range(403):
            (self.project / f"r{i:03d}").touch()
        before = sorted(p.name for p in self.project.iterdir())
        async with self.client() as session:
            info = await session.initialize()
            self.assertEqual(info.serverInfo.name, "instabox-workspace")
            self.assertIsNotNone(info.capabilities.tools)
            self.assertIsNone(info.capabilities.resources)
            tools = (await session.list_tools()).tools
            self.assertEqual({t.name for t in tools}, {"list_workspace_directory", "read_workspace_text", "get_workspace_git_status"})
            self.assertTrue(all(t.annotations.readOnlyHint for t in tools))
            self.assertEqual(self.seen, [])
            self.assertTrue((await session.call_tool("get_workspace_git_diff", {"scope": "unstaged"})).isError)
            self.assertEqual(self.seen, [])
            self.assertEqual(self.get_count, 0)
            repo_status = await session.call_tool("get_workspace_git_status", {})
            self.assertEqual(json.loads(repo_status.content[0].text)["gitState"], "error")
            self.assertTrue((await session.call_tool("save_workspace_text", {"path": target.name, "revision": "0"*64, "content": "x"})).isError)
            listing = await session.call_tool("list_workspace_directory", {})
            first = json.loads(listing.content[0].text)
            second = await session.call_tool("list_workspace_directory", {"cursor": first["nextCursor"]})
            last = json.loads(second.content[0].text)
            paths = [entry["path"] for page in [first,last] for entry in page["entries"]]
            self.assertEqual(len(paths), 405)
            self.assertNotIn(".git", paths)
            self.assertNotIn("outside", paths)
            self.assertIsNone(last["nextCursor"])
            read = await session.call_tool("read_workspace_text", {"path": target.name})
            data = json.loads(read.content[0].text)
            self.assertEqual(data["content"], expected)
            self.assertEqual(data["revision"], hashlib.sha256(expected.encode()).hexdigest())
            for path in ("../secret", "outside/README.md", ".git/config", "data.bin", "missing"):
                with self.subTest(path=path):
                    response = await session.call_tool("read_workspace_text", {"path": path})
                    self.assertTrue(response.isError)
            issued = len(self.seen)
            for name, params in [("save", {}), ("read_workspace_text", {"path": 2}),
                                 ("list_workspace_directory", {"op": "save", "content": "change"})]:
                self.assertTrue((await session.call_tool(name, params)).isError)
            self.assertEqual(len(self.seen), issued)
            self.suspended = True
            response = await session.call_tool("list_workspace_directory", {})
            self.assertTrue(response.isError)
            self.assertIn("恢复", response.content[0].text)
            self.assertNotIn("provider detail", response.content[0].text)
        self.assertTrue(all(req["op"] in {"file", "tree", "status"} for req in self.seen))
        self.assertEqual(before, sorted(p.name for p in self.project.iterdir()))
        self.assertEqual(target.read_bytes(), expected.encode())

    async def test_explicit_save_requires_read_revision_and_rejects_write_conflicts(self):
        subprocess.run(["git", "init", "-q", self.project], check=True)
        path = self.project / "笔记.txt"
        path.write_text("base")
        subprocess.run(["git", "-C", self.project, "add", path.name], check=True)
        path.write_text("newer")
        (self.project / "untracked").touch()
        async with self.client(allow_save=True) as session:
            await session.initialize()
            actions = {tool.name: tool for tool in (await session.list_tools()).tools}
            self.assertNotIn("get_workspace_git_diff", actions)
            self.assertFalse(actions["save_workspace_text"].annotations.readOnlyHint)
            self.assertTrue(actions["save_workspace_text"].annotations.destructiveHint)
            status = await session.call_tool("get_workspace_git_status", {})
            git = json.loads(status.content[0].text)
            self.assertEqual(git["gitState"], "ready")
            self.assertFalse(git["truncated"])
            self.assertIn({"path": path.name, "status": "AM"}, git["entries"])
            self.assertIn({"path": "untracked", "status": "??"}, git["entries"])
            rev = hashlib.sha256(path.read_bytes()).hexdigest()
            args = {"path": path.name, "revision": rev, "content": "正文🙂\r\n"}
            self.assertTrue((await session.call_tool("save_workspace_text", args)).isError)
            read = await session.call_tool("read_workspace_text", {"path": path.name})
            self.assertEqual(json.loads(read.content[0].text)["revision"], rev)
            saved = await session.call_tool("save_workspace_text", args)
            self.assertFalse(saved.isError)
            self.assertEqual(path.read_bytes(), args["content"].encode())
            stale = await session.call_tool("save_workspace_text", {**args,"content":"stale"})
            self.assertTrue(stale.isError)
            self.assertIn("重新读取", stale.content[0].text)
            self.assertEqual(stale.structuredContent, {"code": "conflict"})
            self.assertEqual(path.read_bytes(), args["content"].encode())
            self.assertTrue((await session.call_tool("save_workspace_text", {**args,"path":"missing"})).isError)
            self.assertFalse((self.project / "missing").exists())

    async def test_only_explicit_diff_and_fixed_scopes_reach_the_real_git_worker(self):
        def git(*args):
            subprocess.run(["git", "-C", self.project, "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false",
                            "-c", "core.hooksPath=/dev/null", *args], check=True)

        async with self.client(allow_diff=True) as session:
            await session.initialize()
            actions = {tool.name: tool for tool in (await session.list_tools()).tools}
            self.assertNotIn("save_workspace_text", actions)
            tool = actions["get_workspace_git_diff"]
            self.assertTrue(tool.annotations.readOnlyHint and tool.annotations.idempotentHint)
            self.assertFalse(tool.annotations.destructiveHint or tool.annotations.openWorldHint)
            self.assertEqual(tool.inputSchema["required"], ["scope"])
            self.assertEqual(tool.inputSchema["properties"]["scope"]["enum"], ["staged", "unstaged", "last_commit"])
            for args in ({}, {"scope": None}, {"scope": ["staged"]}, {"scope": "HEAD"},
                         {"scope": "staged", "path": "other"}, {"scope": "last_commit", "ref": "HEAD~9"},
                         {"scope": "unstaged", "command": "git status"}):
                with self.subTest(args=args):
                    self.assertTrue((await session.call_tool(tool.name, args)).isError)
            self.assertEqual(self.seen, [])
            self.assertEqual(self.get_count, 0)

            async def diff(scope):
                result = await session.call_tool(tool.name, {"scope": scope})
                self.assertFalse(result.isError, result.content)
                data = json.loads(result.content[0].text)
                request = {"op": "commit_diff"} if scope == "last_commit" else {"op": "diff", "scope": scope}
                self.assertEqual(self.seen[-1], request)
                self.assertEqual(data["scope"], scope)
                return data

            self.assertEqual((await diff("staged"))["gitState"], "none")
            self.assertEqual((await diff("last_commit"))["gitState"], "none")
            git("init", "-q")
            self.assertEqual((await diff("last_commit"))["gitState"], "unborn")
            target = self.project / "tracked.txt"
            target.write_text("原始正文\n")
            git("add", target.name)
            git("commit", "-qm", "first")
            target.write_text("仅暂存内容\n")
            git("add", target.name)
            target.write_text("原始正文\n")
            (self.project / "untracked.txt").touch()
            staged = await diff("staged")
            unstaged = await diff("unstaged")
            commit = await diff("last_commit")
            for layer in (staged, unstaged, commit):
                self.assertFalse(layer["truncated"])
            self.assertIn("+仅暂存内容", staged["diff"])
            self.assertNotIn("untracked.txt", staged["diff"])
            self.assertIn("-仅暂存内容", unstaged["diff"])
            self.assertIn("untracked.txt", unstaged["diff"])
            self.assertIn("+原始正文", commit["diff"])
            self.assertNotIn("仅暂存内容", commit["diff"])
            self.assertNotIn("untracked.txt", commit["diff"])
            head = subprocess.check_output(["git", "-C", self.project, "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(commit["commit"], head)
            self.assertIsNone(commit["parent"])
            target.write_text("层内大行🙂" * 20000 + "\n")
            large = await diff("unstaged")
            self.assertTrue(large["truncated"])
            self.assertLessEqual(len(large["diff"].encode("utf-8")), 64 * 1024)
            self.assertNotIn("\uFFFD", large["diff"])
            git("commit", "-qm", "second")
            tip = await diff("last_commit")
            self.assertEqual(tip["parent"], head)
            self.assertIn("+仅暂存内容", tip["diff"])
            # A shallow checkout can name a parent whose object is absent locally.
            (self.project / ".git/objects" / head[:2] / head[2:]).unlink()
            self.assertEqual(await diff("last_commit"),
                             {"scope": "last_commit", "gitState": "parent_unavailable", "diff": "",
                              "truncated": False, "commit": tip["commit"], "parent": head})
            (self.project / ".git/HEAD").write_text("broken\n")
            failed = await session.call_tool(tool.name, {"scope": "staged"})
            self.assertTrue(failed.isError)
            self.assertEqual(failed.structuredContent, {"code": "git_error"})
            self.suspended = True
            failed = await session.call_tool(tool.name, {"scope": "last_commit"})
            self.assertTrue(failed.isError)
            self.assertEqual(failed.structuredContent, {"code": "machine_suspended"})
            self.assertNotIn("provider detail", failed.content[0].text)

    async def test_diff_and_save_can_be_enabled_together_without_contacting_provider(self):
        async with self.client(allow_diff=True, allow_save=True) as session:
            await session.initialize()
            tools = {tool.name: tool.annotations for tool in (await session.list_tools()).tools}
            self.assertTrue(tools["get_workspace_git_diff"].readOnlyHint)
            self.assertFalse(tools["save_workspace_text"].readOnlyHint)
            self.assertEqual(self.seen, [])

    async def test_empty_credential_and_generated_config(self):
        flags = ["--machine", MACHINE, "--project", PROJECT, "--credential-source", "environment"]
        output = await asyncio.to_thread(subprocess.check_output,
                                         [sys.executable, str(ROOT / "workspace_mcp.py"), *flags, "--print-config"],
                                         env={**os.environ, "INSTABOX_API_KEY": KEY})
        conf = json.loads(output)["mcpServers"]["instabox-workspace"]
        self.assertEqual(conf["args"], [str(ROOT / "workspace_mcp.py"), *flags])
        self.assertEqual(set(conf), {"command", "args"})
        self.assertNotIn(KEY.encode(), output)
        codex = await asyncio.to_thread(subprocess.check_output, [sys.executable, str(ROOT / "workspace_mcp.py"),
                                              *flags, "--allow-save", "--host", "codex", "--print-config"])
        flags_line = next(line for line in codex.decode().splitlines() if line.startswith("args = "))
        self.assertEqual(json.loads(flags_line.split(" = ",1)[1]), [str(ROOT / "workspace_mcp.py"), *flags, "--allow-save"])
        inherited = json.loads(next(line.partition(" = ")[2] for line in codex.decode().splitlines()
                                    if line.startswith("env_vars = ")))
        self.assertCountEqual(inherited, ["INSTABOX_API_KEY", "HTTPS_PROXY", "https_proxy", "NO_PROXY",
                                          "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"])
        self.assertIn(b'tools.save_workspace_text.approval_mode = "prompt"', codex)
        self.assertNotIn(KEY.encode(), codex)
        async with self.client("") as session:
            await session.initialize()
            result = await session.call_tool("list_workspace_directory", {})
            self.assertTrue(result.isError)
            self.assertIn("凭据", result.content[0].text)
        self.assertEqual(self.seen, [])
