import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "linux/nested_window.py"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


window = load_module("nested_window", HELPER)
session = load_module("gnome_session", ROOT / "linux/gnome_session.py")


class NestedWindowTitle(unittest.TestCase):
    def test_only_a_complete_lowercase_identifier_changes_the_title(self):
        token = "0123456789abcdef" * 2
        self.assertEqual(window.window_title(token), f"Cloud Linux Workspace [{token}]")
        for invalid in (None, "", token[:31], token + "f", token.upper(), token + "\n",
                        "../" + token, "Ａ" * 32, "0" * 31 + "g"):
            with self.subTest(invalid=invalid):
                self.assertEqual(window.window_title(invalid), "Cloud Linux Workspace")

    def test_platform_gate(self):
        self.assertTrue(session.nested_x11_46("GNOME Shell 46.0", "--nested"))
        for version, backend in (("GNOME Shell 46.0", "--devkit"), ("GNOME Shell 47", "--nested"),
                                 ("GNOME Shell 460", "--nested"), ("GNOME Shell 46beta", "--nested")):
            self.assertFalse(session.nested_x11_46(version, backend))

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux process identity")
    def test_same_process_exec_keeps_binary_and_kernel_name_and_can_reexec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = "a8" * 16
            alias = root / f"Cloud Linux Workspace [{token}]"
            pid_file = Path("/proc")
            sleep = Path("/bin/sleep").resolve()
            command = [sys.executable, str(HELPER), temporary, str(sleep), "30"]
            with subprocess.Popen(command, env=dict(os.environ, CLOUDWORKSPACE_VERIFY_ID=token)) as proc:
                try:
                    identity = pid_file / str(proc.pid)
                    limit = time.monotonic() + 4
                    while (identity / "exe").resolve() != sleep and time.monotonic() < limit:
                        time.sleep(0.01)
                    self.assertEqual((identity / "exe").resolve(), sleep)
                    self.assertEqual((identity / "comm").read_text().strip(), "sleep")
                    self.assertEqual((identity / "cmdline").read_bytes().split(b"\0")[:2],
                                     [os.fsencode(alias), b"30"])
                    self.assertTrue(alias.samefile(sleep))
                    self.assertIn(os.fsencode(f"CLOUDWORKSPACE_VERIFY_ID={token}"),
                                  (identity / "environ").read_bytes().split(b"\0"))
                    subprocess.run([str(alias), "0"], check=True, timeout=3)
                finally:
                    proc.terminate()

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux executables")
    def test_alias_failure_still_starts_original_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            alias = Path(temporary) / "Cloud Linux Workspace"
            alias.write_text("already exists")
            result = subprocess.run([sys.executable, str(HELPER), temporary, "/bin/true"],
                                    env=dict(os.environ, CLOUDWORKSPACE_VERIFY_ID="invalid"), timeout=3)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(alias.read_text(), "already exists")
