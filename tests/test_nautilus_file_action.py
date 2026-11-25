"""Exercise the installed Nautilus executable, including hostile-looking literal file names."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "linux" / "install-file-actions.py"
ACTION = "保存到 InstaBox"
REFRESH = "取回 InstaBox 最新版本"
ENTRY_ID = "cloudworkspace-instabox-project-files.desktop"


class NautilusFileAction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.xdg = self.home / "数据"
        self.action = self.xdg / "nautilus" / "scripts" / ACTION
        self.checkout = self.home / "local files"
        self.checkout.mkdir()
        self.bin = self.home / "programs"
        self.bin.mkdir()
        self.python = self.home / 'venv "新"\npath' / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(sys.executable)
        self.client = self.home / '客户端 %F $x\n\' 1.py'
        self.log = self.home / "publish.json"
        self.toast = self.home / "toast.json"
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.home), "XDG_DATA_HOME": str(self.xdg),
            "CLI_LOG": str(self.log), "TOAST_LOG": str(self.toast), "LC_ALL": "C.UTF-8",
        }
        self.client.write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            "Path(os.environ['CLI_LOG']).write_text(json.dumps([sys.executable,*sys.argv]))\n"
            "if os.getenv('PUBLISH_FAILURE'):\n"
            " import subprocess\n subprocess.run(['notify-send','保存到 InstaBox','保存失败：模拟版本冲突'], check=True)\n sys.exit(7)\n"
        )
        notifier = self.bin / "notify-send"
        notifier.write_text("#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n"
                            "Path(os.environ['TOAST_LOG']).write_text(json.dumps(sys.argv[1:]))\n")
        notifier.chmod(0o700)

    def install(self, *more, check=True):
        return subprocess.run([sys.executable, INSTALLER, "--python", self.python, "--client", self.client, *more],
                              env=self.env, capture_output=True, check=check, text=True)

    def select(self, *names, uris="", current=None, action=ACTION, **extra):
        env = {**self.env, "NAUTILUS_SCRIPT_SELECTED_URIS": uris,
               "NAUTILUS_SCRIPT_CURRENT_URI": current or self.checkout.as_uri(), **extra}
        return subprocess.run([self.action.with_name(action), *names], cwd=self.checkout, env=env, capture_output=True, text=True)

    def test_single_local_file_and_cli_failure_use_original_argv_and_venv(self):
        name = '-notes "$(touch pwn)"\n月光 🌙.md'
        path = self.checkout / name
        path.write_text("private fixture")
        self.install()
        self.assertEqual(self.select(name, uris=path.as_uri()+"\n").returncode, 0)
        self.assertEqual(json.loads(self.log.read_text()),
                         [str(self.python), str(self.client), "publish", "--notify", "--", str(path)])
        self.assertFalse((self.checkout/"pwn").exists())
        self.assertEqual(self.select(name, uris=path.as_uri(), action=REFRESH).returncode, 0)
        self.assertEqual(json.loads(self.log.read_text()),
                         [str(self.python), str(self.client), "refresh", "--notify", "--", str(path)])
        self.assertFalse(self.toast.exists())
        result = self.select(str(path), uris=path.as_uri(), PUBLISH_FAILURE="1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("模拟版本冲突", json.loads(self.toast.read_text())[-1])

    def test_remote_multiple_link_dir_and_missing_never_reach_client(self):
        path = self.checkout / "one"
        path.touch()
        (self.checkout/"link").symlink_to(path)
        os.mkfifo(self.checkout/"pipe")
        self.install()
        cases = [
            ([], {"uris":"sftp://example.invalid/a"}, "本机"),
            ([path.name], {"uris":"file://server.invalid/a"}, "本机"),
            ([], {"current":"smb://example.invalid/share"}, "本机"),
            ([path.name, path.name], {}, "只选择一个"),
            ([path.name], {"uris":path.as_uri()+"\n"+path.as_uri()+"\n"}, "只选择一个"),
            ([], {}, "只选择一个"),
            (["link"], {}, "普通文件"), (["."], {}, "普通文件"), (["pipe"], {}, "普通文件"),
            (["missing"], {}, "无法读取"),
        ]
        for names, options, expected in cases:
            with self.subTest(names=names, options=options):
                self.toast.unlink(missing_ok=True)
                self.assertNotEqual(self.select(*names, **options).returncode, 0)
                self.assertIn(expected, json.loads(self.toast.read_text())[-1])
                self.assertFalse(self.log.exists())

    def test_relocated_executable_and_changed_user_actions(self):
        self.install()
        state = self.action.stat()
        self.assertTrue(os.access(self.action, os.X_OK))
        self.install()
        self.assertEqual((self.action.stat().st_ino, self.action.stat().st_mtime_ns),
                         (state.st_ino,state.st_mtime_ns))
        self.assertCountEqual([p.name for p in self.action.parent.iterdir()], [ACTION, REFRESH])
        new = self.home/"新版\n客户端.py"
        shutil.copyfile(self.client, new)
        self.client.unlink()
        (self.checkout/"x").touch()
        self.assertEqual(self.select("x").returncode, 1)
        self.assertIn("重新运行", json.loads(self.toast.read_text())[-1])
        self.client = new
        self.install()
        self.assertEqual(self.select("x").returncode, 0)
        self.assertEqual(json.loads(self.log.read_text())[1], str(new))
        changed = self.action.read_bytes() + b"\n# User customization\n"
        self.action.write_bytes(changed)
        self.assertNotEqual(self.install(check=False).returncode, 0)
        self.assertEqual(self.action.read_bytes(), changed)
        self.action.unlink()
        self.action.symlink_to(new)
        self.assertNotEqual(self.install(check=False).returncode, 0)
        self.assertTrue(self.action.is_symlink())

    def test_does_not_overwrite_preexisting_user_file_or_accept_relative_venv(self):
        self.action.parent.mkdir(parents=True)
        self.action.write_text("my existing script")
        self.assertNotEqual(self.install(check=False).returncode, 0)
        self.assertEqual(self.action.read_text(), "my existing script")
        self.action.unlink()
        self.assertNotEqual(self.install("--python", "./python", check=False).returncode, 0)
        self.assertFalse(self.action.exists())

    def test_browse_is_discoverable_and_preserves_exec_arguments(self):
        self.install()
        entry = self.xdg / "applications" / ENTRY_ID
        self.assertIn("Name=InstaBox Project Files", entry.read_text())
        self.assertIn("Name[zh_CN]=InstaBox 项目文件", entry.read_text())
        self.assertIn("Icon=folder-remote", entry.read_text())
        self.assertIn("Terminal=false", entry.read_text())
        gio = shutil.which("gio")
        validate = shutil.which("desktop-file-validate")
        if not gio or not validate:
            self.skipTest("requires GIO and desktop-file-utils")
        subprocess.run([validate, entry], env=self.env, check=True)
        subprocess.run([gio, "launch", entry], env=self.env, check=True)
        end = time.monotonic() + 5
        while not self.log.exists() and time.monotonic() < end:
            time.sleep(.02)
        self.assertEqual(json.loads(self.log.read_text()),
                         [str(self.python), str(self.client), "browse", "--notify"])

    def test_preflight_preserves_user_desktop_item_and_retry_fills_missing_action(self):
        app = self.xdg / "applications" / ENTRY_ID
        app.parent.mkdir(parents=True)
        app.write_bytes(b"custom launch action")
        self.assertNotEqual(self.install(check=False).returncode, 0)
        self.assertEqual(app.read_bytes(), b"custom launch action")
        self.assertFalse(self.action.exists())
        app.unlink()
        self.install()
        old_stat = self.action.stat()
        self.action.with_name(REFRESH).unlink()
        self.install()
        self.assertEqual(self.action.stat().st_ino, old_stat.st_ino)
        self.assertEqual(self.action.stat().st_mtime_ns, old_stat.st_mtime_ns)
        self.assertTrue(self.action.with_name(REFRESH).is_file())
