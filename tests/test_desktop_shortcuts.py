"""Install GNOME desktop shortcuts with a temporary XDG home and open them with real GIO."""
import configparser
import os
from pathlib import Path
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

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
        self.setup_icon = self.system / "icons/Yaru/256x256/apps/system-software-install.png"
        self.setup_icon.parent.mkdir(parents=True)
        self.setup_icon.write_bytes(self.png(b"\x45\x65\x45"))
        self.env = {
            "PATH": str(self.bin) + ":/usr/bin:/bin", "LC_ALL": "C.UTF-8",
            "HOME": str(self.home), "XDG_DATA_HOME": str(self.data),
            "XDG_DATA_DIRS": str(self.system), "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.home / "state"), "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_TRUST": str(self.trust), "TEST_OPENED": str(self.opened),
        }
        # Only metadata::trusted needs a real GVfs session. All launches below use /usr/bin/gio.
        self.executable(self.bin / "gio", 'test "$1:$2:$3:$4:$6:$7" = "set:-n:-t:string:metadata::trusted:true" || exit 2\nprintf "%s\\n" "$5" >> "$TEST_TRUST"')

        # GIO must route Terminal=true to a terminal, even without a graphical session.
        self.executable(self.bin / "xdg-terminal-exec",
                        'case "$1" in --|-e|-x) shift ;; esac\nexec "$@"')

    @staticmethod
    def executable(path, body):
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o700)

    def run_ok(self, *command):
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, (command, result.stderr))
        return result

    def release(self, version, *, windows=True):
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if windows and not arch:
            self.skipTest("the Windows launcher supports x64 and arm64")
        bundle = self.base / ("version " + version + " %")
        (bundle / "linux").mkdir(parents=True)
        (bundle / "windows").mkdir()
        script = bundle / "linux/install-app-launchers.py"
        shutil.copyfile(ROOT / "linux/install-app-launchers.py", script)
        self.shortcut_script = bundle / "linux/desktop-shortcuts.py"
        shutil.copyfile(SHORTCUTS, self.shortcut_script)
        self.executable(bundle / "linux/setup-vendor-apps.sh",
                        f'printf "%s|%s|%s\\n" "{version}" "$1" "$2" >> "$TEST_OPENED"\nexit 130')
        if not windows:
            return bundle
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

    def native_chatgpt(self):
        entry = self.system / "applications/chatgpt.desktop"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("[Desktop Entry]\nType=Application\nName=ChatGPT\nExec=chatgpt %U\nIcon=chatgpt\n")
        self.executable(self.bin / "chatgpt", 'printf "linux-chatgpt\\n" >> "$TEST_OPENED"')
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
        self.launch(chat, "one|--app|chatgpt")
        self.launch(self.data / "applications/cloudworkspace-windows-chatgpt.desktop", "one|--open-app|ChatGPT")
        shutil.rmtree(old)
        self.assertFalse(self.shortcut_script.exists())
        self.release("two")
        self.offer()
        self.assertEqual(original, {f: (f.stat().st_ino, f.read_bytes()) for f in original})
        self.assertCountEqual(self.trust.read_text().splitlines(), map(str, original))
        self.launch(chat, "two|--app|chatgpt")
        self.launch(claude, "two|--app|claude")

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
        self.launch(moved, "one|--app|claude")

    def test_existing_icon_opens_native_claude_after_installation(self):
        self.release("one")
        self.offer()
        icon = self.home / "Desktop/Claude.desktop"
        original = (icon.stat().st_ino, icon.read_bytes())
        self.launch(icon, "one|--app|claude")
        self.native_claude()
        self.offer()  # The old Windows registration still exists.
        self.launch(icon, "linux-claude")
        self.release("two")  # The launcher installer removes the obsolete Windows entry.
        self.offer()
        self.assertEqual(original, (icon.stat().st_ino, icon.read_bytes()))
        self.assertFalse((self.data / "applications/cloudworkspace-windows-claude.desktop").exists())
        self.launch(icon, "linux-claude")

    def test_old_windows_alias_migrates_without_changing_the_trusted_file(self):
        self.release("one")
        self.offer()
        app = self.home / "Desktop/ChatGPT.desktop"
        alias = self.data / "cloudworkspace/desktop-shortcuts/ChatGPT.desktop"
        # Layout created by releases where the desktop icon fell back to Windows.
        alias.unlink()
        alias.symlink_to(self.data / "applications/cloudworkspace-windows-chatgpt.desktop")
        identity = app.stat().st_ino, app.read_bytes(), self.trust.read_bytes()
        self.launch(app, "one|--open-app|ChatGPT")
        self.offer()
        self.assertEqual((app.stat().st_ino, app.read_bytes(), self.trust.read_bytes()), identity)
        self.launch(app, "one|--app|chatgpt")

    def test_custom_installer_id_preserved_without_falling_back_to_old_windows_alias(self):
        self.release("one")
        self.offer()
        app = self.home / "Desktop/ChatGPT.desktop"
        alias = self.data / "cloudworkspace/desktop-shortcuts/ChatGPT.desktop"
        windows = self.data / "applications/cloudworkspace-windows-chatgpt.desktop"
        installer = alias.readlink()
        alias.unlink()
        alias.symlink_to(windows)
        installer.write_text("[Desktop Entry]\nType=Application\nName=Personal\nExec=/usr/bin/true\n")
        original = installer.read_bytes(), app.read_bytes(), app.stat().st_ino, self.trust.read_bytes()
        self.offer()
        self.assertFalse(os.path.lexists(alias))
        self.assertEqual((installer.read_bytes(), app.read_bytes(), app.stat().st_ino, self.trust.read_bytes()), original)
        self.launch(windows, "one|--open-app|ChatGPT")
        self.launch(self.home / "Desktop/Claude.desktop", "one|--app|claude")
        installer.unlink()
        self.offer()
        self.launch(app, "one|--app|chatgpt")

    def test_fresh_ubuntu_respects_a_hidden_native_registration(self):
        self.release("linux", windows=False)
        self.native_claude()
        override = self.data / "applications/com.anthropic.Claude.desktop"
        override.parent.mkdir(parents=True)
        override.write_text("[Desktop Entry]\nHidden=true\n")
        self.offer()
        self.assertEqual([p.name for p in (self.home / "Desktop").iterdir()], ["ChatGPT.desktop"])
        self.assertEqual(override.read_text(), "[Desktop Entry]\nHidden=true\n")

    def test_fresh_ubuntu_offers_app_specific_installers_and_prefers_the_installed_client(self):
        desktop = self.home / "Desktop"
        self.release("linux", windows=False)
        self.offer()
        self.assertEqual({p.name for p in desktop.iterdir()}, {"ChatGPT.desktop", "Claude.desktop"})
        self.assertFalse(list((self.data / "applications").glob("cloudworkspace-windows-*.desktop")))
        self.assertFalse(self.opened.exists())
        for app in ("ChatGPT", "Claude"):
            shortcut = desktop / (app + ".desktop")
            self.launch(shortcut, "linux|--app|" + app.lower())
            self.assertEqual(Path(self.desktop_icon(shortcut)).read_bytes(), self.setup_icon.read_bytes())
        self.native_claude()
        self.offer()
        self.launch(desktop / "Claude.desktop", "linux-claude")
        self.launch(desktop / "ChatGPT.desktop", "linux|--app|chatgpt")

    def test_both_native_packages_replace_windows_shortcuts_and_honor_hidden_user_entry(self):
        self.release("one")
        self.offer()
        desktop = self.home / "Desktop"
        icon = desktop / "ChatGPT.desktop"
        initial = icon.stat().st_ino, icon.read_bytes()
        self.native_claude()
        self.native_chatgpt()
        self.release("two")
        self.offer()
        self.launch(icon, "linux-chatgpt")
        self.launch(desktop / "Claude.desktop", "linux-claude")
        self.assertEqual(initial, (icon.stat().st_ino, icon.read_bytes()))
        self.assertFalse(list((self.data / "applications").glob("cloudworkspace-windows-*.desktop")))
        override = self.data / "applications/chatgpt.desktop"
        override.write_text("[Desktop Entry]\nHidden=true\n")
        self.release("three")
        self.offer()
        alias = self.data / "cloudworkspace/desktop-shortcuts/ChatGPT.desktop"
        self.assertEqual(alias.readlink().name, "cloudworkspace-install-chatgpt.desktop")
        self.assertIn("NoDisplay=true", alias.read_text())
        self.assertEqual(override.read_text(), "[Desktop Entry]\nHidden=true\n")

    @staticmethod
    def desktop_icon(path):
        entry = configparser.ConfigParser(interpolation=None)
        entry.read(path, encoding="utf-8")
        return entry["Desktop Entry"]["Icon"]

    def change_icon(self, source, icon):
        old = self.desktop_icon(source)
        source.write_text(source.read_text().replace(f"Icon={old}\n", f"Icon={icon}\n"))

    @staticmethod
    def png(color):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        size = 512
        header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
        pixels = zlib.compress((b"\0" + color * size) * size)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")

    def test_new_shortcut_tracks_real_images_without_editing_desktop_or_trust(self):
        self.release("one")
        self.offer()
        file = self.home / "Desktop/Claude.desktop"
        original = file.read_bytes(), file.stat().st_ino
        trust = self.trust.read_bytes()
        cached = Path(self.desktop_icon(file))
        self.assertTrue(cached.is_absolute() and cached.is_file())
        fallback = cached.read_bytes()
        vendor = self.base / "new artwork.png"
        artwork = self.png(b"\xff\x00\x00")
        vendor.write_bytes(artwork)
        self.change_icon(self.data / "applications/cloudworkspace-windows-claude.desktop", vendor)
        self.offer()
        self.assertEqual(cached.read_bytes(), fallback)
        self.assertEqual(fallback, self.setup_icon.read_bytes())
        same_image = cached.stat().st_ino
        self.offer()
        self.assertEqual(cached.stat().st_ino, same_image)
        self.launch(file, "one|--app|claude")
        vendor_native = self.native_claude()
        native_image = self.base / "linux image.png"
        native_image.write_bytes(self.png(b"\x00\xff\x00"))
        self.change_icon(vendor_native, native_image)
        self.offer()
        self.assertEqual(cached.read_bytes(), native_image.read_bytes())
        self.assertNotEqual(cached.read_bytes(), fallback)
        self.assertEqual((file.read_bytes(), file.stat().st_ino), original)
        self.assertEqual(self.trust.read_bytes(), trust)
        self.launch(file, "linux-claude")
        vendor_native.unlink()
        self.offer()
        self.assertEqual(cached.read_bytes(), self.setup_icon.read_bytes())
        self.launch(file, "one|--app|claude")

    def test_legacy_literal_icon_is_preserved_and_its_target_still_updates(self):
        self.release("one")
        self.offer()
        file = self.home / "Desktop/Claude.desktop"
        cached = Path(self.desktop_icon(file))
        self.change_icon(file, "application-x-executable")
        cached.unlink()  # The previous format had only an installed marker and a literal Icon.
        original = file.read_bytes(), file.stat().st_ino
        trust = self.trust.read_bytes()
        self.native_claude()
        self.offer()
        self.assertEqual((file.read_bytes(), file.stat().st_ino), original)
        self.assertFalse(cached.exists())
        self.assertEqual(self.trust.read_bytes(), trust)
        self.launch(file, "linux-claude")

    def test_foreign_cache_and_user_custom_icon_are_preserved(self):
        self.release("one")
        self.offer()
        desktop = self.home / "Desktop/Claude.desktop"
        cached = Path(self.desktop_icon(desktop))
        self.change_icon(desktop, "personal-icon")
        native = self.native_claude()
        picture = self.base / "different.svg"
        picture.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.change_icon(native, picture)
        cached.write_bytes(b"my image")
        self.offer()
        self.assertEqual(cached.read_bytes(), b"my image")
        self.assertEqual(self.desktop_icon(desktop), "personal-icon")
        cached.unlink()
        cached.symlink_to(picture)
        self.offer()
        self.assertTrue(cached.is_symlink())
        self.assertEqual(cached.readlink(), picture)
        other = self.home / "Desktop/ChatGPT.desktop"
        artwork = Path(self.desktop_icon(other))
        replacement = artwork.with_name("kept backup")
        shutil.copy2(artwork, replacement)  # Copy mode and xattrs, but this is a new inode.
        original = replacement.read_bytes()
        replacement.replace(artwork)
        native_art = self.base / "native app.png"
        native_art.write_bytes(self.png(b"\xab\xcd\xef"))
        self.change_icon(self.native_chatgpt(), native_art)
        self.offer()
        self.assertEqual(artwork.read_bytes(), original)

    def test_gnome_loads_named_vendor_art_at_hidpi_without_a_parent_display(self):
        python = "/usr/bin/python3"
        probe = subprocess.run([python, "-c", "import gi; gi.require_version('Gtk', '3.0')"],
                               env=self.env, capture_output=True)
        if probe.returncode:
            self.skipTest("requires GNOME Gtk introspection")
        self.env["XDG_DATA_DIRS"] += ":/usr/share"  # distro GIO MIME registry and pixbuf loaders
        self.native_claude()
        icon = self.system / "pixmaps/claude-desktop.svg"
        icon.parent.mkdir()
        icon.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512">'
                        '<rect width="512" height="512" fill="blue"/></svg>')
        self.run_ok(python, self.shortcut_script)
        desktop = self.home / "Desktop/Claude.desktop"
        cached = Path(self.desktop_icon(desktop))
        self.assertEqual(cached.suffix, ".png")
        self.assertEqual(struct.unpack(">II", cached.read_bytes()[16:24]), (512, 512))
        # Use the GIO/Gtk loading path used by DING, including a 2x monitor.
        self.run_ok(python, "-c", """import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk
icon = Gio.DesktopAppInfo.new_from_filename(sys.argv[1]).get_icon()
pixbuf = Gtk.IconTheme.new().lookup_by_gicon_for_scale(icon, 128, 2, Gtk.IconLookupFlags.FORCE_SIZE).load_icon()
assert (pixbuf.get_width(), pixbuf.get_height()) == (256, 256)
assert bytes(pixbuf.get_pixels()[:3]) == bytes((0, 0, 255))
""", str(desktop))

    def test_official_package_png_used_even_without_python_gi(self):
        self.native_claude()
        icon = self.system / "icons/hicolor/256x256/apps/claude-desktop.png"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(self.png(b"\x00\xa3\x78"))
        self.run_ok(sys.executable, "-S", self.shortcut_script)
        desktop = self.home / "Desktop/Claude.desktop"
        cached = Path(self.desktop_icon(desktop))
        self.assertEqual(cached.read_bytes(), icon.read_bytes())
        self.launch(desktop, "linux-claude")
