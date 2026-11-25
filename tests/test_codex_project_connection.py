"""User-layer ownership, conditional updates and discovery in the bundled ChatGPT Codex."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import fcntl
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from linux import codex_project_connection as connection
from workspace_mcp import client_server_config

BINARY = Path(os.environ.get("CLOUDWORKSPACE_TEST_CHATGPT_CODEX", connection.BUNDLED_CODEX))
CONFIG = Path("/example/config.toml")


def server(project):
    return client_server_config(sys.executable, "m_fixture", project, host="codex")


class CurrentConfig:
    def __init__(self, current=None):
        self.current = current
        self.extra_layers = []
        self.edits = []

    def call(self, method, params):
        if method == "config/read":
            raw = {connection.NAME: self.current} if self.current is not None else {}
            layer = {"name": {"type": "user", "file": str(CONFIG)}, "version": "observed", "config": {"mcp_servers": raw}}
            return {"config": {"mcp_servers": raw}, "layers": [layer, *self.extra_layers]}
        if method != "config/batchWrite" or params["expectedVersion"] != "observed" or params["filePath"] != str(CONFIG):
            raise AssertionError("unexpected configuration request")
        self.edits.extend(params["edits"])
        self.current = dict(self.current or {})
        for edit in params["edits"]:
            self.current[edit["keyPath"].removeprefix("mcp_servers.instabox-workspace.")] = edit["value"]
        return {"status": "ok", "filePath": str(CONFIG)}


class CodexConfigTests(unittest.TestCase):
    def test_update_only_owned_leaf_and_idempotent(self):
        new = server("/project/after")
        state = CurrentConfig(server("/project/before"))
        self.assertTrue(connection._update(state.call, CONFIG, new, None))
        self.assertEqual(state.edits, [{"keyPath": "mcp_servers.instabox-workspace.args", "value": new["args"],
                                       "mergeStrategy": "upsert"}])
        self.assertFalse(connection._update(state.call, CONFIG, new, None))
        self.assertEqual(len(state.edits), 1)

    def test_explicit_receipt_allows_a_relocated_launcher(self):
        new = server("/main")
        old = copy.deepcopy(new)
        old["args"][0] = "/older release/workspace_mcp.py"
        state = CurrentConfig(old)
        with self.assertRaisesRegex(ValueError, "原连接"):
            connection._update(state.call, CONFIG, new, None)
        self.assertFalse(state.edits)
        self.assertTrue(connection._update(state.call, CONFIG, new, old))

    def test_disabled_custom_and_higher_layer_entries_are_preserved(self):
        new = server("/main")
        for current in ({**new, "enabled": False}, {**new, "cwd": "/custom"},
                        {**new, "command": "/some/other/application"}):
            state = CurrentConfig(current)
            with self.subTest(current=current), self.assertRaises(ValueError):
                connection._update(state.call, CONFIG, new, None)
            self.assertFalse(state.edits)
        for name in ({"type": "system", "file": "/etc/codex/config.toml"},
                     {"type": "user", "file": str(CONFIG), "profile": "other"}):
            state = CurrentConfig()
            state.extra_layers = [{"name": name, "config": {"mcp_servers": {connection.NAME: {"url": "example"}}}}]
            with self.assertRaisesRegex(ValueError, "配置层"):
                connection._update(state.call, CONFIG, new, None)
            self.assertFalse(state.edits)

    def test_invalid_requested_server_and_missing_app_do_not_create_config(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODEX_HOME": folder + "/new"}):
            for args in (["--allow-save"], ["--credential-source", "environment"]):
                bad = server("/project")
                bad["args"] += args
                with self.assertRaisesRegex(ValueError, "只读"):
                    connection.configure(bad, executable=folder + "/missing")
            with self.assertRaisesRegex(ValueError, "官方客户端"):
                connection.configure(server("/project"), executable=folder + "/missing")
            self.assertFalse(Path(folder, "new").exists())

    def test_busy_profile_returns_without_touching_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODEX_HOME": folder}):
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                with patch.object(connection, "_TIMEOUT", 0.02), self.assertRaisesRegex(ValueError, "稍后重试"):
                    connection.configure(server("/busy"), executable="/bin/true")
            finally:
                os.close(fd)
            self.assertFalse(Path(folder, "config.toml").exists())

    def test_protocol_errors_hide_unrelated_settings_and_report_version_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "codex"
            for code, expected in (("configVersionConflict", "其他进程"), ("configValidationError", "拒绝")):
                binary.write_text(
                    "#!/usr/bin/python3\nimport json,sys\nfor s in sys.stdin:\n"
                    " o=json.loads(s)\n if 'id' in o:\n"
                    "  print(json.dumps({'id':o['id'],'error':{'message':'private unrelated value',"
                    f"'data':{{'config_write_error_code':{code!r}}}}}}}),flush=True)\n")
                binary.chmod(0o700)
                with patch.dict(os.environ, {"CODEX_HOME": str(root / "settings")}), self.assertRaises(ValueError) as ex:
                    connection.configure(server("/project"), executable=binary)
                self.assertIn(expected, str(ex.exception))
                self.assertNotIn("private unrelated", str(ex.exception))


@unittest.skipUnless(BINARY.is_file() and os.access(BINARY, os.X_OK) and shutil.which("bwrap"),
                     "requires the official ChatGPT Linux bundled Codex and bubblewrap")
class NativeChatGPTConfigTests(unittest.TestCase):
    def setUp(self):
        cleanup = ExitStack()
        self.addCleanup(cleanup.close)
        self.root = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        self.home = self.root / "settings"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        host = self.root / "home"
        host.mkdir()
        args = ["bwrap", "--die-with-parent", "--unshare-net", "--unshare-pid", "--ro-bind", "/", "/",
                "--bind", str(self.root), str(self.root), "--dev", "/dev", "--proc", "/proc"]
        if Path("/etc/codex").is_dir():
            args.extend(["--tmpfs", "/etc/codex"])
        args.append("--")
        probe = subprocess.run([*args, "/bin/true"], capture_output=True, timeout=5)
        if probe.returncode:
            self.skipTest("private PID and network namespaces unavailable")
        self.binary = self.root / "isolated-bundled-codex"
        self.binary.write_text("#!/bin/sh\nexec " + shlex.join([*args, str(BINARY)]) + ' "$@"\n')
        self.binary.chmod(0o700)
        cleanup.enter_context(patch.dict(os.environ, {"HOME": str(host), "CODEX_HOME": str(self.home),
                                                       "PATH": "/usr/local/bin:/usr/bin:/bin", "TMPDIR": str(self.root),
                                                       "LC_ALL": "C.UTF-8"}, clear=True))

    def test_roundtrip_comments_restart_local_mcp_tools_and_custom_name_conflict(self):
        initial = '# retain unrelated comments\n[notice]\n"my-local-note" = true # unchanged\n'
        self.config.write_text(initial)
        wanted = server("/研发/project #2")
        self.assertTrue(connection.configure(wanted, executable=self.binary))
        before = self.config.read_bytes()
        self.assertIn(initial.encode(), before)
        self.assertFalse(connection.configure(wanted, executable=self.binary))
        self.assertEqual(self.config.read_bytes(), before)
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("MCP SDK required for native tool discovery")
        with subprocess.Popen([str(self.binary), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, cwd="/") as process:
            try:
                rpc = connection._AppServer(process)
                rpc.call("initialize", {"clientInfo": {"name": "test_client", "version": "1"}})
                rpc.send({"method": "initialized"})
                listed = rpc.call("mcpServerStatus/list", {"limit": 20})["data"]
                record, = (item for item in listed if item["name"] == connection.NAME)
                self.assertIsNone(record.get("toolsError"))
                self.assertEqual(set(record["tools"]), {"list_workspace_directory", "read_workspace_text",
                                                        "get_workspace_git_status"})
            finally:
                process.terminate()
        self.config.write_text('[mcp_servers.instabox-workspace]\ncommand="/bin/true" # user registered\n')
        custom = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "原连接"):
            connection.configure(wanted, previous=wanted, executable=self.binary)
        self.assertEqual(self.config.read_bytes(), custom)

    def test_concurrent_identical_bindings_only_write_once(self):
        wanted = server("/同一项目")
        def register(_):
            return connection.configure(wanted, executable=self.binary)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertCountEqual(pool.map(register, range(4)), [True, False, False, False])

    def test_linked_dotfile_retains_link_and_unrelated_server_without_launching_it(self):
        dotfiles = self.root / "user-dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "codex.toml"
        marker = self.root / "unrelated-mcp-launched"
        existing = f'# local server, independent of this project\n[mcp_servers.other]\ncommand = "/usr/bin/touch"\nargs = ["{marker}"]\n'
        target.write_text(existing)
        self.config.symlink_to(target)
        inode = self.config.lstat().st_ino
        self.assertTrue(connection.configure(server("/original"), executable=self.binary))
        self.assertTrue(connection.configure(server("/切换后的工程"), executable=self.binary))
        self.assertEqual(self.config.lstat().st_ino, inode)
        self.assertEqual(self.config.readlink(), target)
        self.assertIn(existing, target.read_text())
        self.assertIn("切换后的工程", target.read_text())
        self.assertFalse(marker.exists())

    def test_real_stale_version_leaves_external_change_and_comment_intact(self):
        wanted = server("/project")
        original = connection._AppServer.call
        self.config.write_text("# my existing settings\n")
        changed = '# edit after reading\nmodel = "another-model"\n'
        def intervene(rpc, method, params):
            if method == "config/batchWrite":
                self.config.write_text(changed)
            return original(rpc, method, params)
        with patch.object(connection._AppServer, "call", intervene), self.assertRaisesRegex(ValueError, "其他进程"):
            connection.configure(wanted, executable=self.binary)
        self.assertEqual(self.config.read_text(), changed)
