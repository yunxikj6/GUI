"""Exercise the short-lived GTK helper with a private display and empty user settings."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "linux/recent_file.py"
PYTHON = "/usr/bin/python3"


def check_private_chooser_history():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    base = Path(os.environ["XDG_DATA_HOME"]) / "cloudworkspace/file-copies"
    base.mkdir(parents=True)
    selected = [base / f"计划 #{n}%.txt" for n in range(4)]
    for path in selected:
        path.write_text("工作副本\n", encoding="utf-8")
    processes = [subprocess.Popen([PYTHON, "-I", str(HELPER), str(path)]) for path in selected]
    assert all(process.wait(timeout=8) == 0 for process in processes)

    def uris():
        return {item.get_uri() for item in Gtk.RecentManager.new().get_items()}

    expected = {path.as_uri() for path in selected}
    assert uris() == expected
    for key, disabled in (("remember-recent-files", "false"), ("recent-files-max-age", "0")):
        subprocess.run(["gsettings", "set", "org.gnome.desktop.privacy", key, disabled], check=True)
        other = base / f"{key}.txt"
        other.touch()
        subprocess.run([PYTHON, "-I", str(HELPER), str(other)], check=True, timeout=8)
        subprocess.run(["gsettings", "reset", "org.gnome.desktop.privacy", key], check=True)
        assert uris() == expected


class RecentFile(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xvfb-run") and shutil.which("gsettings") and Path(PYTHON).is_file(),
                         "requires the system GTK test display")
    def test_distinct_short_processes_and_desktop_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {"PATH": os.environ["PATH"], "HOME": directory, "XDG_DATA_HOME": str(home / ".local/share"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "GSETTINGS_BACKEND": "keyfile", "LANG": "C.UTF-8"}
            probe = subprocess.run([PYTHON, "-I", "-c", "import gi; gi.require_version('Gtk', '3.0')"],
                                   env=env, capture_output=True)
            if probe.returncode:
                self.skipTest("requires GTK 3 introspection")
            result = subprocess.run(["xvfb-run", "-a", PYTHON, "-I", __file__, "--private-display"],
                                    env=env, capture_output=True, text=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["--private-display"]:
        check_private_chooser_history()
    else:
        unittest.main()
