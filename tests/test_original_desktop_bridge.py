import asyncio
from contextlib import suppress
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import tempfile
import unittest

from cloud_render import gnome_commands as mesa
from cloud_render.gnome_relay import DesktopService
from experiments.application_surface import Binding
from experiments.live_application_surface import binding_record
from experiments.original_desktop import _PointerDesktop, _WindowInput
from cloud_render.gnome_input import encode_text, encode_select_all, encode_delete
from cloud_render.gnome_wire import FOCUS_STATE, FOCUS_DELETE, SEARCH_INPUT, SELECT_ALL
from experiments.original_desktop_bridge import interactive_fifos, supported_editor_features
from experiments.original_desktop_protocol import SIZE, approved_binding, read_hello
from experiments.mutter_app.controls import ORIGINAL_SHA256
from thin_client.gnome_transport import connect_forwarded


@unittest.skipUnless(os.name == 'posix', 'source bridge requires POSIX FIFOs')
class PrivateOriginalDesktop(unittest.IsolatedAsyncioTestCase):
    async def test_stopping_a_source_closes_an_unfinished_tcp_handshake(self):
        from unittest.mock import patch
        from experiments import original_desktop_bridge as bridge
        from contextlib import redirect_stdout

        accepted = asyncio.Event()
        bound = asyncio.get_running_loop().create_future()
        start_server = asyncio.start_server

        async def listen(callback, *args, **kwargs):
            async def accept(reader, writer):
                accepted.set()
                await callback(reader, writer)
            server = await start_server(accept, *args, **kwargs)
            bound.set_result(server.sockets[0].getsockname()[1])
            return server

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bridge.asyncio, 'start_server', listen),
            redirect_stdout(io.StringIO()),
        ):
            task = asyncio.create_task(bridge.serve(Path(tmp), 0, 'x' * 43))
            reader, writer = await asyncio.open_connection('127.0.0.1', await bound)
            try:
                await accepted.wait()
                writer.write(b'G')
                await writer.drain()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 2.5)
                self.assertEqual(await reader.read(), b'')
            finally:
                writer.close()
                await writer.wait_closed()
                await asyncio.gather(task, return_exceptions=True)

    def test_legacy_sources_negotiate_only_inputs_their_original_editor_supports(self):
        with tempfile.TemporaryDirectory() as temp:
            channel = Path(temp)
            def write(name, data):
                path = channel/name
                path.write_text(json.dumps(data))
                path.chmod(0o600)
            identity = dict(session='41'*32, exe=ORIGINAL_SHA256)
            write('session.json',identity)
            self.assertEqual(supported_editor_features(channel),0)
            (channel/'focus.events').touch(mode=0o600)
            self.assertEqual(supported_editor_features(channel),SEARCH_INPUT|SELECT_ALL)
            for session, flags in ((identity['session'], FOCUS_DELETE),('55'*32,14),
                                   (identity['session'],14.0),(identity['session'],255)):
                write('editor-capabilities.json',dict(session=session,flags=flags))
                with self.assertRaises(ValueError):
                    supported_editor_features(channel)
            write('editor-capabilities.json',dict(session=identity['session'],flags=14))
            self.assertEqual(supported_editor_features(channel),14)

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
            publish('editor-capabilities.json',dict(session=config['session'], flags=14))
            publish('binding.json', identity)
            ping = mesa._HEADER.pack(0, mesa.PING)
            bind = binding_record(len(ping), approved_binding(identity))
            received = []
            focus = channel/'focus.events'
            focus.touch(mode=0o600)
            def focus_change(revision, active):
                with focus.open('ab', buffering=0) as output:
                    record = FOCUS_STATE.pack(revision, active)
                    # An observer can see a partial write on a growing regular file.
                    output.write(record[:2])
                    time.sleep(.015)
                    output.write(record[2:])
            def produce():
                with (channel/'graphics.fifo').open('wb', buffering=0) as graphics:
                    with (channel/'input.fifo').open('rb') as inputs:
                        graphics.write(ping+bind)
                        for index in range(1, 3):
                            received.append(inputs.read(5))
                            publish('last-input.json', dict(sequence=index, applied=True))
                        focus_change(1,1)
                        for index, command, applied in ((3,encode_text(1,'图文'),True),
                                                        (4,encode_select_all(1),False),
                                                        (5,encode_delete(1,backward=True),True),
                                                        (6,encode_delete(1,backward=False),False)):
                            received.append(inputs.read(len(command)))
                            publish('last-input.json', dict(sequence=index, applied=applied))
                        focus_change(2,0)
                        self.assertEqual(inputs.read(1), b'')
            task = asyncio.create_task(asyncio.to_thread(produce))
            token = 'd'*43
            service = DesktopService(token, lambda **opts: interactive_fifos(channel, **opts))
            server = await asyncio.start_server(service.accept, '127.0.0.1', 0)
            async with server:
                port = server.sockets[0].getsockname()[1]
                def client():
                    with connect_forwarded(port,token,size=SIZE,search_input=True,focus_delete=True) as stream:
                        self.assertEqual(read_hello(stream), (bytes.fromhex(config['session']), bytes.fromhex(ORIGINAL_SHA256)))
                        self.assertEqual(stream.read(len(ping+bind)), ping+bind)
                        stream.move(31, 68)
                        stream.click(400,350)
                        until = time.monotonic() + 3
                        while stream.text_focus() is None and time.monotonic() < until:
                            time.sleep(.01)
                        revision = stream.text_focus()
                        self.assertIsNotNone(revision)
                        self.assertTrue(stream.commit_text(revision, '图文'))
                        self.assertFalse(stream.select_all(revision))
                        self.assertTrue(stream.delete_text(revision,backward=True))
                        self.assertFalse(stream.delete_text(revision,backward=False))
                        until = time.monotonic() + 3
                        while stream.text_focus() == revision and time.monotonic() < until:
                            time.sleep(.01)
                        self.assertIsNone(stream.text_focus())
                        self.assertFalse(stream.commit_text(revision,'图文'))
                try:
                    await asyncio.wait_for(asyncio.to_thread(client), 18)
                    await asyncio.wait_for(task, 5)
                finally:
                    await service.close()
            self.assertEqual(received, [b'M\0\x1f\0D', b'L\x01\x90\x01^',
                                        encode_text(1,'图文'), encode_select_all(1),
                                        encode_delete(1,backward=True), encode_delete(1,backward=False)])

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
        self.assertIn('点击未发送',state.pop_input_notice())
        self.assertIsNone(state.pop_input_notice())

    def test_text_is_bound_to_visible_original_application_and_source_focus(self):
        state = _WindowInput()
        reader = SimpleNamespace(binding=None, digest=None, can_present=True)
        state.reader = reader
        actual = []
        upstream = SimpleNamespace(text_focus=lambda: 99,
            commit_text=lambda revision, value: actual.append((revision,value)) or True,
            select_all=lambda revision: actual.append(revision) or True)
        desktop = _PointerDesktop(upstream, state)
        state.presented(reader)
        self.assertIsNone(desktop.text_focus())
        reader.binding, reader.digest = object(), b'original'
        self.assertIsNone(desktop.text_focus())
        state.presented(reader)
        self.assertEqual(desktop.text_focus(),99)
        reader.can_present = False
        state.presented(reader)
        self.assertTrue(desktop.commit_text(99,'文字'))
        self.assertFalse(desktop.select_all(98))
        self.assertIn('焦点',desktop.pop_input_notice())
        reader.binding = object()
        desktop.select_all(99)
        self.assertEqual(actual,[(99,'文字')])


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
