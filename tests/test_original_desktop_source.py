import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from cloud_render import gnome_wire as wire
from experiments.mutter_app.controls import ORIGINAL_SHA256

ROOT = Path(__file__).resolve().parents[1]
MODULE = "experiments.original_desktop_source"
FAKE_RUN = """set -eu
umask 077
test "$1" = --interactive
test -z "${CLOUDWORKSPACE_GNOME_TOKEN+x}${INSTABOX_API_KEY+x}"
mkdir "$2" "$3"
base=$(dirname "$0")
printf '%s\n' "$3" > "$base/scope"
(sleep 120) &
echo $! > "$base/pid"
if test -f "$base/fail"; then exit 42; fi
mkfifo "$2/graphics.fifo" "$2/input.fifo"
printf '%s' '{"session":"SESSIONID","exe":"EXE"}' > "$2/session.json"
exec 4>"$2/graphics.fifo"
exec 5<"$2/input.fifo"
cat <&5 >/dev/null
touch "$3/controller-closed"
"""


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups and owner-private FIFOs")
class OriginalDesktopSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.repo = Path(self.temp)
        self.app = self.repo / "experiments/mutter_app"
        self.app.mkdir(parents=True)
        for file in (ROOT / "experiments").glob("*.py"):
            (self.app.parent / file.name).symlink_to(file)
        (self.app.parent / "original_desktop_source.py").unlink()
        shutil.copyfile(ROOT / "experiments/original_desktop_source.py", self.app.parent / "original_desktop_source.py")
        (self.app / "controls.py").symlink_to(ROOT / "experiments/mutter_app/controls.py")
        (self.repo / "cloud_render").symlink_to(ROOT / "cloud_render", target_is_directory=True)
        (self.app / "run.sh").write_text(FAKE_RUN.replace("SESSIONID", "01" * 32).replace("EXE", ORIGINAL_SHA256))
        self.token = secrets.token_urlsafe(32)

    def start(self, *args, token=True):
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "INSTABOX_API_KEY": "unused-fixture"}
        if token:
            env["CLOUDWORKSPACE_GNOME_TOKEN"] = self.token
        proc = subprocess.Popen(
            [sys.executable, "-m", MODULE, *args], cwd=self.repo, env=env, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.stdout.close)
        def stop():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=12)
        self.addCleanup(stop)
        return proc

    def port(self, proc):
        with selectors.DefaultSelector() as monitor:
            monitor.register(proc.stdout, selectors.EVENT_READ)
            self.assertTrue(monitor.select(5), "port not reported")
        line = proc.stdout.readline().decode().strip()
        return int(re.fullmatch(r"原厂应用私有桌面端口 (\d+)。", line)[1])

    def verify_cleanup(self):
        limit = time.monotonic() + 10
        scope = Path((self.app / "scope").read_text().strip()).parent
        pid = (self.app / "pid").read_text().strip()
        while scope.exists() and time.monotonic() < limit:
            time.sleep(.05)
        self.assertFalse(scope.exists())
        stat = Path("/proc") / pid / "stat"
        if stat.is_file():
            self.assertEqual(stat.read_text().rsplit(")", 1)[1].split()[0], "Z")

    def test_bad_pairing_then_paired_disconnect_releases_source_and_port(self):
        proc = self.start("--port", "0")
        port = self.port(proc)
        with socket.create_connection(("127.0.0.1", port), 4) as first:
            first.sendall(wire.AUTH_SIZE + b"!" * 43 + b"\n")
            self.assertEqual(first.recv(1), b"")
        with socket.create_connection(("127.0.0.1", port), 4) as client, client.makefile("rb") as reader:
            client.sendall(wire.AUTH_SIZE + self.token.encode() + b"\n" + wire.SESSION.pack(1280, 900, 0))
            while True:
                kind, data = wire.read(reader, (wire.BLOCK, wire.READY))
                if kind == wire.READY:
                    self.assertEqual(wire.DIMENSIONS.unpack(data), (1280, 900))
                    break
        stdout, stderr = proc.communicate(timeout=12)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertNotIn(self.token.encode(), stdout + stderr)
        self.verify_cleanup()
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), 1)

    def test_unpaired_lifetime_and_abrupt_controller_death_cleanup(self):
        for exit_case in ("timeout", "terminated", "killed"):
            with self.subTest(exit_case=exit_case):
                proc = self.start("--port", "0", "--lifetime", "1" if exit_case == "timeout" else "60")
                self.port(proc)
                for _ in range(100):
                    if (self.app / "pid").is_file():
                        break
                    time.sleep(.02)
                if exit_case == "terminated":
                    proc.terminate()
                if exit_case == "killed":
                    proc.kill()
                expected = {"timeout": 124, "terminated": 143, "killed": -signal.SIGKILL}[exit_case]
                stdout, stderr = proc.communicate(timeout=12)
                self.assertEqual(proc.returncode, expected, stderr)
                self.assertNotIn(self.token.encode(), stdout + stderr)
                self.verify_cleanup()
                (self.app / "pid").unlink()

    def test_explicit_failure_for_source_crash_port_conflict_or_missing_pairing(self):
        (self.app / "fail").touch()
        proc = self.start("--port", "0")
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("原厂桌面源意外退出", err.decode())
        self.verify_cleanup()
        (self.app / "fail").unlink()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            proc = self.start("--port", str(occupied.getsockname()[1]))
            _, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 1)
            self.assertIn(b"address already in use", err.lower())
        proc = self.start("--port", "0", token=False)
        _, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"CLOUDWORKSPACE_GNOME_TOKEN", err)
