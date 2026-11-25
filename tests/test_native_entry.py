"""Verify that launching the native workspace neither requests a cloud secret nor opens the web workbench."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import launcher
import native_workspace


class NativeEntry(unittest.TestCase):
    def test_default_and_check_enter_native_dispatch_before_cloud_or_http(self):
        for options, expected in (([], (False, None, False)),
                                  (['--check-native', '--distro', 'Ubuntu-24.04'], (True, 'Ubuntu-24.04', False)),
                                  (['--setup-native', '--distro', 'Ubuntu-24.04'], (False, 'Ubuntu-24.04', True))):
            with self.subTest(options=options), patch.object(sys, 'argv', ['launcher.py', *options]), \
                    patch.object(native_workspace, 'start', return_value=7) as native, \
                    patch.object(launcher.getpass, 'getpass', side_effect=AssertionError), \
                    patch.object(launcher, 'LocalServer', side_effect=AssertionError), \
                    patch.object(launcher, 'InstaBox', side_effect=AssertionError):
                self.assertEqual(launcher.main(), 7)
                native.assert_called_once_with(check=expected[0], distro=expected[1], setup=expected[2])

    def test_windows_python_entry_uses_packaged_launcher_without_cloud_key(self):
        with patch.object(sys, 'platform', 'win32'), patch.dict(os.environ, {
                'PROCESSOR_ARCHITECTURE': 'x86', 'PROCESSOR_ARCHITEW6432': 'ARM64', 'INSTABOX_API_KEY': 'private-test-marker'}), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(native_workspace.start(check=True, distro='Ubuntu local'), 0)
            invocation = run.call_args
            self.assertEqual(Path(invocation.args[0][0]).name, 'CloudLinuxWorkspace-arm64.exe')
            self.assertEqual(invocation.args[0][1:], ['--distro', 'Ubuntu local', '--check-native'])
            self.assertNotIn('INSTABOX_API_KEY', invocation.kwargs['env'])

    def test_script_check_is_read_only_and_failed_preflight_does_not_install_or_launch(self):
        if sys.platform != 'linux':
            self.skipTest('exercise the Linux desktop entry')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'linux'
            base.mkdir()
            (base.parent / 'native_files.py').write_text(
                "import sys\nfrom pathlib import Path\nPath('project-args').write_text(' '.join(sys.argv[1:]))\n")
            script = base / 'start-workspace.sh'
            script.write_bytes((native_workspace.ROOT / 'linux' / script.name).read_bytes())
            (base / 'start-gnome.sh').write_text('printf "%s\\n" "$*" >> calls\nexit "${TEST_CHECK_RESULT:-0}"\n')
            (base / 'install-app-launchers.py').write_text("raise AssertionError('unexpected install')\n")
            for args, success in ((['--check'], True), ([], False)):
                result = subprocess.run(['/bin/bash', str(script), *args], env=dict(os.environ, TEST_CHECK_RESULT='0' if success else '3'))
                self.assertEqual(result.returncode, 0 if success else 3)
            self.assertEqual((base / 'calls').read_text().splitlines(), ['--check', '--check'])
            self.assertFalse((base / 'project-args').exists())
            env = dict(os.environ, TEST_CHECK_RESULT='0', XDG_CONFIG_HOME=str(base / 'config'))
            subprocess.run(['/bin/bash', str(script)], env=env, check=True)
            self.assertEqual((base / 'calls').read_text().splitlines()[-2:], ['--check', '--desktop-shortcuts'])
            self.assertEqual((base / 'project-args').read_text(), 'relink --offer-setup')
