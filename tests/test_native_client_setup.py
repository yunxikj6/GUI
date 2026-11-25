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
        ui.authorize.return_value = False
        with patch("linux.native_mcp_runtime.find_runtime", return_value=None), \
                patch("linux.native_mcp_runtime.prepare_runtime", side_effect=RuntimeError("download failed")) as prepare, \
                patch.object(setup, "register") as register:
            setup.connect_project_client(load_project=lambda: project, state=self.root, dialogs=ui)
            prepare.assert_not_called()
            ui.authorize.assert_called_once_with("Claude", *project[:2], True)
            ui.authorize.return_value = True
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
        ui.authorize.return_value = True
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
