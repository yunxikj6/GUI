import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch

from cloud_render import gnome_wire as wire
from cloud_render.gnome_relay import DesktopService
from instabox import BridgeError
from thin_client import gnome_transport as transport
from thin_client.instabox_transport import InstaBoxControl, TransportError


class Source:
    """A controlled data/metadata pair to test routing, backpressure and input acknowledgement."""
    content = hashlib.shake_256(b'large test drawing').digest(1100000)

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(self.content)
        self.events = asyncio.StreamReader()
        self.events.feed_data(b'{"event":"ready","width":800,"height":520,"frames":1}\n')
        self.events.feed_data(b'{"event":"unavailable","application":"chatgpt"}\n')
        self.events.feed_data(b'{"event":"unavailable","application":"chatgpt"}\n')
        self.events.feed_data(b'{"event":"unavailable","application":"wayland"}\n')
        self.stdin = self
        self.applied = []
        self.end = asyncio.Event()

    def write(self, data):
        self.applied.append(data)
        self.events.feed_data(json.dumps({'event': 'input', 'sequence': len(self.applied), 'framesBefore': 1}).encode() + b'\n')
        self.stdout.feed_data(b'after ' + data)

    async def drain(self):
        pass

    async def wait(self):
        await self.end.wait()
        return 0


class LocalProvider:
    def __init__(self):
        self.lost = set()
        self.calls = []

    def exec(self, args, stdin, timeout):
        req = json.loads(stdin)
        output = subprocess.run(args, input=stdin, capture_output=True, text=True, check=True, timeout=timeout)
        if req['op'] == 'desktop':
            self.calls.append(req['id'])
            if req['id'] not in self.lost:
                self.lost.add(req['id'])
                raise BridgeError('network', 'lost response')
        return output.stdout


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return data


class DesktopSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sources = []
        self.retired = asyncio.Event()

        @asynccontextmanager
        async def original():
            source = Source()
            self.sources.append(source)
            try:
                yield source, source.events
            finally:
                source.end.set()
                self.retired.set()

        self.token = 'j' * 43
        self.service = DesktopService(self.token, original)
        self.server = await asyncio.start_server(self.service.accept, '127.0.0.1', 0, limit=1024)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.service.close()

    def exercise_controls(self, stream):
        with stream:
            self.assertEqual(read_exact(stream, len(Source.content)), Source.content)
            cases = [(stream.send_super, (), b'S'), (stream.move, (799, 519), b'M\x03\x1f\x02\x07'),
                     (stream.click, (4, 9), b'L\x00\x04\x00\x09'),
                     (stream.click, (1, 5, 'right'), b'R\x00\x01\x00\x05'), (stream.key, ('Tab',), b'K\x01')]
            for fn, args, body in cases:
                self.assertTrue(fn(*args))
                self.assertEqual(read_exact(stream, len(b'after ') + len(body)), b'after ' + body)
            self.assertEqual([stream.pop_unavailable() for _ in range(3)], ['chatgpt', 'wayland', None])
            for fn, args in [(stream.move, (800, 1)), (stream.click, (-1, 5)), (stream.key, ('Other',))]:
                with self.assertRaises(ValueError):
                    fn(*args)

    async def test_forwarded_lifecycle_wrong_pair_and_exact_native_inputs(self):
        def local():
            with transport.connect_forwarded(self.port, 'x' * 43) as wrong:
                with self.assertRaises(TransportError):
                    wrong.read(8)
            self.exercise_controls(transport.connect_forwarded(self.port, self.token))
        await asyncio.wait_for(asyncio.to_thread(local), 15)
        await asyncio.wait_for(self.retired.wait(), 3)
        self.assertEqual(len(self.sources), 1)
        self.assertEqual(len(self.sources[0].applied), 5)

    async def test_authenticated_provider_file_retry_and_lost_super_receipt(self):
        box = LocalProvider()
        paths = []
        def local_file(owner, n):
            p = Path('/tmp') / ('clrp-' + owner.session) / f'msg-{n}.bin'
            paths.append(p)
            return p.open('rb')
        def local():
            with patch.object(InstaBoxControl, '_open_file', local_file):
                self.exercise_controls(transport.connect(box, self.port, self.token))
        await asyncio.wait_for(asyncio.to_thread(local), 30)
        await asyncio.wait_for(self.retired.wait(), 3)
        self.assertTrue(paths)
        self.assertTrue(all(not p.parent.exists() for p in paths))
        self.assertEqual(box.calls, [1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
        self.assertEqual(len(self.sources[0].applied), 5)

    async def test_verifies_and_drains_final_frame_before_eof(self):
        stream = await asyncio.to_thread(transport.connect_forwarded, self.port, self.token)
        try:
            while not self.sources:
                await asyncio.sleep(.01)
            s = self.sources[0]
            s.stdout.feed_eof()
            s.events.feed_eof()
            s.end.set()
            self.assertEqual(await asyncio.to_thread(read_exact, stream, len(s.content)), s.content)
            self.assertEqual(await asyncio.to_thread(stream.read, 1), b'')
        finally:
            await asyncio.to_thread(stream.close)

    async def test_close_wakes_an_idle_renderer_and_releases_source(self):
        stream = await asyncio.to_thread(transport.connect_forwarded, self.port, self.token)
        await asyncio.to_thread(read_exact, stream, len(Source.content))
        blocked = asyncio.create_task(asyncio.to_thread(stream.read, 8))
        await asyncio.sleep(.02)
        self.assertFalse(blocked.done())
        await asyncio.to_thread(stream.close)
        self.assertEqual(await asyncio.wait_for(blocked, 2), b'')
        await asyncio.wait_for(self.retired.wait(), 2)

    def test_wrong_cursor_hash_and_expanded_length_fail_before_exposing_bytes(self):
        raw = b'original' * 1000
        pkt, _ = wire.pack_block(raw, 1, wire.ZERO)
        data = pkt[wire.HEADER.size:]
        for body, i, prior in [(data, 2, wire.ZERO), (data, 1, b'x' * 32),
                               (data[:-1], 1, wire.ZERO), (data + b'garbage', 1, wire.ZERO),
                               (data[:4] + struct.pack('!I', 10) + data[8:], 1, wire.ZERO)]:
            with self.assertRaises(wire.DesktopError):
                wire.unpack_block(body, i, prior)
