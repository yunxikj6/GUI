import hashlib
import selectors
import subprocess
import sys
import unittest
from pathlib import Path

from cloud_render.provision import apt, packages


class UbuntuProvisionTests(unittest.TestCase):
    def test_checked_public_root_and_ubuntu_package(self):
        ring = Path(apt.__file__).with_name("keys") / "ubuntu-archive-keyring.gpg"
        self.assertEqual(hashlib.sha256(ring.read_bytes()).hexdigest(), apt.KEYRING)
        row = {"url": "https://example.invalid/ubuntu/pool/a/a.deb", "sha256": "0" * 64, "size": 12}
        with self.assertRaisesRegex(ValueError, "Canonical"):
            packages.fetch(row, Path("/unused"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process scopes")
    def test_owner_disconnect_removes_build_scope_and_descendant(self):
        repository = Path(__file__).resolve().parents[1]
        # Exercise the real guardian with an isolated stand-in long-running
        # worker. It reports its own PID and a spawned child to the test only.
        worker = """import os,signal,subprocess,sys,time
from pathlib import Path
pid=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']).pid
print(sys.argv[1],os.getpid(),pid,flush=True)
time.sleep(60)
"""
        helper = """import sys,subprocess
from unittest.mock import patch
from cloud_render.provision import guardian
original=subprocess.Popen
def spawn(argv,**kwargs):return original([sys.executable,'-c',sys.argv[1],argv[4]],**kwargs)
with patch.object(guardian.subprocess,'Popen',side_effect=spawn):raise SystemExit(guardian.supervise(17325,30))
"""
        with subprocess.Popen(
            [sys.executable, "-c", helper, worker],
            cwd=repository,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            proc.stdin.write(b"A" * 43 + b"\n")
            proc.stdin.flush()
            with selectors.DefaultSelector() as monitor:
                monitor.register(proc.stdout, selectors.EVENT_READ)
                self.assertTrue(monitor.select(5), "disposable worker did not start")
            name, leader, child = proc.stdout.readline().decode().split()
            scope = Path(name)
            self.assertTrue(scope.is_dir())
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read().decode())
            self.assertFalse(scope.exists())
            for pid in (leader, child):
                stat = Path("/proc") / pid / "stat"
                if stat.exists():
                    self.assertEqual(stat.read_text().rsplit(")", 1)[1].split()[0], "Z")


if __name__ == "__main__":
    unittest.main()
