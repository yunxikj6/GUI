import asyncio
from contextlib import suppress
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import tempfile
import unittest

from cloud_render import gnome_commands as mesa
from cloud_render.gnome_relay import DesktopService
from experiments.application_surface import Binding
from experiments.live_application_surface import binding_record
from experiments.original_desktop import _WindowInput
from experiments.original_desktop_bridge import interactive_fifos
from experiments.original_desktop_protocol import SIZE, approved_binding, read_hello
from experiments.mutter_app.controls import ORIGINAL_SHA256
from thin_client.gnome_transport import connect_forwarded


@unittest.skipUnless(os.name == 'posix', 'source bridge requires POSIX FIFOs')
class PrivateOriginalDesktop(unittest.IsolatedAsyncioTestCase):
    async def test_production_loopback_transport_observes_applied_pointer_and_closes_source(self):
        with tempfile.TemporaryDirectory() as temp:
            channel = Path(temp)
            for name in ('input.fifo', 'graphics.fifo'):
                os.mkfifo(channel/name, 0o600)
            config = dict(session='01'*32, exe=ORIGINAL_SHA256)
            identity = dict(config, pid=11, instance='03'*32, surface=3, handle=4, creation=1)
            def publish(name, data):
                path = channel/name
                tmp = channel/(name+'.new')
                tmp.write_text(json.dumps(data))
                tmp.chmod(0o600)
                tmp.replace(path)
            publish('session.json', config)
            publish('binding.json', identity)
            ping = mesa._HEADER.pack(0, mesa.PING)
            bind = binding_record(len(ping), approved_binding(identity))
            received = []
            def produce():
                with (channel/'graphics.fifo').open('wb', buffering=0) as graphics:
                    with (channel/'input.fifo').open('rb') as inputs:
                        graphics.write(ping+bind)
                        for index in range(1, 3):
                            received.append(inputs.read(5))
                            publish('last-input.json', dict(sequence=index, applied=True))
                        self.assertEqual(inputs.read(1), b'')
            task = asyncio.create_task(asyncio.to_thread(produce))
            token = 'd'*43
            service = DesktopService(token, lambda **opts: interactive_fifos(channel, **opts))
            server = await asyncio.start_server(service.accept, '127.0.0.1', 0)
            async with server:
                port = server.sockets[0].getsockname()[1]
                def client():
                    with connect_forwarded(port,token,size=SIZE) as stream:
                        self.assertEqual(read_hello(stream), (bytes.fromhex(config['session']), bytes.fromhex(ORIGINAL_SHA256)))
                        self.assertEqual(stream.read(len(ping+bind)), ping+bind)
                        stream.move(31, 68)
                        stream.click(400,350)
                try:
                    await asyncio.wait_for(asyncio.to_thread(client), 18)
                    await asyncio.wait_for(task, 5)
                finally:
                    await service.close()
            self.assertEqual(received, [b'M\0\x1f\0D', b'L\x01\x90\x01^'])

    def test_desktop_pointer_captures_current_view_and_discards_a_stale_click(self):
        from types import SimpleNamespace
        state = _WindowInput()
        reader = SimpleNamespace(can_present=True, binding=None, digest=None)
        state.reader = reader
        state.presented(reader)
        events = []
        dispatch = state.events(events.append)
        dispatch(('click', 20, 30, 'left'))
        self.assertEqual(events, [('click',20,30,'left',(None,None))])
        reader.can_present = False
        self.assertIsNone(state.ticket())
        dispatch(('click', 20, 30, 'left'))
        dispatch(('key', 'Enter'))
        dispatch(('move', 20, 30))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1], ('move',20,30))

class RetainedNativeWindow(unittest.TestCase):
    def test_disconnect_leaves_local_frame_and_refuses_old_inputs_until_user_closes(self):
        from thin_client.gnome_desktop import present_session
        class Peer:
            def __init__(self):
                self.closed = threading.Event()
            def close(self):
                self.closed.set()
            def pop_unavailable(self):
                return None
            def click(self,*_):
                raise AssertionError('an input reached the detached cloud peer')
        for failure in (None, ConnectionError('peer gone'), ValueError('invalid graphics')):
            with self.subTest(failure=type(failure).__name__):
                observed = []
                peer = Peer()
                class Window:
                    def __init__(self, title, width, height, submit):
                        self.submit = submit
                        self.title = title
                    def present(self,pixels):
                        observed.append(pixels)
                        return True
                    def set_title(self,title):
                        self.title = title
                    def pump(self):
                        if '只读' in self.title:
                            self.submit(('click',12,22,'left'))
                            observed.append(self.title)
                            return False
                        return True
                    def close(self):
                        observed.append('native closed')
                def render(_session, show):
                    show(*SIZE,b'local original scanout')
                    if failure:raise failure
                def run():
                    present_session(lambda:peer,render,Window,size=SIZE,retain_on_disconnect=True)
                if isinstance(failure,ValueError):
                    with self.assertRaises(ValueError):run()
                else:
                    run()
                self.assertTrue(peer.closed.is_set())
                self.assertEqual(observed[0],b'local original scanout')
                self.assertIn('只读',observed[1])
                self.assertEqual(observed[-1],'native closed')
