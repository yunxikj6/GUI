from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import ANY, Mock, patch

import native_files
from linux import native_client_setup as setup
from workspace_mcp import client_server_config, is_previous_readonly_server


class DesktopClientSetup(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.env = patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.root / "data"), "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"), "INSTABOX_API_KEY": "synthetic-key-for-this-test",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_entry_can_cancel_before_project_or_dependencies_are_needed(self):
        self.assertEqual(native_files.main(["relink", "--offer-setup"]), 0)
        entry = self.root / "data/applications/cloudworkspace-instabox-clients.desktop"
        self.assertIn(" connect-clients --notify\n", entry.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "state").exists())
        ui = Mock()
        ui.choose.return_value = None
        with patch.object(setup, "ConnectionDialogs", return_value=ui), \
                patch("linux.native_mcp_runtime.find_runtime") as find, patch.object(native_files, "configure") as project:
            self.assertEqual(native_files.main(["connect-clients"]), 0)
        find.assert_not_called()
        project.assert_not_called()
        self.assertFalse(native_files.settings_path().exists())

    def test_confirm_before_install_and_no_vendor_write_on_cancel_or_download_error(self):
        project = ("m_fixture", "/data/with spaces", None)
        ui = Mock()
        ui.choose.return_value = 1
        ui.authorize.return_value = None
        with patch("linux.native_mcp_runtime.find_runtime", return_value=None), \
                patch("linux.native_mcp_runtime.prepare_runtime", side_effect=RuntimeError("download failed")) as prepare, \
                patch.object(setup, "register") as register:
            setup.connect_project_client(load_project=lambda: project, state=self.root, dialogs=ui)
            prepare.assert_not_called()
            ui.authorize.assert_called_once_with("Claude", *project[:2], True, None)
            ui.authorize.return_value = "basic"
            ui.installing.return_value = nullcontext(Mock())
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                setup.connect_project_client(load_project=lambda: project, state=self.root, dialogs=ui)
            register.assert_not_called()
        self.assertFalse((self.root / "client-connections").exists())

    def test_custom_search_entry_leaves_files_and_its_client_menu_available(self):
        applications = self.root / "data/applications"
        applications.mkdir(parents=True)
        foreign = applications / "cloudworkspace-instabox-clients.desktop"
        foreign.write_text("my saved desktop entry")
        result = subprocess.run([sys.executable, native_files.__file__, "integrate", "--machine",
                                 "m_fixture", "--project", "/data/project"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("独立搜索入口", result.stderr)
        self.assertEqual(foreign.read_text(), "my saved desktop entry")
        self.assertIn("[Desktop Action ConnectClients]", (applications /
                      "cloudworkspace-instabox-project-files.desktop").read_text())
        self.assertTrue((self.root / "data/nautilus/scripts/保存到 InstaBox").is_file())

    def test_file_project_to_claude_including_move_and_conflict(self):
        python = sys.executable
        native_files.integrate("m_fixture", "/data/one")
        ui = Mock()
        ui.choose.return_value = 1
        ui.authorize.return_value = "basic"
        with patch.object(setup, "ConnectionDialogs", return_value=ui), \
                patch("linux.native_mcp_runtime.find_runtime", return_value=python), \
                patch("linux.native_mcp_runtime.prepare_runtime") as prepare:
            self.assertEqual(native_files.main(["connect-clients"]), 0)
            ui.completed.assert_called_with("Claude", True, "m_fixture", "/data/one", ANY)
            config = self.root / "config/Claude/claude_desktop_config.json"
            receipt = self.root / "state/cloudworkspace/client-connections/claude.json"
            a = json.loads(config.read_text())["mcpServers"]["instabox-workspace"]
            self.assertEqual(a, json.loads(receipt.read_text()))
            self.assertNotIn("synthetic-key-for-this-test", config.read_text() + receipt.read_text())
            first_identity = config.stat().st_ino, config.stat().st_mtime_ns
            self.assertEqual(native_files.main(["connect-clients"]), 0)
            ui.completed.assert_called_with("Claude", False, "m_fixture", "/data/one", ANY)
            self.assertEqual(first_identity, (config.stat().st_ino, config.stat().st_mtime_ns))
            native_files.integrate("m_fixture", "/data/two")
            with patch("workspace_mcp.__file__", str(self.root / "new location/workspace_mcp.py")):
                self.assertEqual(native_files.main(["connect-clients"]), 0)
            new = json.loads(receipt.read_text())
            self.assertEqual(new["args"][4], "/data/two")
            self.assertEqual(json.loads(config.read_text())["mcpServers"]["instabox-workspace"], new)
            foreign = '{"mcpServers":{"instabox-workspace":{"command":"my-other-client"}}}'
            config.write_text(foreign)
            with self.assertRaisesRegex(ValueError, "同名"):
                setup.register("claude", a, self.root / "state/cloudworkspace")
            self.assertEqual(config.read_text(), foreign)
            self.assertEqual(json.loads(receipt.read_text()), new)
            prepare.assert_not_called()

    def test_one_permission_dialog_and_unknown_or_cancelled_selection(self):
        ui = setup.ConnectionDialogs()
        for previous, expected_row in [(None, "TRUE\nbasic"), ("basic", "TRUE\nbasic"), ("diff", "TRUE\ndiff")]:
            with self.subTest(previous=previous), patch.object(ui, "run", return_value="diff") as run:
                self.assertEqual(ui.authorize("Claude", "m_fixture", "/data/project", False, previous), "diff")
                self.assertIn(expected_row + "\n", run.call_args.kwargs["cells"])
                options = run.call_args.args[1]
                self.assertIn("--radiolist", options)
                self.assertIn("--print-column=2", options)
                text = next(option for option in options if option.startswith("--text="))
                for word in ("未暂存", "已暂存", "最近一次提交", "删除行", "未跟踪", "厂商会话"):
                    self.assertIn(word, text)
                if previous:
                    self.assertIn("上次", text)
        with patch.object(ui, "run", return_value=None):
            self.assertIsNone(ui.authorize("Claude", "m_x", "/p", True, "diff"))
        with patch.object(ui, "run", return_value="unexpected"):
            with self.assertRaisesRegex(ValueError, "读取权限"):
                ui.authorize("Claude", "m_x", "/p", True, None)

    def test_long_authorization_path_is_complete_in_the_scrollable_table(self):
        project = "/data/" + "目录/" * 45 + "target\nfolder"
        ui = setup.ConnectionDialogs()
        with patch.object(ui, "run", return_value="basic") as run:
            self.assertEqual(ui.authorize("Claude", "m_fixture", project, False, None), "basic")
        options = run.call_args.args[1]
        self.assertIn("--column=完整项目目录", options)
        path = setup._display(project)
        self.assertEqual(run.call_args.kwargs["cells"].splitlines()[3::4], [path, path])
        text = next(option for option in options if option.startswith("--text="))
        self.assertIn("…", text)
        self.assertIn("target", text)
        self.assertNotIn(path, text)

    def test_same_project_grant_keep_cancel_revoke_and_new_target_defaults(self):
        state = self.root / "state"
        config = self.root / "config/Claude/claude_desktop_config.json"
        receipt = state / "client-connections/claude.json"
        ui = Mock()
        ui.choose.return_value = 1
        target = ["m_fixture", "/data/project", None]
        def connect(choice, prior):
            ui.authorize.return_value = choice
            setup.connect_project_client(load_project=lambda: target, state=state, dialogs=ui)
            ui.authorize.assert_called_with("Claude", *target[:2], False, prior)
        def saved():
            value = json.loads(config.read_text())["mcpServers"]["instabox-workspace"]
            self.assertEqual(value, json.loads(receipt.read_text()))
            return value["args"]
        with patch("linux.native_mcp_runtime.find_runtime", return_value=sys.executable):
            connect("basic", None)
            self.assertNotIn("--allow-diff", saved())
            connect("diff", "basic")
            self.assertEqual(saved()[-1], "--allow-diff")
            self.assertIn("包含 Git 差异", ui.completed.call_args.args[-1])
            snapshots = [(p.stat().st_ino, p.read_bytes()) for p in (config, receipt)]
            connect("diff", "diff")
            self.assertIs(ui.completed.call_args.args[1], False)
            connect(None, "diff")
            self.assertEqual([(p.stat().st_ino, p.read_bytes()) for p in (config, receipt)], snapshots)
            connect("basic", "diff")
            self.assertNotIn("--allow-diff", saved())
            self.assertIn("基础读取", ui.completed.call_args.args[-1])
            connect("diff", "basic")
            target[1] = "/another-project"
            connect("basic", None)
            self.assertNotIn("--allow-diff", saved())
            connect("diff", "basic")
            target[0] = "m_another"
            connect("basic", None)
            self.assertNotIn("--allow-diff", saved())

    def test_codex_diff_choice_does_not_grant_claude_the_same_project(self):
        target = ("m_fixture", "/shared/project", None)
        ui = Mock()
        ui.choose.return_value = 0
        ui.authorize.return_value = "diff"
        with patch("linux.native_mcp_runtime.find_runtime", return_value=sys.executable), \
                patch.object(setup.os, "access", return_value=True), \
                patch("linux.codex_project_connection.configure", return_value=True) as codex:
            setup.connect_project_client(load_project=lambda: target, state=self.root, dialogs=ui)
            ui.authorize.assert_called_with("ChatGPT", *target[:2], False, None)
            server = codex.call_args.args[0]
            self.assertEqual(server, client_server_config(sys.executable, *target[:2], host="codex", allow_diff=True))
            self.assertEqual(json.loads((self.root / "client-connections/codex.json").read_text()), server)
            ui.choose.return_value = 1
            ui.authorize.return_value = None
            setup.connect_project_client(load_project=lambda: target, state=self.root, dialogs=ui)
            ui.authorize.assert_called_with("Claude", *target[:2], False, None)
        self.assertFalse((self.root / "client-connections/claude.json").exists())
        self.assertFalse((self.root / "config/Claude/claude_desktop_config.json").exists())

    def test_an_older_consent_dialog_cannot_overwrite_a_new_permission(self):
        target = ("m_fixture", "/p", None)
        basic = client_server_config(sys.executable, *target[:2], host="claude")
        diff = client_server_config(sys.executable, *target[:2], host="claude", allow_diff=True)
        setup.register("claude", basic, self.root)
        ui = Mock()
        ui.choose.return_value = 1
        def meanwhile(*args):
            self.assertEqual(args[-1], "basic")
            setup.register("claude", diff, self.root)
            return "basic"
        ui.authorize.side_effect = meanwhile
        with patch("linux.native_mcp_runtime.find_runtime", return_value=sys.executable):
            with self.assertRaisesRegex(ValueError, "重新打开"):
                setup.connect_project_client(load_project=lambda: target, state=self.root, dialogs=ui)
        self.assertEqual(json.loads((self.root / "client-connections/claude.json").read_text()), diff)
        self.assertEqual(json.loads((self.root / "config/Claude/claude_desktop_config.json").read_text())
                         ["mcpServers"]["instabox-workspace"], diff)

    def test_manual_config_migration_only_recognizes_original_readonly_system_adapter(self):
        new = client_server_config(sys.executable, "m_new", "/data/new", host="codex")
        old = client_server_config(sys.executable, "m_old", "/data/prior", host="codex")
        self.assertTrue(is_previous_readonly_server(old, new, host="codex"))
        for foreign in [dict(old, command="/a/different/python"), dict(old, disabled=True),
                        dict(old, env_vars=["A_PRIVATE_VARIABLE"]),
                        client_server_config(sys.executable, "m_old", "/data/prior", host="codex", allow_save=True),
                        client_server_config(sys.executable, "m_old", "/data/prior", host="codex",
                                             credential_source="environment")]:
            self.assertFalse(is_previous_readonly_server(foreign, new, host="codex"))
