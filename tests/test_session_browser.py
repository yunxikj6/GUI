"""Resolve URLs with real GIO and temporary desktop registrations."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from linux.gnome_session import configure_browser


@unittest.skipUnless(shutil.which("gio") and shutil.which("update-desktop-database"), "requires GIO desktop handlers")
class BrowserPreferences(unittest.TestCase):
    def setUp(self):
        python = Path("/usr/bin/python3")
        if not python.is_file() or subprocess.run([python, "-I", "-c", "from gi.repository import Gio"],
                                                  capture_output=True).returncode:
            self.skipTest("requires the distribution's Python GIO binding")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = {"PATH": os.defpath, "LC_ALL": "C", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CACHE_HOME"):
            path = self.root / key
            path.mkdir()
            self.env[key] = str(path)
        self.apps = Path(self.env["XDG_DATA_DIRS"]) / "applications"
        self.apps.mkdir()
        self.preferences = Path(self.env["XDG_CONFIG_HOME"]) / "mimeapps.list"

    def install(self, app_id, category, *schemes):
        mimes = ";".join("x-scheme-handler/" + scheme for scheme in schemes)
        (self.apps / app_id).write_text(f"[Desktop Entry]\nName=Fixture\nType=Application\nExec=/bin/true %U\n"
                                        f"Categories={category};\nMimeType={mimes};\n")
        subprocess.run(["update-desktop-database", self.apps], env=self.env, check=True)

    def selected(self, scheme):
        output = subprocess.check_output(["gio", "mime", "x-scheme-handler/" + scheme], env=self.env, text=True)
        return output.splitlines()[0].partition(": ")[2]

    def test_corrects_only_implicit_vendor_handler_and_retains_both_callback_schemes(self):
        self.install("chatgpt.desktop", "Utility", "http", "https", "codex")
        self.install("com.anthropic.Claude.desktop", "Utility", "claude")
        self.install("chromium.desktop", "Network;WebBrowser", "http", "https")
        self.install("my-private-opener.desktop", "Utility", "http")
        before = ("[Default Applications]\nx-scheme-handler/http=my-private-opener.desktop\n"
                  "[Added Associations]\nx-scheme-handler/https=chatgpt.desktop;chromium.desktop;\n")
        self.preferences.write_text(before)
        self.assertEqual(self.selected("https"), "chatgpt.desktop")
        self.env.update(KDE_FULL_SESSION="true", DESKTOP_SESSION="plasma")
        configure_browser(self.env, self.root)
        self.assertNotIn("KDE_FULL_SESSION", self.env)
        self.assertEqual(self.env["DESKTOP_SESSION"], "gnome")
        original, overlay = self.env["XDG_DATA_DIRS"].split(":")
        self.assertEqual(Path(original), self.apps.parent)
        defaults = (Path(overlay) / "applications/mimeapps.list").read_text()
        self.assertEqual(defaults.splitlines(), ["[Default Applications]", "x-scheme-handler/https=chromium.desktop;"])
        self.assertEqual(self.selected("http"), "my-private-opener.desktop")
        self.assertEqual(self.selected("https"), "chromium.desktop")
        self.assertEqual(self.selected("codex"), "chatgpt.desktop")
        self.assertEqual(self.selected("claude"), "com.anthropic.Claude.desktop")
        self.assertEqual(self.preferences.read_text(), before)

        # Both current and legacy user defaults still win, including an explicit vendor choice.
        for location in (self.preferences, Path(self.env["XDG_DATA_HOME"]) / "applications/mimeapps.list"):
            with self.subTest(location=location):
                self.preferences.unlink(missing_ok=True)
                location.parent.mkdir(exist_ok=True)
                location.write_text("[Default Applications]\nx-scheme-handler/https=chatgpt.desktop\n")
                self.assertEqual(self.selected("https"), "chatgpt.desktop")

    def test_absent_browser_and_unavailable_discovery_leave_desktop_usable(self):
        self.install("chatgpt.desktop", "Utility", "http", "https")
        original = self.env["XDG_DATA_DIRS"]
        configure_browser(self.env, self.root)
        self.assertEqual(self.env["XDG_DATA_DIRS"], original)
        self.assertFalse((self.root / "browser-data").exists())
        with patch("linux.gnome_session.subprocess.run", side_effect=OSError):
            configure_browser(self.env, self.root)
        self.assertEqual(self.env["XDG_DATA_DIRS"], original)
        self.assertFalse(self.preferences.exists())
