"""Install GNOME desktop shortcuts with a temporary XDG home and open them with real GIO."""
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHORTCUTS = ROOT / "linux/desktop-shortcuts.py"
GIO = Path("/usr/bin/gio")


@unittest.skipUnless(sys.platform.startswith("linux") and GIO.is_file(), "requires Linux with GLib and GIO")
class DesktopShortcuts(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.shortcut_script = SHORTCUTS
        self.home = self.base / "user home"
        self.home.mkdir()
        self.data = self.home / "xdg data"
        self.config = self.home / "config"
        self.config.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.system = self.base / "system"
        self.trust = self.base / "trust"
        self.opened = self.base / "opened"
        self.env = {
            "PATH": str(self.bin) + ":/usr/bin:/bin", "LC_ALL": "C.UTF-8",
            "HOME": str(self.home), "XDG_DATA_HOME": str(self.data),
            "XDG_DATA_DIRS": str(self.system), "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.home / "state"), "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_TRUST": str(self.trust), "TEST_OPENED": str(self.opened),
        }
        # Only metadata::trusted needs a real GVfs session. All launches below use /usr/bin/gio.
        self.executable(self.bin / "gio", 'test "$1:$2:$3:$4:$6:$7" = "set:-n:-t:string:metadata::trusted:true" || exit 2\nprintf "%s\\n" "$5" >> "$TEST_TRUST"')

    @staticmethod
    def executable(path, body):
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o700)

    def run_ok(self, *command):
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, (command, result.stderr))
        return result

    def release(self, version):
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if not arch:
            self.skipTest("the Windows launcher supports x64 and arm64")
        bundle = self.base / ("version " + version + " %")
        (bundle / "linux").mkdir(parents=True)
        (bundle / "windows").mkdir()
        script = bundle / "linux/install-app-launchers.py"
        shutil.copyfile(ROOT / "linux/install-app-launchers.py", script)
        self.shortcut_script = bundle / "linux/desktop-shortcuts.py"
        shutil.copyfile(SHORTCUTS, self.shortcut_script)
        self.executable(bundle / "windows" / f"CloudLinuxWorkspace-{arch}.exe",
                        f'printf "%s|%s|%s\\n" "{version}" "$1" "$2" >> "$TEST_OPENED"')
        self.run_ok(sys.executable, script)
        return bundle

    def offer(self):
        self.run_ok(sys.executable, self.shortcut_script)

    def native_claude(self):
        entry = self.system / "applications/com.anthropic.Claude.desktop"
        entry.parent.mkdir(parents=True)
        entry.write_text("[Desktop Entry]\nType=Application\nName=Claude\nExec=claude-desktop %U\nIcon=claude-desktop\n")
        self.executable(self.bin / "claude-desktop", 'printf "linux-claude\\n" >> "$TEST_OPENED"')
        return entry

    def launch(self, shortcut, expected):
        before = self.opened.read_text().splitlines() if self.opened.exists() else []
        self.run_ok(str(GIO), "launch", shortcut)
        for _ in range(100):
            if self.opened.exists() and len(self.opened.read_text().splitlines()) > len(before):
                break
            time.sleep(.02)
        self.assertEqual(self.opened.read_text().splitlines() if self.opened.exists() else [], before + [expected])

    def test_default_icons_reopen_after_the_old_release_is_removed(self):
        old = self.release("one")
        self.offer()
        desktop = self.home / "Desktop"
        chat = desktop / "ChatGPT.desktop"
        claude = desktop / "Claude.desktop"
        self.assertEqual(set(desktop.iterdir()), {chat, claude})
        original = {f: (f.stat().st_ino, f.read_bytes()) for f in (chat, claude)}
        for f in original:
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o700)
        self.assertCountEqual(self.trust.read_text().splitlines(), map(str, original))
        self.launch(chat, "one|--open-app|ChatGPT")
        shutil.rmtree(old)
        self.assertFalse(self.shortcut_script.exists())
        self.release("two")
        self.offer()
        self.assertEqual(original, {f: (f.stat().st_ino, f.read_bytes()) for f in original})
        self.assertCountEqual(self.trust.read_text().splitlines(), map(str, original))
        self.launch(chat, "two|--open-app|ChatGPT")
        self.launch(claude, "two|--open-app|Claude")

    def test_configured_desktop_disabled_or_missing(self):
        self.release("one")
        conf = self.config / "user-dirs.dirs"
        conf.write_text('XDG_DESKTOP_DIR="$HOME/"\n')
        self.offer()
        self.assertFalse((self.home / "Desktop").exists())
        self.assertFalse(self.trust.exists())
        conf.write_text('XDG_DESKTOP_DIR="$HOME/My Desk"\n')
        custom = self.home / "My Desk"
        self.offer()
        self.assertFalse(custom.exists())
        custom.mkdir()
        self.offer()
        self.assertEqual({p.name for p in custom.iterdir()}, {"ChatGPT.desktop", "Claude.desktop"})
        self.assertFalse((self.home / "Desktop").exists())

    def test_existing_files_and_dangling_links_are_preserved(self):
        self.release("one")
        desktop = self.home / "Desktop"
        desktop.mkdir()
        chat = desktop / "ChatGPT.desktop"
        chat.write_bytes(b"existing user shortcut\n")
        claude = desktop / "cloudworkspace-windows-claude.desktop"
        claude.symlink_to("missing-user-entry")
        self.offer()
        self.assertEqual(chat.read_bytes(), b"existing user shortcut\n")
        self.assertEqual(claude.readlink(), Path("missing-user-entry"))
        self.assertEqual(set(desktop.iterdir()), {chat, claude})
        self.assertFalse(self.trust.exists())

    def test_user_move_or_deletion_is_not_reversed(self):
        self.release("one")
        self.offer()
        desktop = self.home / "Desktop"
        moved = self.home / "My Claude.desktop"
        (desktop / "Claude.desktop").rename(moved)
        (desktop / "ChatGPT.desktop").unlink()
        self.offer()
        self.assertEqual(list(desktop.iterdir()), [])
        desktop.rmdir()
        self.offer()
        self.assertFalse(desktop.exists())
        self.launch(moved, "one|--open-app|Claude")

    def test_existing_icon_opens_native_claude_after_installation(self):
        self.release("one")
        self.offer()
        icon = self.home / "Desktop/Claude.desktop"
        original = (icon.stat().st_ino, icon.read_bytes())
        self.launch(icon, "one|--open-app|Claude")
        self.native_claude()
        self.offer()  # The old Windows registration still exists.
        self.launch(icon, "linux-claude")
        self.release("two")  # The launcher installer removes the obsolete Windows entry.
        self.offer()
        self.assertEqual(original, (icon.stat().st_ino, icon.read_bytes()))
        self.assertFalse((self.data / "applications/cloudworkspace-windows-claude.desktop").exists())
        self.launch(icon, "linux-claude")

    def test_only_registered_applications_receive_shortcuts(self):
        desktop = self.home / "Desktop"
        self.offer()
        self.assertFalse(desktop.exists())
        self.assertFalse(self.data.exists())
        self.assertFalse((self.home / "state").exists())
        self.native_claude()
        self.offer()
        self.assertEqual([p.name for p in desktop.iterdir()], ["Claude.desktop"])
        self.launch(desktop / "Claude.desktop", "linux-claude")
