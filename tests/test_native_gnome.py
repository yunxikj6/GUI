import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_gnome", ROOT / "linux/gnome_session.py")
gnome = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gnome)


class NativeGnome(unittest.TestCase):
    def test_optional_shortcuts_run_on_the_new_bus_before_shell_even_if_setup_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mode = root / "data/gnome-shell/modes/ubuntu.json"
            mode.parent.mkdir(parents=True)
            mode.write_text("{}")
            check_order = root / "steps"
            for script in ("gnome_session.py", "start-gnome.sh"):
                shutil.copy(ROOT / "linux" / script, root / script)
            (root / "desktop-shortcuts.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['STEPS']).write_text(os.environ['DBUS_SESSION_BUS_ADDRESS'] + '\\n')\n"
                "raise SystemExit(3)\n")
            for name, contents in {
                "xdpyinfo": "exit 0", "gdbus": "exit 0",
                "dbus-run-session": 'shift; export DBUS_SESSION_BUS_ADDRESS=private-bus; exec "$@"',
                "gnome-shell": """case "$1" in
  --help-all) echo '--nested --wayland-display' ;;
  --version) echo 'GNOME Shell fixture' ;;
  *) echo shell >> "$STEPS" ;;
esac""",
            }.items():
                command = root / name
                command.write_text("#!/bin/sh\n" + contents + "\n")
                command.chmod(0o700)
            env = {**os.environ, "PATH": f"{root}:/usr/bin:/bin", "DISPLAY": ":123", "HOME": str(root),
                   "XDG_RUNTIME_DIR": str(root), "XDG_DATA_DIRS": str(root / "data"), "STEPS": str(check_order)}
            start = [str(root / "start-gnome.sh"), "--desktop-shortcuts"]
            subprocess.run([*start, "--check"], env=env, capture_output=True, check=True)
            self.assertFalse(check_order.exists())
            launched = subprocess.run(start, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(check_order.read_text().splitlines(), ["private-bus", "shell"])
            self.assertIn("图标", launched.stderr)

    def test_backend_never_selects_a_login_screen_or_an_unavailable_parent(self):
        self.assertEqual(gnome.select_backend({"--nested", "--devkit"}, True, True), "--nested")
        self.assertEqual(gnome.select_backend({"--nested", "--devkit"}, False, True), "--devkit")
        self.assertEqual(gnome.select_backend({"--devkit"}, True, False), "--devkit")
        for flags, x, wayland in [({"--nested"}, False, True), ({"--devkit"}, False, False),
                                  ({"--display-server", "--headless"}, True, True)]:
            with self.subTest(flags=flags), self.assertRaises(gnome.LaunchError):
                gnome.select_backend(flags, x, wayland)

    def test_mode_comes_from_the_installed_session(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_DATA_DIRS":root}):
            self.assertEqual(gnome.session_mode(), "user")
            mode = Path(root) / "gnome-shell/modes/ubuntu.json"
            mode.parent.mkdir(parents=True)
            mode.write_text('{}')
            self.assertEqual(gnome.session_mode(), "ubuntu")

    def test_check_does_not_start_a_compositor_and_shell_errors_propagate(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name, contents in {
                "xdpyinfo": "exit 0",
                "gdbus": "exit 0",
                "gnome-shell": """case "$1" in
  --help-all) echo '--wayland --nested --wayland-display' ;;
  --version) echo 'GNOME Shell fixture' ;;
  *) echo 'unexpected execution'; exit 7 ;;
esac""",
                "dbus-run-session": 'printf "%s" "$WAYLAND_DISPLAY" > "$HANDSHAKE"; shift; exec "$@"',
            }.items():
                script = root / name
                script.write_text("#!/bin/sh\n" + contents + "\n")
                script.chmod(0o700)
            checkpoint = root / "started"
            env = {**os.environ, "PATH":f"{root}:/usr/bin:/bin", "DISPLAY":":123",
                   "XDG_RUNTIME_DIR":str(root), "XDG_DATA_DIRS":str(root), "HANDSHAKE":str(checkpoint)}
            entry = str(ROOT / "linux/start-gnome.sh")
            checked = subprocess.run([entry, "--check"], env=env, capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertFalse(checkpoint.exists())
            launched = subprocess.run([entry], env=env, capture_output=True)
            self.assertEqual(launched.returncode, 7, launched.stderr)
            self.assertEqual(Path(checkpoint.read_text()).name, "wayland-0")
            self.assertFalse(Path(checkpoint.read_text()).parent.exists())
