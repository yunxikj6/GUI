import os
import unittest

from experiments.mutter_app.window_focus import Process, Window, WindowFocus


class NativeFocusTest(unittest.TestCase):
    def setUp(self):
        self.app = Process(71, 981)
        self.browser = Process(72, 982)
        self.seat = WindowFocus(app=self.app, browser=self.browser)

    def send(self, lines):
        for line in lines.splitlines():
            self.seat.record(line)

    def test_real_focus_and_close_require_the_compositor_to_restore_the_app(self):
        self.send('XDG app 71 981 1 4 6\nTOP app 71 981 1 6 9\nENTER app 71 981 1 4 0')
        self.assertEqual(self.seat.focused, Window('app', self.app, 1, 4, 9))
        self.send('XDG browser 72 982 2 4 6\nTOP browser 72 982 2 6 9\nLEAVE app 71 981 1 4 0')
        self.assertIsNone(self.seat.focused)
        self.send('ENTER browser 72 982 2 4 0')
        self.assertEqual(self.seat.focused, Window('browser', self.browser, 2, 4, 9))
        self.send('DROP_TOP browser 72 982 2 9 0\nDISCONNECT 2')
        self.assertIsNone(self.seat.focused)
        self.send('ENTER app 71 981 1 4 0')
        self.assertEqual(self.seat.focused, Window('app', self.app, 1, 4, 9))

    def test_reused_pid_ids_popup_and_two_windows_are_never_selected(self):
        self.send('XDG browser 72 860 2 4 6\nTOP browser 72 860 2 6 9\nENTER browser 72 860 2 4 0')
        self.assertIsNone(self.seat.focused)
        self.send('XDG browser 72 982 3 4 6\nTOP browser 72 982 3 6 9\nENTER browser 72 982 3 12 0')
        self.assertIsNone(self.seat.focused)
        self.send('XDG browser 72 982 3 15 17\nTOP browser 72 982 3 17 19\nENTER browser 72 982 3 15 0')
        self.assertIsNone(self.seat.focused)
        self.send('DROP_SURFACE browser 72 982 3 4 0')
        self.assertEqual(self.seat.focused, Window('browser', self.browser, 3, 15, 19))
        self.send('DROP_XDG browser 72 982 3 17 0')
        self.assertIsNone(self.seat.focused)
        self.send('XDG browser 72 982 3 15 17\nTOP browser 72 982 3 17 19')
        self.assertIsNone(self.seat.focused)

    def test_unknown_process_focus_clears_original_and_native_identity_has_start_time(self):
        self.send('XDG app 71 981 1 4 6\nTOP app 71 981 1 6 9\nENTER app 71 981 1 4 0')
        self.send('ENTER browser 80 997 2 4 0')
        self.assertIsNone(self.seat.focused)
        self.assertGreater(Process.from_pid(os.getpid()).start_ticks, 0)
