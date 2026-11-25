"""Exercise real GIO activation, native interop argv, and optional registered app icons."""
import base64
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "linux" / "install-app-launchers.py"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jqRgAAAAASUVORK5CYII="


class WSLAppLaunchers(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        repo = root / '桌面 app "β" `tick` $env %F 50% back\\slash'
        self.script = repo / "linux" / SOURCE.name
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(SOURCE, self.script)
        self.helper = repo / "windows" / "Open-OfficialApp.ps1"
        self.helper.parent.mkdir()
        self.helper.write_text("# Optional registered-icon provider", encoding="utf-8")
        self.apps = root / "用户目录" / "applications"
        self.system = root / "xdg-system"
        self.icons = self.apps.parent / "cloudworkspace" / "windows-app-icons"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "activation.json"
        self.icon_log = root / "icons.jsonl"
        self.winpath = r'C:\Workspace 中文\app "Ω" %F\windows\Open-OfficialApp.ps1'
        self.env = {
            "HOME": str(root), "XDG_DATA_HOME": str(self.apps.parent),
            "XDG_DATA_DIRS": str(self.system), "PATH": str(self.bin) + os.pathsep + os.defpath,
            "LC_ALL": "C.UTF-8", "NATIVE_ARGS_LOG": str(self.log), "ICON_ARGS_LOG": str(self.icon_log),
            "FIXTURE_HELPER": str(self.helper), "FIXTURE_WINPATH": self.winpath, "FIXTURE_PNG": PNG,
        }
        machine = platform.machine().lower()
        if machine not in {"x86_64", "amd64", "arm64", "aarch64"}:
            self.skipTest("fixture needs a supported WSL architecture")
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        self.exe = self.helper.with_name(f"CloudLinuxWorkspace-{arch}.exe")
        self.program(self.exe, """import json, os, sys
from pathlib import Path
Path(os.environ["NATIVE_ARGS_LOG"]).write_text(json.dumps([sys.argv[0], *sys.argv[1:]]))
if os.environ.get("NATIVE_EXIT"):
    print("Windows resolver diagnostic", file=sys.stderr)
    sys.exit(int(os.environ["NATIVE_EXIT"]))
""")
        self.program(self.bin / "wslpath", """import os, sys
assert len(sys.argv) == 3 and sys.argv[1] == "-w"
if sys.argv[2] == os.environ["FIXTURE_HELPER"]:
    print(os.environ["FIXTURE_WINPATH"])
else:
    print("Z:" + sys.argv[2].replace("/", chr(92)))
""")
        self.program(self.bin / "powershell.exe", """import base64, json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
assert args[:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-File", os.environ["FIXTURE_WINPATH"]]
assert len(args) == 9 and args[5] == "-App" and args[7] == "-IconPath" and args[8].startswith("Z:")
with open(os.environ["ICON_ARGS_LOG"], "a") as out:
    out.write(json.dumps(args) + chr(10))
if os.environ.get("ICON_GATE"):
    gate = Path(os.environ["ICON_GATE"])
    (gate / args[6]).touch()
    end = time.monotonic() + 3
    while len(list(gate.iterdir())) < 2 and time.monotonic() < end:
        time.sleep(.02)
    if len(list(gate.iterdir())) < 2:
        sys.exit(4)
if os.environ.get("ICON_FAILURE"):
    sys.exit(3)
Path(args[8][2:].replace(chr(92), "/")).write_bytes(base64.b64decode(os.environ["FIXTURE_PNG"]))
""")

    def program(self, path, body):
        path.write_text("#!/usr/bin/python3\n" + body)
        path.chmod(0o755)

    def install(self):
        subprocess.run([sys.executable, self.script], env=self.env, check=True)

    def desktop(self, app):
        return self.apps / f"cloudworkspace-windows-{app.lower()}.desktop"

    def test_gio_activates_native_launcher_with_exact_argv(self):
        gio = shutil.which("gio")
        validate = shutil.which("desktop-file-validate")
        if not gio or not validate:
            self.skipTest("requires GIO and desktop-file-utils")
        self.assertFalse(self.apps.exists())
        self.install()
        self.assertFalse(self.log.exists())
        for app in ("ChatGPT", "Claude"):
            with self.subTest(app=app):
                item = self.desktop(app)
                before = item.stat().st_mtime_ns
                self.install()
                self.assertEqual(item.stat().st_mtime_ns, before)
                self.assertIn(f"Name={app} (Windows)", item.read_text())
                subprocess.run([validate, item], env=self.env, check=True)
                self.log.unlink(missing_ok=True)
                subprocess.run([gio, "launch", item], env=self.env, check=True)
                end = time.monotonic() + 4
                while not self.log.exists() and time.monotonic() < end:
                    time.sleep(.025)
                self.assertEqual(json.loads(self.log.read_text()), [str(self.exe), "--open-app", app, "--offer-install"])

    def test_registered_icons_cached_and_negative_results_rechecked_after_expiry(self):
        gate = self.bin.parent / "waiting-apps"
        gate.mkdir()
        self.env["ICON_GATE"] = str(gate)
        self.install()
        first = self.icon_log.read_text()
        self.assertCountEqual([json.loads(line)[6] for line in first.splitlines()], ["ChatGPT", "Claude"])
        for app in ("ChatGPT", "Claude"):
            icon = self.icons / (app.lower() + ".png")
            self.assertIn("Icon=" + str(icon), self.desktop(app).read_text())
            self.assertEqual(icon.read_bytes(), base64.b64decode(PNG))
        self.env["ICON_FAILURE"] = "1"
        self.install()
        self.assertEqual(self.icon_log.read_text(), first)
        icon = self.icons / "chatgpt.png"
        os.utime(icon, (0, 0))
        self.install()
        missed = self.icon_log.read_text()
        self.assertEqual(len(missed.splitlines()), 3)
        self.assertIn("Icon=application-x-executable", self.desktop("ChatGPT").read_text())
        self.assertEqual(icon.read_bytes(), base64.b64decode(PNG))
        del self.env["ICON_FAILURE"]
        self.install()
        self.assertEqual(self.icon_log.read_text(), missed)
        os.utime(icon.with_suffix(".unavailable"), (0, 0))
        self.install()
        self.assertIn("Icon=" + str(icon), self.desktop("ChatGPT").read_text())
        self.assertEqual(len(self.icon_log.read_text().splitlines()), 4)
        icon.write_bytes(b"invalid")
        self.install()
        self.assertEqual(icon.read_bytes(), base64.b64decode(PNG))

    def test_missing_powershell_does_not_delay_later_installs_or_block_activation(self):
        (self.bin / "powershell.exe").unlink()
        self.program(self.bin / "wslpath", """import os, sys
assert sys.argv[1] == "-u"
with open(os.environ["ICON_ARGS_LOG"], "a") as out:
    out.write("looked for PowerShell" + chr(10))
sys.exit(1)
""")
        self.install()
        attempted = self.icon_log.read_text()
        self.assertEqual(len(attempted.splitlines()), 2)
        self.assertIn("Icon=application-x-executable", self.desktop("ChatGPT").read_text())
        self.install()
        self.assertEqual(self.icon_log.read_text(), attempted)
        self.helper.unlink()
        self.install()
        self.assertFalse(self.log.exists())
        subprocess.run([sys.executable, self.script, "--open", "ChatGPT"], env=self.env, check=True)
        self.assertEqual(json.loads(self.log.read_text()), [str(self.exe), "--open-app", "ChatGPT", "--offer-install"])

    def test_vendor_linux_claude_entry_replaces_only_managed_fallback(self):
        self.install()
        native = self.system / "applications" / "com.anthropic.Claude.desktop"
        native.parent.mkdir(parents=True)
        original = "[Desktop Entry]\nType=Application\nName=Claude\nExec=claude-desktop %U\nIcon=claude-desktop\n"
        native.write_text(original)
        self.program(self.bin / "claude-desktop", "raise AssertionError('installer must not launch apps')\n")
        self.icon_log.unlink()
        for icon in self.icons.glob("*.png"):
            os.utime(icon, (0, 0))
        self.install()
        self.assertFalse(self.desktop("Claude").exists())
        self.assertTrue(self.desktop("ChatGPT").is_file())
        self.assertEqual(native.read_text(), original)
        self.assertEqual([json.loads(r)[6] for r in self.icon_log.read_text().splitlines()], ["ChatGPT"])
        (self.bin / "claude-desktop").unlink()
        self.install()
        self.assertTrue(self.desktop("Claude").is_file())
        self.assertFalse(self.log.exists())

    def test_launch_surfaces_resolver_error_and_preserves_user_entries(self):
        self.install()
        command = [sys.executable, self.script, "--open", "ChatGPT"]
        result = subprocess.run(command, env={**self.env, "NATIVE_EXIT": "1"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Windows resolver diagnostic", result.stderr)
        self.exe.chmod(0o644)
        result = subprocess.run(command, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WSL 无法执行", result.stderr)
        custom = self.desktop("Claude")
        custom.write_text("[Desktop Entry]\nName=My personal entry\n")
        result = subprocess.run([sys.executable, self.script], env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(custom.read_text(), "[Desktop Entry]\nName=My personal entry\n")
