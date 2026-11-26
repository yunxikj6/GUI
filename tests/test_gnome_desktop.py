import threading
import unittest
from unittest.mock import Mock, patch

from thin_client.gnome_desktop import HEIGHT, WIDTH, InputQueue, present_session


class DesktopTests(unittest.TestCase):
    def test_forwarded_desktop_requests_only_pairing_and_dials_local_transport(self):
        from thin_client import gnome_desktop, gnome_transport
        fake_drawing = Mock()
        with patch.dict('sys.modules', {'thin_client.gnome_window': fake_drawing,
                                       'thin_client.virgl_windows': fake_drawing}), \
                patch('sys.platform', 'win32'), \
                patch('thin_client.viewer.hidden_value', return_value='t' * 43) as secret, \
                patch('instabox.InstaBox', side_effect=AssertionError('provider credential access')), \
                patch.object(gnome_transport, 'connect_forwarded') as forwarded, \
                patch.object(gnome_desktop, 'present_session',
                             side_effect=lambda start, *args, **kwargs: start()):
            self.assertEqual(gnome_desktop.main(['--forwarded', '--port', '41325', '--size', '1280x900']), 0)
        self.assertEqual([call.args[0] for call in secret.call_args_list], ['CLOUDWORKSPACE_GNOME_TOKEN'])
        forwarded.assert_called_once_with(41325, 't' * 43, size=(1280, 900), wallpaper=True, search_input=True, focus_delete=True)

    def test_pointer_moves_coalesce_only_between_buttons_and_keys(self):
        inputs = InputQueue(3)
        for event in [('move', 10, 12), ('move', 20, 24), ('click', 20, 24, 'left'),
                      ('move', 35, 40), ('move', 37, 41)]:
            inputs.put(event)
        with self.assertRaises(RuntimeError):
            inputs.put(('key', 'Tab'))
        self.assertEqual([inputs.get() for _ in range(3)],
                         [('move', 20, 24), ('click', 20, 24, 'left'), ('move', 37, 41)])
        inputs.close()
        self.assertIsNone(inputs.get())

    def test_window_input_reaches_peer_and_close_releases_blocked_decoder(self):
        frame = threading.Event()
        sent = threading.Event()
        closed = threading.Event()
        output = []
        events = []

        class Peer:
            def pop_unavailable(self):
                return None

            def click(self, *args):
                events.append(args)
                sent.set()

            def close(self):
                closed.set()

        class Window:
            def __init__(self, title, width, height, callback):
                self.submit = callback
                self.clicked = False
                output.append((width, height))

            def present(self, pixels):
                output.append(pixels)
                frame.set()
                return True

            def set_title(self, title):
                pass

            def pump(self):
                if frame.is_set() and not self.clicked:
                    self.clicked = True
                    self.submit(('click', 520, 327, 'right'))
                return not sent.is_set()

            def close(self):
                output.append('closed')

        def replay(session, on_scanout):
            on_scanout(WIDTH, HEIGHT, b'local scanout')
            if not closed.wait(2):
                raise RuntimeError('decoder did not wake')

        present_session(Peer, replay, Window)
        self.assertTrue(closed.is_set())
        self.assertEqual(events, [(520, 327, 'right')])
        self.assertEqual(output, [(800, 520), b'local scanout', 'closed'])

    def test_committed_text_and_select_all_keep_the_window_focus_token_and_order(self):
        focused = 1 << 140
        frame, sent, closed = threading.Event(), threading.Event(), threading.Event()
        peer = Mock()
        peer.text_focus.return_value = focused
        peer.pop_unavailable.return_value = None
        peer.close.side_effect = closed.set
        peer.select_all.side_effect = lambda revision: sent.set()

        class Window:
            def __init__(self, title, width, height, submit):
                self.submit = submit
                self.typed = False

            def present(self, pixels):
                frame.set()
                return True

            def pump(self):
                if frame.is_set() and not self.typed:
                    self.typed = True
                    revision = self.text_focus()
                    self.submit(('text', revision, '你好'))
                    self.submit(('select-all', revision))
                    self.submit(('delete-key', revision, True))
                    self.submit(('delete-key', None, False))
                return not sent.is_set()

            def set_title(self, title):
                pass

            def close(self):
                pass

        def replay(session, on_scanout):
            on_scanout(WIDTH, HEIGHT, b'local scanout')
            self.assertTrue(closed.wait(2))

        present_session(lambda: peer, replay, Window)
        peer.commit_text.assert_called_once_with(focused, '你好')
        peer.delete_text.assert_called_once_with(focused, backward=True)
        peer.select_all.assert_called_once_with(focused)
        first = [c[0] for c in peer.method_calls if c[0] in ('commit_text', 'select_all')]
        self.assertEqual(first, ['commit_text', 'select_all'])

    def test_rejected_source_frame_stops_and_releases_session(self):
        closed = threading.Event()

        class Peer:
            def close(self):
                closed.set()

        class Window:
            def __init__(self, *args):
                pass

            def pump(self):
                return True

            def close(self):
                pass

        def replay(session, on_scanout):
            on_scanout(4, 4, b'bad dimensions')

        with self.assertRaises(ValueError):
            present_session(Peer, replay, Window)
        self.assertTrue(closed.is_set())
