import importlib.util
from pathlib import Path
import secrets
import shutil
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'linux/verify_native_desktop.py'
spec = importlib.util.spec_from_file_location('native_verify', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class BoundDesktop(unittest.TestCase):
    def setUp(self):
        self.nonce = secrets.token_hex(16)
        prepared = helper.execute('prepare', self.nonce)
        self.files = Path(prepared['folder'])
        self.root = self.files.parent
        self.expected = self.files / prepared['expected']
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.shell = {'pid': 61, 'start': 100, 'socket': [6, 8], 'bus': 'private-address', 'display': '/private-display'}
        self.manager = {'pid': 71, 'start': 200, 'owner': ':1.11'}
        self.calls = self.replace('call', return_value='()')
        self.replace('discover', return_value=self.shell)
        self.details = self.replace('shell_details', return_value=self.shell)
        self.owner = self.replace('shell_owner', return_value=':1.3')
        self.file_owner = self.replace('files_owner', return_value=self.manager)
        self.bus_id = self.replace('bus_id', return_value='private-server-id')
        self.replace('bus_call', return_value='(uint32 1,)')
        self.replace('ubuntu_version', return_value='24.04')

    def replace(self, name, **kwargs):
        p = patch.object(helper, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def test_absence_is_checked_both_before_and_after_opening_the_folder(self):
        self.expected.mkdir()
        with self.assertRaisesRegex(helper.Failure, 'unexpected_entry'):
            helper.execute('attach', self.nonce)
        self.calls.assert_not_called()
        self.expected.rmdir()

        def create_during_open(*args, **kwargs):
            self.expected.mkdir()
            return '()'

        self.calls.side_effect = create_during_open
        with self.assertRaisesRegex(helper.Failure, 'unexpected_entry'):
            helper.execute('attach', self.nonce)
        self.assertEqual(self.calls.call_count, 1)
        with self.assertRaisesRegex(helper.Failure, 'not_attached'):
            helper.execute('check', self.nonce)

    def test_only_prompt_notifies_and_retries_preserve_session_and_receipt(self):
        first = helper.execute('attach', self.nonce)
        second = helper.execute('attach', self.nonce)
        self.assertEqual(first, second)
        self.assertEqual((first['shellPid'], first['filesPid'], first['ubuntuVersion'], first['backend']), (61, 71, '24.04', 'nested'))
        self.assertEqual([call.args[3] for call in self.calls.call_args_list], ['org.freedesktop.FileManager1.ShowFolders'])
        self.assertEqual(helper.execute('check', self.nonce)['status'], 'pending')
        reply = helper.execute('prompt', self.nonce)
        self.assertEqual(reply, {'status': 'prompted', 'expected': self.expected.name, 'shellPid': 61, 'filesPid': 71})
        self.assertEqual(helper.execute('prompt', self.nonce), reply)
        self.assertEqual([call.args[3] for call in self.calls.call_args_list],
                         ['org.freedesktop.FileManager1.ShowFolders', 'org.freedesktop.Notifications.Notify'])
        self.assertEqual(helper.execute('check', self.nonce)['status'], 'pending')
        self.expected.mkdir()
        with self.assertRaisesRegex(helper.Failure, 'unexpected_entry'):
            helper.execute('prompt', self.nonce)
        self.assertEqual(self.calls.call_count, 2)
        self.assertEqual(helper.execute('check', self.nonce)['status'], 'verified')
        for target, replacement in ((self.owner, ':1.87'), (self.file_owner, {**self.manager, 'start': 202}),
                                    (self.details, {**self.shell, 'socket': [6, 9]}), (self.bus_id, 'different-bus-id')):
            original = target.return_value
            with self.subTest(replacement=replacement):
                target.return_value = replacement
                with self.assertRaisesRegex(helper.Failure, 'session_changed'):
                    helper.execute('check', self.nonce)
                with self.assertRaisesRegex(helper.Failure, 'session_changed'):
                    helper.execute('attach', self.nonce)
                with self.assertRaisesRegex(helper.Failure, 'session_changed'):
                    helper.execute('prompt', self.nonce)
                target.return_value = original

    def test_unverified_items_remain_and_verified_nonempty_target_is_preserved(self):
        helper.execute('attach', self.nonce)
        helper.execute('prompt', self.nonce)
        self.expected.mkdir()
        self.assertEqual(helper.execute('cleanup', self.nonce), {'status': 'preserved'})
        self.assertEqual(helper.execute('check', self.nonce)['status'], 'verified')
        user_file = self.expected / 'notes.txt'
        user_file.write_bytes(b'user data')
        with self.assertRaisesRegex(helper.Failure, 'unexpected_entry'):
            helper.execute('check', self.nonce)
        self.assertEqual(helper.execute('cleanup', self.nonce), {'status': 'preserved'})
        self.assertEqual(user_file.read_bytes(), b'user data')

    def test_existing_entry_cannot_arm_the_challenge_or_pass_before_prompt(self):
        helper.execute('attach', self.nonce)
        self.expected.mkdir()
        for action in ('check', 'prompt'):
            with self.subTest(action=action), self.assertRaisesRegex(helper.Failure, 'unexpected_entry'):
                helper.execute(action, self.nonce)
        self.assertEqual(self.calls.call_count, 1)
        self.assertEqual(helper.execute('cleanup', self.nonce), {'status': 'preserved'})
        self.assertTrue(self.expected.is_dir())

    def test_failed_notification_can_be_retried_without_reopening_files(self):
        self.calls.side_effect = ['()', helper.Failure('bus_unavailable'), '(uint32 3,)']
        self.assertEqual(helper.execute('attach', self.nonce)['status'], 'attached')
        with self.assertRaisesRegex(helper.Failure, 'notification_failed'):
            helper.execute('prompt', self.nonce)
        self.expected.mkdir()
        with self.assertRaisesRegex(helper.Failure, 'unexpected_entry'):
            helper.execute('check', self.nonce)
        self.expected.rmdir()
        self.assertEqual(helper.execute('prompt', self.nonce)['status'], 'prompted')
        self.expected.mkdir()
        self.assertEqual(helper.execute('check', self.nonce)['status'], 'verified')

    def test_late_user_file_is_preserved_and_cleanup_resumes(self):
        real_remove = helper.remove_empty
        new_file = self.root/'untitled.txt'
        def add_before_final_rmdir(path, parent=None):
            if path == self.root:
                new_file.write_bytes(b'keep this')
            return real_remove(path, parent)
        with patch.object(helper, 'remove_empty', side_effect=add_before_final_rmdir):
            self.assertEqual(helper.execute('cleanup', self.nonce), {'status': 'preserved'})
        self.assertEqual(new_file.read_bytes(), b'keep this')
        new_file.unlink()
        self.assertEqual(helper.execute('cleanup', self.nonce), {'status': 'cleaned'})


if __name__ == '__main__':
    unittest.main()
