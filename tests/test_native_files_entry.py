"""Exercise the native project entry with normal files and the same cloud worker as the application."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import native_files
from cloud import workspace
from instabox import BridgeError


class NativeFilesEntry(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cloud = self.root / "cloud"
        self.cloud.mkdir()
        (self.cloud / "note.txt").write_text("cloud copy\n", encoding="utf-8")
        dirs = {"XDG_DATA_HOME": str(self.root / "data"), "XDG_CONFIG_HOME": str(self.root / "config")}
        stack = patch.dict(os.environ, dirs)
        stack.start()
        self.addCleanup(stack.stop)
        self.calls = []

        def rpc(machine, project, request):
            self.calls.append((machine, project, request["op"]))
            try:
                return workspace.dispatch(request, self.cloud)
            except workspace.WorkspaceError as exc:
                raise BridgeError(exc.code, str(exc)) from None
        transport = patch.object(native_files, "client", return_value=rpc)
        transport.start()
        self.addCleanup(transport.stop)

    def invoke(self, *args, expect=0):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            rc = native_files.main([str(a) for a in args])
        self.assertEqual(rc, expect, error.getvalue())
        return out.getvalue().strip(), error.getvalue()

    def test_connect_open_save_and_conflict_recovery_from_files(self):
        self.invoke("integrate", "--machine", "m_fixture", "--project", "/data/project")
        self.assertEqual(self.calls, [])
        self.assertTrue((self.root / "data/applications/cloudworkspace-instabox-project-files.desktop").is_file())
        with patch("linux.native_file_picker.choose_file", return_value="note.txt"), patch.object(native_files, "show_file") as show:
            out, _ = self.invoke("browse", "--notify")
            local = Path(out)
            show.assert_called_once_with(local)
        local.write_text("本地稿\n", encoding="utf-8")
        before = len(self.calls)
        self.assertEqual(Path(self.invoke("checkout", "note.txt")[0]), local)
        self.assertEqual(json.loads(self.invoke("inspect", local)[0])["status"], "modified")
        self.assertEqual(len(self.calls), before)
        with patch.object(native_files, "message") as message, patch.object(native_files, "show_file") as show:
            self.invoke("publish", "--notify", "--", local)
            self.assertEqual((self.cloud / "note.txt").read_bytes(), local.read_bytes())
            (self.cloud / "note.txt").write_text("云端新稿\n", encoding="utf-8")
            local.write_text("我的下一稿\n", encoding="utf-8")
            self.invoke("publish", "--notify", "--", local, expect=1)
            self.assertTrue(message.call_args.kwargs["error"])
            self.assertIn("取回", message.call_args.args[0])
            fork = json.loads(self.invoke("refresh", "--notify", "--", local)[0])
            show.assert_called_once_with(fork["remoteCopy"])
            self.assertEqual(Path(fork["remoteCopy"]).read_text(), "云端新稿\n")
            self.assertEqual(local.read_text(), "我的下一稿\n")

    def test_relink_without_configuration_does_not_register_actions(self):
        self.invoke("relink")
        self.assertFalse((self.root / "data").exists())
        self.invoke("integrate", "--machine", "m_fixture", "--project", "/", expect=1)
        self.assertFalse(native_files.settings_path().exists())
        self.assertEqual(self.calls, [])

    def test_first_start_offers_only_the_activities_entry_without_connecting(self):
        self.invoke("relink", "--offer-setup")
        self.assertTrue((self.root / "data/applications/cloudworkspace-instabox-project-files.desktop").is_file())
        self.assertFalse((self.root / "data/nautilus/scripts").exists())
        self.assertFalse(native_files.settings_path().exists())
        self.assertEqual(self.calls, [])

    def test_cancel_first_launch_leaves_project_unbound(self):
        with patch.object(native_files, "configure", return_value=False) as configure:
            self.invoke("browse", "--notify")
        configure.assert_called_once_with()
        self.assertFalse(native_files.settings_path().exists())
        self.assertEqual(self.calls, [])

    def test_file_manager_uri_keeps_special_characters_out_of_dbus_arguments(self):
        selected = self.root / "note' #?\n.txt"
        with patch.object(native_files, "remember_shown_copy"), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            native_files.show_file(selected)
        args = run.call_args.args[0]
        self.assertIn("org.freedesktop.FileManager1.ShowItems", args)
        self.assertEqual(json.loads(args[-2]), [selected.as_uri()])

    def test_only_successfully_shown_explicit_selections_enter_recent_files(self):
        self.invoke("integrate", "--machine", "m_fixture", "--project", "/data/project")
        with patch.object(native_files, "remember_shown_copy") as remember, patch.object(subprocess, "run") as show:
            local = Path(self.invoke("checkout", "note.txt")[0])
            self.invoke("inspect", local)
            with patch("linux.native_file_picker.choose_file", return_value=None):
                self.invoke("browse")
            remember.assert_not_called()
            show.return_value.returncode = 1
            self.invoke("checkout", "--show", "note.txt", expect=1)
            remember.assert_not_called()
            show.return_value.returncode = 0
            self.invoke("checkout", "--show", "note.txt")
            remember.assert_called_once_with(local)
            with patch("linux.native_file_picker.choose_file", return_value="note.txt"):
                self.invoke("browse")
            self.assertEqual(remember.call_count, 2)
            remember.assert_called_with(local)
            remember.reset_mock()
            with patch.object(native_files, "message"):
                self.invoke("refresh", "--notify", local)
                remember.assert_not_called()
                local.write_text("本地草稿\n")
                (self.cloud / "note.txt").write_text("最新云版本\n")
                notice, _ = self.invoke("refresh", "--notify", local)
            newer = Path(json.loads(notice)["remoteCopy"])
            self.assertNotEqual(newer, local)
            remember.assert_called_once_with(newer)

    def test_file_display_succeeds_without_recent_file_helper(self):
        overrides = {"INSTABOX_API_KEY": "test", "XDG_CONFIG_HOME": "relative", "XDG_DATA_HOME": "/absolute"}
        with patch.dict(os.environ, overrides), patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("GTK", 3)) as call:
            native_files.remember_shown_copy(self.root / "note.txt")
        env = call.call_args.kwargs["env"]
        self.assertNotIn("INSTABOX_API_KEY", env)
        self.assertNotIn("XDG_CONFIG_HOME", env)
        self.assertEqual(env["XDG_DATA_HOME"], "/absolute")
