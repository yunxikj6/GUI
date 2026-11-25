import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from linux import claude_project_connection as connection
from workspace_mcp import client_server_config

ROOT = Path(__file__).resolve().parents[1]
NAME = "instabox-workspace"


class ClaudeProjectConnection(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.path = self.base / "Claude" / "claude_desktop_config.json"
        self.path.parent.mkdir()
        self.server = client_server_config(sys.executable, "m_alpha", "/data/project", host="claude")

    def configure(self, server=None, **kwargs):
        return connection.configure(self.server if server is None else server, path=self.path, **kwargs)

    def write(self, text):
        self.path.write_bytes(text.encode("utf-8"))

    def config(self):
        return json.loads(self.path.read_bytes())["mcpServers"][NAME]

    def test_default_path_xdg_then_home_and_private_first_creation(self):
        home, xdg = self.base / "home", self.base / "profile"
        with patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}):
            self.assertTrue(connection.configure(self.server))
            self.assertEqual(connection.config_path(), xdg / "Claude/claude_desktop_config.json")
            self.assertEqual(stat.S_IMODE(connection.config_path().stat().st_mode), 0o600)
            self.assertEqual(json.loads(connection.config_path().read_bytes())["mcpServers"], {NAME: self.server})
        for relative in ("", "relative"):
            with patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": relative}):
                self.assertEqual(connection.config_path(), home / ".config/Claude/claude_desktop_config.json")

    def test_preserves_unrelated_json_bytes_duplicates_and_precision(self):
        prefix = '\ufeff{\r\n  "repeat": 3, "repeat": [2.123456789012345678901, 1e9999],\r\n  '
        inner = '"mcpServers":{"the\\u006de":{"env":{"foo": "旧", "foo": "重"}}, "trailer": 1}'
        suffix = ',\r\n  "name":"\\ud83d\\ude80 / abc"\r\n}\r\n'
        self.write(prefix + inner + suffix)
        os.chmod(self.path, 0o640)
        self.assertTrue(self.configure())
        raw = self.path.read_bytes().decode("utf-8")
        self.assertTrue(raw.startswith(prefix + inner[:-1] + ", "))
        self.assertTrue(raw.endswith("}" + suffix))
        self.assertEqual(self.config(), self.server)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        before = self.path.stat()
        self.assertFalse(self.configure())
        self.assertEqual(self.path.stat().st_ino, before.st_ino)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.path.read_bytes(), raw.encode())

    def test_adds_to_existing_empty_or_missing_server_group(self):
        for text in ('{}', '{"mcpServers":{ \n}}', '{"theme":"light"}', ' { "theme": {} } '):
            with self.subTest(text=text):
                self.write(text)
                self.assertTrue(self.configure())
                self.assertEqual(self.config(), self.server)
                current = json.loads(self.path.read_text())
                if 'theme' in json.loads(text):
                    self.assertEqual(current['theme'], json.loads(text)['theme'])

    def test_rebinds_exact_real_previous_manual_readonly_entry(self):
        command = [sys.executable, str(ROOT / "workspace_mcp.py"), "--machine", "m_old",
                   "--project", "/data/中文 old", "--print-config"]
        manual = json.loads(subprocess.check_output(command))
        self.write(json.dumps(manual))
        self.assertFalse(self.configure(manual["mcpServers"][NAME]))
        self.assertTrue(self.configure())
        self.assertEqual(self.config(), self.server)

    def test_old_install_must_match_saved_entry_exactly(self):
        old = {"command": "/old/venv/python", "args": ["/old/checkout/workspace_mcp.py",
               "--machine", "m_old", "--project", "/old", "--credential-source", "system"]}
        content = {"mcpServers": {NAME: old}, "theme": "dark"}
        self.write(json.dumps(content))
        with self.assertRaisesRegex(ValueError, "同名"):
            self.configure()
        self.assertTrue(self.configure(previous=old))
        self.assertEqual(json.loads(self.path.read_text()), {"mcpServers": {NAME: self.server}, "theme": "dark"})
        for changed in (dict(old, env={"private": "value"}), dict(old, args=[*old["args"], "--allow-save"])):
            self.write(json.dumps({"mcpServers": {NAME: changed}}))
            before = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "同名"):
                self.configure(previous=old)
            self.assertEqual(before, self.path.read_bytes())

    def test_rejects_ambiguous_managed_keys_or_invalid_root_without_rewriting(self):
        documents = ['{} {}', '[]', '{"mcpServers":null}', '{"mcpServers":{},"mcpServers":{}}',
                     '{"mcpServers":{"instabox-workspace":{},"instabox-workspace":{}}}',
                     '{"mcpServers":{"instabox-workspace":{"args":[],"args":[]}}}',
                     '{"mcpServers":{"instabox-workspace":null}}', '{"token": NaN}', '']
        for document in documents:
            with self.subTest(document=document):
                self.write(document)
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual(self.path.read_text(), document)

    def test_does_not_write_links_or_bad_server(self):
        target = self.base / "target"
        target.write_text('{"theme":1}')
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.configure()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), '{"theme":1}')
        self.path.unlink()
        for server in ({}, {"args": []}, dict(self.server, env={"TOKEN": "secret"}),
                       dict(self.server, args=[*self.server["args"], "--allow-save"])):
            with self.subTest(server=server), self.assertRaises(ValueError):
                self.configure(server)
        self.assertFalse(self.path.exists())

    def test_create_preserves_an_editor_who_creates_the_file_first(self):
        real_link = os.link
        def create_first(*args, **kwargs):
            self.write('{"new":"editor"}')
            return real_link(*args, **kwargs)
        with patch.object(connection.os, "link", side_effect=create_first), self.assertRaisesRegex(ValueError, "改变"):
            self.configure()
        self.assertEqual(self.path.read_text(), '{"new":"editor"}')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_intervening_replacements_even_during_recovery_leave_the_newest_save(self):
        real_exchange = connection._exchange
        for saves in (1, 2, 3):
            self.write('{"theme":0}')
            count = 0
            def racing_exchange(*args):
                nonlocal count
                count += 1
                if count <= saves:
                    editor = ("from pathlib import Path; import sys; "
                              "tmp = Path(sys.argv[1]); tmp.write_text(sys.argv[3]); tmp.replace(sys.argv[2])")
                    subprocess.run([sys.executable, "-c", editor, self.base / "editor-save",
                                    self.path, '{"theme":%d}' % count], check=True)
                return real_exchange(*args)
            with self.subTest(saves=saves), patch.object(connection, "_exchange", side_effect=racing_exchange):
                with self.assertRaisesRegex(ValueError, "改变"):
                    self.configure()
                self.assertEqual(self.path.read_text(), '{"theme":%d}' % saves)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_detects_changed_bytes_with_identical_inode_mtime_and_length(self):
        self.write('{"v":1}')
        first = self.path.stat()
        real_exchange = connection._exchange
        def edit(*args):
            if self.path.stat().st_ino == first.st_ino:
                self.write('{"v":2}')
                os.utime(self.path, ns=(first.st_atime_ns, first.st_mtime_ns))
            return real_exchange(*args)
        with patch.object(connection, "_exchange", side_effect=edit), self.assertRaisesRegex(ValueError, "改变"):
            self.configure()
        self.assertEqual(self.path.read_text(), '{"v":2}')

    def test_edit_after_our_exchange_keeps_both_external_versions(self):
        self.write('{"before":1}')
        real_exchange = connection._exchange
        def save_twice(*args):
            other = self.base / "other"
            other.write_text('{"during":2}')
            other.replace(self.path)
            real_exchange(*args)
            other.write_text('{"after":3}')
            other.replace(self.path)

        with patch.object(connection, "_exchange", side_effect=save_twice), self.assertRaisesRegex(ValueError, "保存在"):
            self.configure()
        self.assertEqual(self.path.read_text(), '{"after":3}')
        saved, = self.path.parent.glob(".cloudworkspace-claude-*")
        self.assertEqual(saved.read_text(), '{"during":2}')

    def test_unsupported_filesystem_preserves_original_and_explains_manual_merge(self):
        self.write('{"theme": 9}')
        with patch.object(connection.ctypes, "CDLL", return_value=object()):
            with self.assertRaisesRegex(ValueError, "编辑配置"):
                self.configure()
        self.assertEqual(self.path.read_text(), '{"theme": 9}')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
