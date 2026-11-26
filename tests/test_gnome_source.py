import io
import json
import os
from pathlib import Path
import select
import socket
import struct
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing

from cloud_render import gnome_app_request, gnome_input, gnome_search_input, gnome_source


class GnomeSourceTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Linux peer credentials required')
    def test_only_original_shell_can_confirm_a_focused_commit(self):
        with tempfile.TemporaryDirectory() as home, ExitStack() as stack:
            channel = gnome_search_input.SearchBridge(Path(home), stack)
            with socket.socket(socket.AF_UNIX) as outsider:
                outsider.connect(str(Path(home) / 'text-input'))
                self.assertFalse(channel.connect(os.getpid() + 1))
                self.assertEqual(outsider.recv(1), b'')
            with socket.socket(socket.AF_UNIX) as original:
                original.connect(str(Path(home) / 'text-input'))
                self.assertTrue(channel.connect(os.getpid()))
                events = []
                def notify(event, **fields):
                    events.append((event, fields))
                original.sendall(b'F 1')
                channel.receive(notify)
                original.sendall(b' 1\n')
                channel.receive(notify)
                def apply(_timeout):
                    self.assertEqual(json.loads(original.recv(256)), [1, '你好'])
                    original.sendall(b'A 1\nF 2 0\nF 3 1\n')
                    channel.receive(notify)
                self.assertTrue(channel.commit(1, '你好', apply))
                self.assertFalse(channel.commit(1, '过期', apply))
                self.assertEqual(events, [('focus', dict(revision=1, active=True)),
                                          ('focus', dict(revision=2, active=False)),
                                          ('focus', dict(revision=3, active=True))])
                original.sendall(b'A 1\n')
                with self.assertRaises(ValueError):
                    channel.receive(notify)

    def test_input_is_one_exact_bounded_action(self):
        for action in (b'M', b'L', b'R'):
            for x, y in [(0, 0), (799, 519)]:
                self.assertEqual(gnome_input.decode_input(gnome_input.encode_pointer(action, x, y)),
                                 (action, (x, y)))
        for key in gnome_input.KEYS:
            self.assertEqual(gnome_input.decode_input(gnome_input.encode_key(key))[0], b'K')
        self.assertEqual(gnome_input.decode_input(b'S'), (b'S', ()))
        for invalid in (b'', b'X', b'SS', b'K', b'K\x10', b'K\x00', b'M\x03\x20\0\0', b'R\x00\x01\x02\x08'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                gnome_input.decode_input(invalid)

    def test_startup_graphics_wait_for_the_exact_spawned_shell_pid(self):
        for pid in (19, 23):
            output = io.BytesIO()
            queue = gnome_source._CommandOutput(output)
            queue.write(b'first')
            self.assertEqual(output.getvalue(), b'')
            read, write = os.pipe()
            with os.fdopen(write, 'wb') as peer:
                peer.write(struct.pack('!I', pid))
            queue.write(b'second')
            queue.finish()
            with os.fdopen(read, 'rb'):
                queue.publish(read, 19)
            if pid == 19:
                self.assertEqual(output.getvalue(), b'firstsecond')
            else:
                with self.assertRaises(gnome_source.SourceError):
                    queue.write(b'blocked')
                self.assertEqual(output.getvalue(), b'')

    def test_slow_receiver_blocks_only_after_bounded_graphics_queue_fills(self):
        entered = threading.Event()
        resume = threading.Event()

        class SlowSink(io.BytesIO):
            def write(self, data):
                entered.set()
                if not resume.wait(5):
                    raise OSError('fixture timeout')
                return super().write(data)

        sink = SlowSink()
        queue = gnome_source._CommandOutput(sink, capacity=8)
        read, write = os.pipe()
        with os.fdopen(write, 'wb') as peer:
            peer.write(struct.pack('!I', 101))
        with os.fdopen(read, 'rb'):
            consumer = threading.Thread(target=queue.publish, args=(read, 101), daemon=True)
            consumer.start()
            queue.write(b'1234')
            self.assertTrue(entered.wait(1))
            queue.flush()
            queue.write(b'5678')
            self.assertEqual(queue.used, 8)
            sent = threading.Event()

            def produce():
                queue.write(b'9')
                sent.set()
                queue.finish()

            producer = threading.Thread(target=produce, daemon=True)
            producer.start()
            self.assertFalse(sent.wait(.03))
            resume.set()
            producer.join(1)
            consumer.join(1)
        self.assertTrue(sent.is_set())
        self.assertEqual(sink.getvalue(), b'123456789')
        self.assertEqual(queue.used, 0)

    def test_closed_receiver_wakes_a_blocked_graphics_producer(self):
        entered = threading.Event()
        fail = threading.Event()

        class ClosedSink(io.BytesIO):
            def write(self, data):
                entered.set()
                fail.wait(2)
                raise BrokenPipeError

        queue = gnome_source._CommandOutput(ClosedSink(), capacity=4)
        read, write = os.pipe()
        with os.fdopen(write, 'wb') as peer:
            peer.write(struct.pack('!I', 101))
        with os.fdopen(read, 'rb'):
            worker = threading.Thread(target=queue.publish, args=(read, 101), daemon=True)
            worker.start()
            queue.write(b'1234')
            self.assertTrue(entered.wait(1))
            fail.set()
            with self.assertRaises(BrokenPipeError):
                queue.write(b'5')
            worker.join(1)
            self.assertFalse(worker.is_alive())

    @unittest.skipUnless(Path('/proc/net/unix').exists(), 'Linux UNIX domain sockets required')
    def test_ordinary_wayland_client_can_never_reach_mutter_after_seal(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            gate = gnome_source._ClientGate(Path(temp), stack)
            with socket.socket(socket.AF_UNIX) as original, socket.socket(socket.AF_UNIX) as app:
                self.assertFalse(gate.seal())
                original.bind(str(gate.named))
                original.listen()
                self.assertTrue(gate.seal())
                app.connect(str(gate.named))
                self.assertEqual(gate.refused(), 'wayland')
                self.assertEqual(app.recv(1), b'')
                self.assertEqual(select.select([original], [], [], 0)[0], [])

    @unittest.skipUnless(Path('/proc/net/unix').exists(), 'Linux UNIX domain sockets required')
    def test_seal_rejects_queued_and_accepted_preexisting_clients(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                gate = gnome_source._ClientGate(Path(temp), stack)
                original = stack.enter_context(socket.socket(socket.AF_UNIX))
                original.bind(str(gate.named))
                original.listen()
                app = stack.enter_context(socket.socket(socket.AF_UNIX))
                app.connect(str(gate.named))
                if accepted:
                    stack.enter_context(closing(original.accept()[0]))
                with self.assertRaises(gnome_source.SourceError):
                    gate.seal()

    @unittest.skipUnless(Path('/proc/net/unix').exists(), 'Linux UNIX domain sockets required')
    def test_background_display_probes_do_not_claim_the_user_launched_a_window(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            control, keep_open = os.pipe()
            read, events = os.pipe()
            for fd in (control, keep_open, read, events):
                stack.callback(os.close, fd)
            source = gnome_source._Source(stack, root, events, control, 'ubuntu')
            source.gate = gnome_source._ClientGate(root, stack)
            with socket.socket(socket.AF_UNIX) as helper:
                helper.connect(str(source.gate.denied))
                source.poll(.1)
                self.assertEqual(helper.recv(1), b'')
            self.assertFalse(select.select([read], [], [], 0)[0])
            gnome_app_request.activate(root, 'wayland')
            source.poll(.1)
            self.assertEqual(json.loads(os.read(read, 128)), {'event': 'unavailable', 'application': 'wayland'})

    def test_original_desktop_metadata_routes_all_exec_and_dbus_activations(self):
        with tempfile.TemporaryDirectory(prefix='gnome-', dir='/tmp') as temp, ExitStack() as stack:
            root = Path(temp)
            metadata = root / 'distro'
            metadata.mkdir()
            original = ('[Desktop Entry]\nType=Application\nName=ChatGPT\nName[zh_CN]=ChatGPT\n'
                        'Icon=official-mark\nExec=vendor --open %U\nDBusActivatable=true\n'
                        'Terminal=true\nTryExec=vendor\nActions=new;\n\n'
                        '[Desktop Action new]\nName=New window\nExec=vendor --new\n')
            (metadata / 'chatgpt.desktop').write_text(original)
            gnome_source._route_launchers(root, [metadata])
            actual = (root / 'home/.local/share/applications/chatgpt.desktop').read_text()
            self.assertIn('Icon=official-mark\n', actual)
            self.assertIn('Name[zh_CN]=ChatGPT\n', actual)
            self.assertIn('TryExec=vendor\n', actual)
            self.assertIn('DBusActivatable=false\n', actual)
            self.assertNotIn('Exec=vendor --', actual)
            self.assertEqual(actual.count('Exec=/usr/bin/python3 ' + str(root / 'activate.py') + ' chatgpt'), 2)
            self.assertEqual((metadata / 'chatgpt.desktop').read_text(), original)
            gate = gnome_source._ClientGate(root, stack)
            gnome_app_request.activate(root, 'chatgpt')
            self.assertEqual(gate.activation.recv(16), b'chatgpt')
            with self.assertRaises(ValueError):
                gnome_app_request.activate(root, '../application')
