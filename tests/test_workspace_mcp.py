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
    async def client(self, key=KEY, *, allow_save=False):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        # Replace only the official API origin in this test child. The adapter has no URL flag.
        shim = ("import sys, instabox, workspace_mcp; instabox.InstaBox.base=sys.argv.pop(1); "
                "sys.exit(workspace_mcp.main())")
        args = ["-c", shim, self.provider, "--machine", MACHINE, "--project", PROJECT,
                "--credential-source", "environment"]
        if allow_save:
            args.append("--allow-save")
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
