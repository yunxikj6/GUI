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
        forwarded.assert_called_once_with(41325, 't' * 43, size=(1280, 900), wallpaper=True, search_input=True)

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
