import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import threading
import unittest
from unittest.mock import patch

from cloud_render import gnome_wire as wire
from cloud_render import gnome_input, instabox_relay
from cloud_render.gnome_relay import DesktopService
from instabox import BridgeError
from thin_client import gnome_transport as transport
from thin_client.instabox_transport import InstaBoxControl, TransportError


class Source:
    """A controlled data/metadata pair to test routing, backpressure and input acknowledgement."""
    content = hashlib.shake_256(b'large test drawing').digest(1100000)

    def __init__(self, size=gnome_input.DEFAULT_SIZE):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(self.content)
        self.events = asyncio.StreamReader()
        self.events.feed_data(json.dumps(dict(event='ready', width=size[0], height=size[1], frames=1)).encode() + b'\n')
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
        self.artwork = []
        self.retired = asyncio.Event()

        @asynccontextmanager
        async def original(*, size=gnome_input.DEFAULT_SIZE, wallpaper=False, search_input=False):
            self.artwork.append(wallpaper)
            source = Source(size)
            if search_input:
                source.events.feed_data(b'{"event":"focus","revision":1,"active":true}\n')
                def apply(data):
                    source.applied.append(data)
                    seq = len(source.applied)
                    source.events.feed_data(json.dumps(dict(event='input', sequence=seq, applied=seq == 1)).encode() + b'\n')
                source.write = apply
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

    async def test_explicit_source_size_and_private_provider_pointer_bounds(self):
        def local_file(owner, index):
            return (Path('/tmp') / ('clrp-' + owner.session) / f'msg-{index}.bin').open('rb')

        def check(size, private):
            width, height = size
            box = LocalProvider()
            with patch.object(InstaBoxControl, '_open_file', local_file):
                stream = (transport.connect(box, self.port, self.token, size=size) if private
                          else transport.connect_forwarded(self.port, self.token, size=size))
                with stream:
                    self.assertEqual(read_exact(stream, len(Source.content)), Source.content)
                    with self.assertRaises(ValueError):
                        stream.move(width, height - 1)
                    with self.assertRaises(ValueError):
                        stream.click(width - 1, height)
                    self.assertTrue(stream.click(width - 1, height - 1))
                    if private:
                        self.assertEqual(box.calls, [1, 1])
                    self.assertEqual(read_exact(stream, 11), b'after L' + struct.pack('!HH', width - 1, height - 1))

        for size, private in [((1280, 900), False), ((1920, 1080), True)]:
            self.retired.clear()
            await asyncio.wait_for(asyncio.to_thread(check, size, private), 20)
            await asyncio.wait_for(self.retired.wait(), 3)
            self.assertEqual(len(self.sources[-1].applied), 1)

    async def test_private_and_forwarded_clients_advertise_only_present_artwork(self):
        def local_file(owner, index):
            return (Path('/tmp') / ('clrp-' + owner.session) / f'msg-{index}.bin').open('rb')
        def run(private, available):
            with patch.object(InstaBoxControl, '_open_file', local_file):
                peer = (transport.connect(LocalProvider(), self.port, self.token, wallpaper=available) if private
                        else transport.connect_forwarded(self.port, self.token, wallpaper=available))
                with peer:
                    self.assertEqual(read_exact(peer, len(Source.content)), Source.content)
        for private, available in [(True, True), (False, True), (True, False)]:
            self.retired.clear()
            await asyncio.wait_for(asyncio.to_thread(run, private, available), 12)
            await asyncio.wait_for(self.retired.wait(), 3)
        self.assertEqual(self.artwork, [True, True, False])

    async def test_old_service_uses_original_800_stream_when_client_has_new_artwork(self):
        logins = []
        async def legacy(reader, writer):
            login = await reader.readline()
            logins.append(login)
            if login == wire.AUTH + self.token.encode('ascii') + b'\n':
                writer.write(wire.packet(wire.READY, wire.DIMENSIONS.pack(800, 520)))
                data, _ = wire.pack_block(b'original graphics', 1, wire.ZERO)
                writer.write(data)
                await writer.drain()
                await reader.read()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(legacy, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        def local():
            with transport.connect(LocalProvider(), port, self.token, wallpaper=True, search_input=True) as peer:
                self.assertEqual(read_exact(peer, 17), b'original graphics')
                self.assertIsNone(peer.text_focus())
        try:
            await asyncio.wait_for(asyncio.to_thread(local), 12)
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(logins, [wire.QUERY + self.token.encode('ascii') + b'\n',
                                  wire.AUTH + self.token.encode('ascii') + b'\n'])

    async def test_search_commit_lost_reply_stale_target_and_new_session(self):
        def local_file(owner, index):
            return (Path('/tmp') / ('clrp-' + owner.session) / f'msg-{index}.bin').open('rb')

        def exercise(private, previous):
            with patch.object(InstaBoxControl, '_open_file', local_file):
                provider = LocalProvider()
                stream = (transport.connect(provider, self.port, self.token, search_input=True) if private else
                          transport.connect_forwarded(self.port, self.token, search_input=True))
                with stream:
                    self.assertEqual(read_exact(stream, len(Source.content)), Source.content)
                    revision = stream.text_focus()
                    self.assertTrue(revision)
                    if previous is not None:
                        self.assertFalse(stream.commit_text(previous, '旧会话'))
                    self.assertTrue(stream.commit_text(revision, '搜索😀'))
                    self.assertFalse(stream.commit_text(revision, '已失焦'))
                    if private:
                        self.assertEqual(provider.calls, [1, 1, 2, 2])
                    return revision

        last = None
        for private in (True, False):
            self.retired.clear()
            last = await asyncio.wait_for(asyncio.to_thread(exercise, private, last), 15)
            await asyncio.wait_for(self.retired.wait(), 3)
            self.assertEqual([gnome_input.decode_input(data)[1] for data in self.sources[-1].applied],
                             [(1, '搜索😀'), (1, '已失焦')])

    def test_unicode_input_length_and_framing(self):
        for text in ('中文', '👩‍💻', 'e\u0301', '人' * 1365 + 'a'):
            raw = gnome_input.encode_text(1, text)
            self.assertEqual(gnome_input.decode_input(raw), (b'T', (1, text)))
            self.assertEqual(gnome_input.command_size(raw[:3]), None)
            self.assertEqual(gnome_input.command_size(raw[:7]), len(raw))
        for text in ('', 'a\n', '\u0085', '\u2028', '\u2029', '\udc00', '人' * 1366):
            with self.assertRaises(ValueError):
                gnome_input.encode_text(1, text)
        for malformed in (b'T', b'T' + bytes(6), b'T' + struct.pack('!IH', 1, 1) + b'\x80'):
            with self.assertRaises(ValueError):
                gnome_input.decode_input(malformed)

    async def test_bad_size_or_pair_never_starts_a_source(self):
        for token, size in [(self.token, (1919, 1080)), ('x' * 43, (1280, 900))]:
            reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
            writer.write(wire.AUTH_SIZE + token.encode('ascii') + b'\n' + wire.SESSION.pack(*size, 0))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
            writer.close()
            await writer.wait_closed()
        self.assertFalse(self.sources)

    def test_ready_must_match_request_and_helper_sizes_match_source(self):
        self.assertEqual(instabox_relay.GNOME_SIZES, gnome_input.SUPPORTED_SIZES)
        class WrongReady:
            def receive(self):
                return wire.READY, wire.DIMENSIONS.pack(800, 520)
            def close(self):
                pass
        with transport.DesktopStream(WrongReady(), size=(1280, 900)) as stream:
            with self.assertRaises(TransportError):
                stream.read(1)
        start = dict(op='start', session='a' * 32, port=2, pair='b' * 43)
        for extra in (dict(width=1280), dict(protocol='gnome', height=900), dict(protocol='gnome', width=1280.0, height=900),
                      dict(width=1920, height=1080), dict(protocol='gnome', width=1920, height=1200)):
            with self.assertRaises(instabox_relay.RelayError):
                instabox_relay.check_request({**start, **extra})

    async def test_private_source_ack_survives_lost_responses_and_blocked_media(self):
        # Stall the Windows poll after READY. The source must really apply both
        # actions before local confirmation, while every older draw block and
        # both ordered ACKs remain available for subsequent replay.
        seen = []
        allow_ack = asyncio.Event()
        accepted = asyncio.Event()
        finish = asyncio.Event()
        async def original(reader, writer):
            self.assertEqual(await reader.readline(), wire.AUTH + self.token.encode('ascii') + b'\n')
            writer.write(wire.packet(wire.READY, wire.DIMENSIONS.pack(800, 520)))
            chain = wire.ZERO
            for index in range(1, 17):
                packet, chain = wire.pack_block(bytes([index]) * 32, index, chain)
                writer.write(packet)
            await writer.drain()
            for number in (1, 2):
                kind, body = await wire.read_async(reader, {wire.INPUT})
                self.assertEqual((kind, body), (wire.INPUT, wire.SEQUENCE.pack(number) + b'S'))
                seen.append(number)
                accepted.set()
                await allow_ack.wait()
                writer.write(wire.packet(wire.ACK, wire.INPUT_ACK.pack(number, 1)))
                await writer.drain()
            await finish.wait()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(original, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        class BlockedProvider(LocalProvider):
            def __init__(self):
                super().__init__()
                self.unblock = threading.Event()
            def exec(self, args, stdin, timeout):
                req = json.loads(stdin)
                if req['op'] == 'poll' and req['after'] >= 1:
                    self.unblock.wait(8)
                return super().exec(args, stdin, timeout)
        provider = BlockedProvider()
        stream = await asyncio.to_thread(transport.connect, provider, port, self.token)
        try:
            first = asyncio.create_task(asyncio.to_thread(stream.send_super))
            await asyncio.wait_for(accepted.wait(), 3)
            self.assertFalse(first.done())
            allow_ack.set()
            self.assertTrue(await asyncio.wait_for(first, 3))
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(stream.send_super), 3))
            self.assertEqual(seen, [1, 2])
            self.assertEqual(provider.calls, [1, 1, 2, 2])
            self.assertEqual((stream.confirmed, stream.ordered_ack), (2, 0))
            provider.unblock.set()
            draw = await asyncio.wait_for(asyncio.to_thread(read_exact, stream, 16 * 32), 7)
            self.assertEqual(draw, b''.join(bytes([index]) * 32 for index in range(1, 17)))
            for _ in range(100):
                if stream.ordered_ack == 2:
                    break
                await asyncio.sleep(.025)
            self.assertEqual(stream.ordered_ack, 2)
        finally:
            provider.unblock.set()
            finish.set()
            await asyncio.to_thread(stream.close)
            server.close()
            await server.wait_closed()

    async def test_missing_both_http_input_replies_accepts_only_the_ordered_source_ack(self):
        class NoInputReplies(LocalProvider):
            def exec(self, args, stdin, timeout):
                req = json.loads(stdin)
                if req['op'] == 'desktop':
                    self.calls.append(req['id'])
                    subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, check=True)
                    raise BridgeError('network', 'missing HTTP response')
                return super().exec(args, stdin, timeout)
        provider = NoInputReplies()
        def local_file(owner, index):
            return (Path('/tmp') / ('clrp-' + owner.session) / f'msg-{index}.bin').open('rb')
        def local():
            with patch.object(InstaBoxControl, '_open_file', local_file):
                with transport.connect(provider, self.port, self.token) as stream:
                    self.assertEqual(read_exact(stream, len(Source.content)), Source.content)
                    self.assertTrue(stream.send_super())
                    self.assertEqual(stream.ordered_ack, 1)
        await asyncio.wait_for(asyncio.to_thread(local), 10)
        self.assertEqual(self.sources[0].applied, [b'S'])
        self.assertEqual(provider.calls, [1, 1])

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
