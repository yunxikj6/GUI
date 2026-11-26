import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from cloud_render.system_browser import DESKTOP_ID, SystemBrowserRegistration


class SystemBrowserRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.session = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_private_registration_leaves_existing_cloud_profile_and_defaults_intact(self):
        root = self.session / 'registration'
        record = SystemBrowserRegistration.create(root, executable=Path('/bin/true'), platform='wayland')
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(record.profile.stat().st_mode & 0o777, 0o700)
        base = {'HOME': '/home/cloud', 'XDG_CONFIG_HOME': '/cloud/config',
                'XDG_DATA_HOME': '/cloud/data', 'XDG_CONFIG_DIRS': '/etc/xdg'}
        env = record.environment(base)
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
            self.assertEqual(env[key], base[key])
        self.assertEqual(base['XDG_CONFIG_DIRS'], '/etc/xdg')
        self.assertEqual(env['XDG_CONFIG_DIRS'], str(root / 'config') + ':/etc/xdg')
        with self.assertRaises(FileExistsError):
            SystemBrowserRegistration.create(root, executable=Path('/bin/true'), platform='wayland')
        self.assertTrue((root / 'data' / 'applications' / DESKTOP_ID).is_file())

    @unittest.skipUnless(shutil.which('gio'), 'GIO is required for an XDG browser handoff')
    def test_gio_delivers_one_uri_directly_to_the_registered_executable(self):
        # This executable observes only the fixed URI the test itself supplies.
        from shlex import quote
        probe = self.session / 'test browser'
        received = self.session / 'received'
        probe.write_text('#!/bin/sh\nprintf "%s\\0" "$@" > ' + quote(str(received)) + '\n')
        probe.chmod(0o700)
        record = SystemBrowserRegistration.create(
            self.session / '含 空格 % " $ `', executable=probe, platform='x11')
        env = record.environment({'HOME': str(self.session), 'PATH': os.defpath, 'LC_ALL': 'C'})
        result = subprocess.check_output(['gio', 'mime', 'x-scheme-handler/https'], env=env)
        self.assertIn(DESKTOP_ID.encode(), result.splitlines()[0])
        url = 'https://example.invalid/xdg-handoff'
        subprocess.run(['gio', 'open', url], env=env, check=True)
        until = time.monotonic() + 5
        while not received.is_file() and time.monotonic() < until:
            time.sleep(.02)
        self.assertEqual(received.read_bytes().decode().split('\0'),
                         [*record.arguments, '--new-window', '--', url, ''])
