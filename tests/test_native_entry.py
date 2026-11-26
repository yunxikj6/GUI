"""Check routing between the thin client and the explicit compatibility modes."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import launcher
import native_workspace
import thin_client_entry


class NativeEntry(unittest.TestCase):
    def test_cloud_options_are_validated_before_any_local_or_legacy_launch(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(native_workspace, 'start', side_effect=AssertionError), \
                patch.object(launcher.getpass, 'getpass', side_effect=AssertionError), \
                patch.object(launcher, 'LocalServer', side_effect=AssertionError), \
                patch.object(launcher, 'InstaBox', side_effect=AssertionError), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            for options in (['--machine', 'example'], ['--no-browser'], ['--port', '8000'],
                            ['--connect-port', '65536'], ['--connect-port', '0', '--native'],
                            ['--desktop-port', '0'], ['--cloud-app', '--workbench'],
                            ['--desktop-port', '9', '--connect-port', '8'], ['--cloud-app', '--desktop-port', '9']):
                with self.subTest(options=options), patch.object(sys, 'argv', ['launcher.py', *options]), self.assertRaises(SystemExit) as exit:
                    launcher.main()
                self.assertEqual(exit.exception.code, 2)

    def test_standalone_and_full_entry_choose_the_same_cloud_transport(self):
        from thin_client import viewer
        cases = (
            ([], {}, False, ['--instabox', '--port', '17325']),
            ([], {'CLOUDWORKSPACE_RENDER_PORT': '4040'}, False, ['--instabox', '--port', '17325']),
            (['--desktop-port', '4100', '--machine', 'm_abc'], {}, False, ['--instabox', '--port', '4100', '--machine', 'm_abc']),
            (['--machine', 'm_abc'], {}, False, ['--instabox', '--port', '17325', '--machine', 'm_abc']),
            (['--instabox'], {'CLOUDWORKSPACE_RENDER_PORT': '4040'}, False, ['--instabox', '--port', '17325']),
            (['--cloud-app'], {}, True, ['--instabox', '--port', '17324']),
            (['--cloud-app'], {'CLOUDWORKSPACE_RENDER_PORT': '4040'}, True, ['--port', '4040']),
            (['--connect-port', '5050'], {'CLOUDWORKSPACE_RENDER_PORT': '4040'}, True, ['--port', '5050']),
            (['--instabox', '--connect-port', '6001', '--machine', 'm_abc'], {}, True,
             ['--instabox', '--port', '6001', '--machine', 'm_abc']),
        )
        for options, env, app, expected in cases:
            desktop = Mock()
            desktop.main.return_value = 9
            with self.subTest(options=options, env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, 'argv', ['launcher.py', *options]), patch.object(viewer, 'main', return_value=9) as view, \
                    patch.dict(sys.modules, {'thin_client.gnome_desktop': desktop}), \
                    patch.object(launcher, 'InstaBox', side_effect=AssertionError), \
                    patch.object(launcher, 'LocalServer', side_effect=AssertionError), \
                    patch.object(native_workspace, 'start', side_effect=AssertionError):
                target, unused = (view, desktop.main) if app else (desktop.main, view)
                self.assertEqual(launcher.main(), 9)
                target.assert_called_once_with(expected)
                target.reset_mock()
                self.assertEqual(thin_client_entry.main(options), 9)
                target.assert_called_once_with(expected)
                unused.assert_not_called()

    def test_explicit_compatibility_flags_enter_native_dispatch_before_cloud_or_http(self):
        for options, expected in ((['--native'], (False, None, False)),
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
                'PROCESSOR_ARCHITECTURE': 'x86', 'PROCESSOR_ARCHITEW6432': 'ARM64', 'INSTABOX_API_KEY': 'private-test-marker', 'CLOUDWORKSPACE_RENDER_TOKEN':'private-render-marker'}), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(native_workspace.start(check=True, distro='Ubuntu local'), 0)
            invocation = run.call_args
            self.assertEqual(Path(invocation.args[0][0]).name, 'CloudLinuxWorkspace-arm64.exe')
            self.assertEqual(invocation.args[0][1:], ['--distro', 'Ubuntu local', '--check-native'])
            self.assertNotIn('INSTABOX_API_KEY', invocation.kwargs['env'])
            self.assertNotIn('CLOUDWORKSPACE_RENDER_TOKEN', invocation.kwargs['env'])
            self.assertEqual(native_workspace.start(), 0)
            self.assertEqual(run.call_args.args[0][1:], ['--native'])

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

    @unittest.skipUnless(sys.platform == 'linux', 'exercise the Linux workspace entry')
    def test_wsl_starts_gnome_while_optional_windows_menu_is_busy_or_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'linux'
            base.mkdir()
            (base / 'os-release').write_text('ID=ubuntu\n')
            (base / 'wslg-versions').touch()
            entry = base / 'start-workspace.sh'
            source = (native_workspace.ROOT / 'linux' / entry.name).read_text()
            entry.write_text(source.replace('/etc/os-release', './os-release')
                             .replace('/mnt/wslg/versions.txt', './wslg-versions'))
            (base.parent / 'native_files.py').write_text('')
            (base / 'start-gnome.sh').write_text(
                'if [[ $1 == --check ]]; then exit 0; fi\n'
                '[[ $1 == --desktop-shortcuts ]] || exit 1\n'
                'touch gnome-started\nexit 17\n')
            (base / 'install-app-launchers.py').write_text(
                "from pathlib import Path\nimport time\n"
                "end = time.monotonic() + 4\n"
                "while not Path('gnome-started').exists() and time.monotonic() < end:\n"
                "    time.sleep(0.02)\n"
                "if Path('gnome-started').exists():\n"
                "    Path('gnome-started-before-menu-finished').touch()\n"
                "raise SystemExit(1)\n")
            run = subprocess.run(['/bin/bash', str(entry), '--wsl'], capture_output=True, text=True, timeout=12)
            self.assertEqual(run.returncode, 17, run.stderr)
            self.assertTrue((base / 'gnome-started-before-menu-finished').exists())
            self.assertIn('Windows 版应用的菜单项未能更新', run.stderr)
