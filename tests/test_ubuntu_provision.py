import hashlib
import tempfile
import os
from contextlib import ExitStack
from unittest.mock import patch
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

    def test_download_profile_and_selected_deadline_reach_the_same_guardian(self):
        from cloud_render.provision import __main__ as provision
        from cloud_render.provision import guardian
        with tempfile.TemporaryDirectory() as base, ExitStack() as stack:
            profile = Path(base) / 'account'
            stack.enter_context(patch.dict(os.environ, {'CLOUDWORKSPACE_GNOME_TOKEN': 'A' * 43}))
            stack.enter_context(patch.object(provision, 'prerequisites'))
            child = stack.enter_context(patch('subprocess.Popen'))
            child.return_value.wait.return_value = 0
            self.assertEqual(provision.main(['--chatgpt', '--profile', str(profile),
                                             '--lifetime-minutes', '121']), 0)
            argv = child.call_args.args[0]
            self.assertEqual(argv[4:], ['watch', '17326', '7260', '--chatgpt', '--profile', str(profile)])
            stack.enter_context(patch.object(sys, 'argv', ['guardian', *argv[4:]]))
            watch = stack.enter_context(patch.object(guardian, 'watch_private_worker', return_value=0))
            self.assertEqual(guardian.main(), 0)
            self.assertEqual(watch.call_args.args, (guardian.MODULE, 17326, 7260))
            self.assertEqual(watch.call_args.kwargs['args'], ('--chatgpt',))
            self.assertEqual(watch.call_args.kwargs['profile'], profile)

    def test_app_worker_uses_its_existing_guardian_and_pins_the_persistent_mount(self):
        from cloud_render.provision import worker
        with tempfile.TemporaryDirectory() as base, ExitStack() as stack:
            directory = Path(base)
            binary = directory / 'ubuntu/usr/bin/bwrap'
            binary.parent.mkdir(parents=True)
            binary.touch(mode=0o700)
            for action in ('resolve', 'build', 'generate', 'chatgpt.install'):
                stack.enter_context(patch('cloud_render.provision.worker.' + action))
            staged = stack.enter_context(patch.object(worker.chatgpt, 'stage'))
            fetched = stack.enter_context(patch.object(worker.chatgpt, 'fetch_vendor'))
            execute = stack.enter_context(patch.object(worker.os, 'execve'))
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            worker.run(directory, 17326, 'A' * 43, directory / 'test.deb', fd)
            _, command, _ = execute.call_args.args
            mount = command.index('--bind-fd')
            self.assertEqual(command[mount + 1:mount + 3], [str(fd), '/run/cw-application-data'])
            staged.assert_called_once_with(directory / 'test.deb', directory)
            fetched.assert_not_called()
            worker.run(directory, 17326, 'A' * 43, '--chatgpt', fd)
            fetched.assert_called_once_with(directory)
            staged.assert_called_once()
            source = command.index('experiments.original_desktop_source')
            self.assertEqual(command[source:], ['experiments.original_desktop_source', 'provisioned',
                                                 '17326', '--persistent'])

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
