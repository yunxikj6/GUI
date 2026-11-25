"""Exercise real GIO activation, native interop argv, and optional registered app icons."""
import base64
import fcntl
import json
import os
from pathlib import Path
import platform
import pty
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
if os.environ.get("ICON_RELEASE"):
    release = Path(os.environ["ICON_RELEASE"])
    (release.parent / args[6]).touch()
    end = time.monotonic() + 4
    while not release.exists() and time.monotonic() < end:
        time.sleep(.02)
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

    def offer(self, *, check=True):
        return subprocess.run([sys.executable, "-c", "import runpy,sys;runpy.run_path(sys.argv[1])['offer_app_setup']()",
                               self.script], env=self.env, capture_output=True, text=True, check=check)

    def setup_fixture(self):
        self.env.update(INSTALL_LOG=str(self.bin.parent / "install.log"), XDG_STATE_HOME=str(self.bin.parent / "state"),
                        OPENED=str(self.bin.parent / "ubuntu-opened"), WSL_DISTRO_NAME="Ubuntu",
                        CLOUDWORKSPACE_NESTED_GNOME="1", CLOUDWORKSPACE_SYSTEM_PATH=str(self.bin))
        self.installer = self.script.with_name("setup-vendor-apps.sh")
        self.installer.write_text("""set -eu
printf '%s\\n' "$*" >>"$INSTALL_LOG"
test -z "${INSTABOX_API_KEY+x}"
read -r consent
[[ $consent == y ]] || exit 130
case "$2" in
  chatgpt) desktop=chatgpt; command=chatgpt ;;
  claude) desktop=com.anthropic.Claude; command=claude-desktop ;;
  *) exit 64 ;;
esac
mkdir -p "$XDG_DATA_DIRS/applications"
printf '[Desktop Entry]\\nType=Application\\nName=%s\\nExec=%s %%U\\nIcon=%s\\n' "$desktop" "$command" "$command" >"$XDG_DATA_DIRS/applications/$desktop.desktop"
""")
        self.program(self.script.with_name("desktop-shortcuts.py"), "pass\n")
        for app in ("chatgpt", "claude-desktop"):
            self.program(self.bin / app, """import json, os, sys
from pathlib import Path
with Path(os.environ['OPENED']).open('a') as f:
    print(json.dumps([Path(sys.argv[0]).name, os.environ.get('WAYLAND_DISPLAY'), 'INSTABOX_API_KEY' in os.environ]), file=f)
""")

    def invoke_setup(self, app, answer="", *, script=None):
        return subprocess.run([sys.executable, script or self.script, "--setup-app", app], env=self.env,
                              input=answer, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=8)

    def test_installers_are_independent_relocatable_and_preserve_foreign_entries(self):
        self.setup_fixture()
        self.offer()
        generic = self.apps / "cloudworkspace-install-apps.desktop"
        chatgpt = self.apps / "cloudworkspace-install-chatgpt.desktop"
        claude = self.apps / "cloudworkspace-install-claude.desktop"
        self.assertIn("--setup-apps\n", generic.read_text())
        for app, item in (("ChatGPT", chatgpt), ("Claude", claude)):
            data = item.read_text()
            self.assertIn(f"Name[zh_CN]=安装 Ubuntu 版 {app}\n", data)
            self.assertIn(f" --setup-app {app}\n", data)
            self.assertIn("Terminal=true\n", data)
            self.assertIn("NoDisplay=false\n", data)
            if shutil.which("desktop-file-validate"):
                subprocess.run(["desktop-file-validate", item], check=True)
        self.assertFalse(self.log.exists())
        self.assertFalse(Path(self.env["INSTALL_LOG"]).exists())
        old = self.script.parent
        new = old.with_name("moved Linux 包")
        old.rename(new)
        self.script = new / self.script.name
        self.offer()
        self.assertIn("moved Linux 包", chatgpt.read_text())
        custom = "[Desktop Entry]\nType=Application\nName=My own installer\n"
        chatgpt.write_text(custom)
        claude.unlink()
        self.assertNotEqual(self.offer(check=False).returncode, 0)
        self.assertEqual(chatgpt.read_text(), custom)
        self.assertTrue(claude.is_file())

    def test_one_app_cancel_retry_then_direct_native_launch(self):
        self.setup_fixture()
        self.env.update(INSTABOX_API_KEY="fixture-only", WAYLAND_DISPLAY="private-fixture-display")
        self.install()
        opened = Path(self.env["OPENED"])
        calls = Path(self.env["INSTALL_LOG"])
        self.assertEqual(self.invoke_setup("Claude", "\n").returncode, 130)
        self.assertEqual(calls.read_text().splitlines(), ["--app claude"])
        self.assertFalse(opened.exists())
        self.assertFalse((self.system / "applications").exists())
        for index in range(2):
            result = self.invoke_setup("Claude", "y\n" if index == 0 else "")
            self.assertEqual(result.returncode, 0, result.stdout)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and (not opened.exists() or len(opened.read_text().splitlines()) < index + 1):
                time.sleep(.02)
            self.assertEqual([json.loads(row) for row in opened.read_text().splitlines()],
                             [["claude-desktop", "private-fixture-display", False]] * (index + 1))
        self.assertEqual(calls.read_text().splitlines(), ["--app claude", "--app claude"])
        self.assertFalse(self.log.exists())
        self.assertFalse(self.desktop("Claude").exists())
        self.assertTrue(self.desktop("ChatGPT").is_file())
        self.offer()
        self.assertIn("NoDisplay=true\n", (self.apps / "cloudworkspace-install-claude.desktop").read_text())

    def test_desktop_access_after_external_install_refreshes_only_that_app(self):
        self.setup_fixture()
        shortcuts = self.script.with_name("desktop-shortcuts.py")
        shutil.copyfile(SOURCE.with_name(shortcuts.name), shortcuts)
        self.install()
        subprocess.run([sys.executable, shortcuts], env=self.env, check=True, capture_output=True)
        aliases = self.apps.parent / "cloudworkspace/desktop-shortcuts"
        chatgpt = aliases / "ChatGPT.desktop"
        claude = aliases / "Claude.desktop"
        self.assertEqual(chatgpt.readlink(), self.apps / "cloudworkspace-install-chatgpt.desktop")
        self.assertEqual(claude.readlink(), self.apps / "cloudworkspace-install-claude.desktop")
        self.assertTrue(self.desktop("ChatGPT").is_file())

        # The user installs the package outside the running workspace, before clicking its existing icon.
        native = self.system / "applications/chatgpt.desktop"
        native.parent.mkdir(parents=True)
        native.write_text("[Desktop Entry]\nType=Application\nName=ChatGPT\nExec=chatgpt %U\nIcon=chatgpt\n")
        result = self.invoke_setup("ChatGPT")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(chatgpt.readlink(), native)
        self.assertFalse(self.desktop("ChatGPT").exists())
        self.assertEqual(claude.readlink(), self.apps / "cloudworkspace-install-claude.desktop")
        self.assertTrue(self.desktop("Claude").is_file())
        self.assertFalse(Path(self.env["INSTALL_LOG"]).exists())
        icon = Path(self.env["HOME"]) / "Desktop/ChatGPT.desktop"
        subprocess.run(["/usr/bin/gio", "launch", icon], env=self.env, check=True)
        opened = Path(self.env["OPENED"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (not opened.exists() or len(opened.read_text().splitlines()) < 2):
            time.sleep(.02)
        self.assertEqual([json.loads(line)[0] for line in opened.read_text().splitlines()], ["chatgpt", "chatgpt"])

    def test_install_terminal_closes_without_hanging_up_the_client(self):
        self.setup_fixture()
        self.program(self.bin / "chatgpt", """import json, os
from pathlib import Path
opened = Path(os.environ["OPENED"])
pending = opened.with_suffix(".tmp")
pending.write_text(json.dumps({
    "session": os.getsid(0), "terminal": [os.isatty(fd) for fd in (0, 1, 2)]
}))
pending.replace(opened)
""")
        master, slave = pty.openpty()
        try:
            # Give the launcher a controlling terminal just as GNOME Terminal does.
            enter_terminal = (
                "import fcntl, os, sys, termios\n"
                "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
                "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
            )
            with subprocess.Popen([sys.executable, "-c", enter_terminal, str(self.script), "--setup-app", "ChatGPT"],
                                  stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=self.env) as proc:
                try:
                    os.write(master, b"y\n")
                    self.assertEqual(proc.wait(timeout=8), 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
        finally:
            os.close(slave)
            os.close(master)
        opened = Path(self.env["OPENED"])
        deadline = time.monotonic() + 3
        while not opened.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(opened.exists(), "Closing the setup terminal prevented the app from starting")
        child = json.loads(opened.read_text())
        self.assertNotEqual(child["session"], proc.pid)
        self.assertEqual(child["terminal"], [False, False, False])

    def test_existing_hidden_or_modified_vendor_registration_is_not_reinstalled(self):
        self.setup_fixture()
        self.apps.mkdir(parents=True)
        user = self.apps / "chatgpt.desktop"
        for entry in ("Hidden=true\nExec=chatgpt %U", "NoDisplay=true\nExec=chatgpt %U",
                      "Exec=unrelated-program", "Exec=chatgpt --custom-profile %U"):
            with self.subTest(entry=entry):
                original = f"[Desktop Entry]\nType=Application\nName=User config\n{entry}\n"
                user.write_text(original)
                self.offer()
                self.assertIn("NoDisplay=true\n", (self.apps / "cloudworkspace-install-chatgpt.desktop").read_text())
                result = self.invoke_setup("ChatGPT", "y\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("chatgpt.desktop", result.stdout)
                self.assertEqual(user.read_text(), original)
                self.assertFalse(Path(self.env["INSTALL_LOG"]).exists())
                self.assertFalse(Path(self.env["OPENED"]).exists())

    def test_installation_lock_is_shared_by_a_second_unpacked_copy(self):
        self.setup_fixture()
        moved = self.script.parent.with_name("second-release")
        shutil.copytree(self.script.parent, moved)
        directory = Path(self.env["XDG_STATE_HOME"]) / "cloudworkspace/vendor-install"
        directory.mkdir(parents=True)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for script in (self.script, moved / self.script.name):
                result = self.invoke_setup("ChatGPT", "y\n", script=script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("另一个 Ubuntu", result.stdout)
        finally:
            os.close(fd)
        self.assertFalse(Path(self.env["INSTALL_LOG"]).exists())
        self.assertEqual(self.invoke_setup("ChatGPT", "\n", script=moved / self.script.name).returncode, 130)

    def test_package_install_completing_during_windows_icon_lookup_wins(self):
        self.setup_fixture()
        hold = self.bin.parent / "hold"
        hold.mkdir()
        self.env["ICON_RELEASE"] = str(hold / "continue")
        with subprocess.Popen([sys.executable, self.script], env=self.env) as background:
            try:
                deadline = time.monotonic() + 4
                while not (hold / "Claude").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((hold / "Claude").exists())
                installed = self.system / "applications/com.anthropic.Claude.desktop"
                installed.parent.mkdir(parents=True)
                installed.write_text("[Desktop Entry]\nType=Application\nName=Claude\nExec=claude-desktop %U\n")
            finally:
                (hold / "continue").touch()
            self.assertEqual(background.wait(timeout=5), 0)
        self.assertFalse(self.desktop("Claude").exists())
        self.assertTrue(self.desktop("ChatGPT").is_file())

    def test_installer_failure_or_unregistered_success_does_not_open_an_app(self):
        self.setup_fixture()
        self.installer.write_text("exit 72\n")
        self.assertEqual(self.invoke_setup("ChatGPT", "y\n").returncode, 72)
        self.installer.write_text("exit 0\n")
        result = self.invoke_setup("ChatGPT", "y\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("尚未注册官方 ChatGPT", result.stdout)
        self.assertFalse(Path(self.env["OPENED"]).exists())
        self.assertFalse(self.log.exists())

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
