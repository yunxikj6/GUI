import unittest

from thin_client.gnome_wintext import TextInput


class WindowsTextTests(unittest.TestCase):
    def setUp(self):
        self.revision = 3
        self.sent = []
        self.text = TextInput(lambda: self.revision, self.sent.append)

    def test_ime_captures_focus_revision_and_preedit_never_enters_queue(self):
        self.text.start()
        for ch in 'nihao':
            self.text.character(ord(ch))
        self.assertEqual(self.sent, [])
        self.text.commit('你好')
        self.text.reset()
        self.assertEqual(self.sent, [('text', 3, '你好')])
        self.text.start()
        self.revision = 5
        self.text.commit('stale')
        self.text.reset()
        self.text.commit('late')
        self.assertEqual(len(self.sent), 1)

    def test_result_after_end_reuses_only_its_original_focus_once(self):
        self.revision = 1 << 140
        self.text.start()
        self.text.end()
        self.text.commit('你好')
        self.text.commit('duplicate')
        self.text.start()
        self.text.end()
        self.text.next_key()
        self.text.commit('expired')
        self.assertEqual(self.sent, [('text', 1 << 140, '你好')])

    def test_cancel_lost_focus_controls_and_utf8_limits(self):
        self.text.start()
        self.text.reset()
        self.text.commit('A')
        for revision, value in [(None, 'A'), (4, ''), (5, 'x\x7f'), (6, 'x\r\ny'),
                                (7, '中'*1366), (8, '\ud800')]:
            self.revision = revision
            self.text.start()
            self.text.commit(value)
            self.text.reset()
        self.assertEqual(self.sent, [])

    def test_printable_unicode_surrogate_pairs_are_bound_to_one_focus(self):
        self.text.character(ord('a'))
        self.text.character(0xd83d)
        self.text.character(0xde42)
        self.text.character(0xd83d)
        self.revision = 4
        self.text.character(0xde42)
        self.text.character(0xd83d)
        self.text.reset()
        self.text.character(0xde42)
        self.text.character(0x0d)
        self.text.character(ord('文'))
        self.assertEqual(self.sent, [('text', 3, 'a'), ('text', 3, '🙂'), ('text', 4, '文')])
