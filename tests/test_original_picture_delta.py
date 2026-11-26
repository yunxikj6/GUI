import asyncio
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import random
import tempfile
import unittest

from cloud_render import frame_delta, gnome_commands as mesa
from cloud_render.wire import Frame, Layer, encode_message
from experiments.application_surface import SurfaceError
from experiments.live_application_surface import (
    DRAW, DRAW_DELTA, DRAW_PREFIX_BYTES, LiveSurfaceStream, binding_record, replacement_record,
)
from experiments.mutter_app.controls import ORIGINAL_SHA256
from experiments.original_desktop_bridge import _Graphics
from experiments.original_desktop_protocol import approved_binding


class OriginalPictureDelta(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.meta = dict(session='73'*32, exe=ORIGINAL_SHA256, pid=37, instance='70'*32,
                         surface=11, handle=7, creation=1)
        self.binding = approved_binding(self.meta)
        self.create = mesa._HEADER.pack(10, mesa.CREATE) + mesa._RESOURCE.pack(7,2,67,10,2,2,1,1,0,0)
        self.unref = mesa._HEADER.pack(1, mesa.UNREF) + mesa._WORD.pack(7)
        self.desc = (7,0,8,0,0,0,0,2,2,1,16)
        self.pixels = b'pix\xff'*4
        self.put = mesa._HEADER.pack(15, mesa.PUT) + mesa._TRANSFER.pack(*self.desc) + self.pixels
        asset = b'skiapict' + random.Random(12).randbytes(150000)
        self.pictures = [encode_message(Frame(n,2,2,(Layer(0,0,2,2,asset),))) for n in (1,2)]
        self.offset = len(self.create)
        self.prefix = self.create + binding_record(self.offset, self.binding)
        self.first, self.second = [self.record(n, picture) for n,picture in enumerate(self.pictures,1)]
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.channel = Path(self.private.name)
        witness = self.channel/'binding.json'
        witness.write_text(json.dumps(self.meta))
        witness.chmod(0o600)

    def record(self, serial, picture):
        return replacement_record(self.offset + (serial-1)*len(self.put), serial, self.desc,
                                  digest=hashlib.sha256(picture).digest(), picture=picture)

    async def relay(self, raw, enabled):
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        bridge = _Graphics(self.channel, reader, self.meta, lambda:None, picture_delta=enabled)
        chunks = []
        while chunk := await bridge._packet():
            chunks.append(chunk)
        return bridge, chunks

    def receiver(self, raw, enabled):
        return LiveSurfaceStream(io.BytesIO(raw), session=self.binding.session, authorize=lambda b: (
                b == self.binding or b == replace(self.binding, creation=2)),
                rasterize=lambda frame:self.pixels, picture_delta=enabled)

    async def test_reconstruct_across_hold_and_reuse_and_clear_on_unref(self):
        hold = replacement_record(self.offset + 2*len(self.put), 3, self.desc)
        reuse = replacement_record(self.offset + 3*len(self.put), 4, self.desc,
                                   digest=hashlib.sha256(self.pictures[1]).digest())
        raw = self.prefix + self.first + self.second + hold + reuse + self.unref
        legacy, old = await self.relay(raw, False)
        self.assertEqual(b''.join(old), raw)
        self.assertFalse(legacy.previous_picture)
        bridge, chunks = await self.relay(raw, True)
        self.assertEqual(mesa._HEADER.unpack_from(chunks[2])[1], DRAW)
        self.assertEqual(mesa._HEADER.unpack_from(chunks[3])[1], DRAW_DELTA)
        self.assertLess(len(chunks[3]), len(self.second)//5)
        self.assertFalse(bridge.previous_picture)
        client = self.receiver(b''.join(chunks), True)
        self.assertEqual(client.read(100), self.create)
        for picture in self.pictures:
            self.assertEqual(client.read(100), self.put)
            self.assertEqual(client.picture, picture)
        self.assertEqual(client.read(100), self.put)
        self.assertEqual((client.drawings,client.reuses,client.holds),(2,1,1))
        self.assertEqual(client.read(100), self.unref)
        self.assertIsNone(client.picture)
        self.assertEqual(client.read(100), b'')

    async def test_reject_unnegotiated_corruption_and_cross_lifetime_dictionary(self):
        _, chunks = await self.relay(self.prefix + self.first + self.second, True)
        patch = chunks[-1]
        corrupt = patch[:-1] + bytes([patch[-1] ^ 1])
        outer = bytearray(patch)
        outer[mesa._HEADER.size + DRAW_PREFIX_BYTES - 1] ^= 1
        after_rebind = self.offset + len(self.put) + len(self.unref) + len(self.create)
        new_patch = bytearray(patch)
        new_patch[8:16] = after_rebind.to_bytes(8,'little')
        rebind = self.unref + self.create + binding_record(after_rebind, replace(self.binding, creation=2))
        prefix = self.prefix + self.first
        for data, enabled in ((prefix+patch,False), (prefix+corrupt,True), (prefix+outer,True),
                              (prefix+patch[:-8],True), (prefix+rebind+new_patch,True)):
            client = self.receiver(data, enabled)
            with self.assertRaises(SurfaceError):
                while client.read(4096):
                    pass
            self.assertFalse(client.can_present)
            self.assertIsNone(client.picture)

    async def test_keep_raw_draw_when_gvgl_deflate_is_smaller(self):
        picture = encode_message(Frame(1,2,2,(Layer(0,0,2,2,b'skiapict'+bytes(8192)),)))
        self.assertLess(len(frame_delta.encode_frame(picture)), len(picture))
        raw = self.prefix + self.record(1, picture)
        _, chunks = await self.relay(raw, True)
        self.assertEqual(b''.join(chunks), raw)

    async def test_raw_small_draw_and_wrong_resource(self):
        picture = encode_message(Frame(1,2,2,(Layer(0,0,2,2,b'skiapicttiny'),)))
        raw = self.prefix + self.record(1, picture)
        _, chunks = await self.relay(raw, True)
        self.assertEqual(b''.join(chunks), raw)
        invalid = bytearray(self.first)
        invalid[24:28] = (19).to_bytes(4,'little')
        with self.assertRaisesRegex(SurfaceError, 'binding'):
            await self.relay(self.prefix+invalid, True)
