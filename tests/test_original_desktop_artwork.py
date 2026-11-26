import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from cloud_render import gnome_commands as mesa, gnome_wallpaper as art
from experiments.application_surface import Binding, SurfaceError
from experiments.live_application_surface import LiveSurfaceStream, binding_record
from experiments.mutter_app.controls import ORIGINAL_SHA256
from experiments.original_desktop_bridge import _Graphics


class OriginalDesktopArtwork(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pixels = art.decoded_asset(Path(__file__).resolve().parents[1]/'thin_client/vendor/ubuntu-noble/wallpaper.rgb.xz')
        cls.desc = art.TRANSFER.pack(7,0,art.WIDTH*4,0,0,0,0,art.WIDTH,art.HEIGHT,1,len(cls.pixels))
        cls.ref = art.reference_for_upload(cls.desc, cls.pixels)
        cls.create = mesa._HEADER.pack(10,mesa.CREATE) + mesa._RESOURCE.pack(7,2,67,10,art.WIDTH,art.HEIGHT,1,1,0,0)
        cls.head = mesa._HEADER.pack((len(cls.desc)+len(cls.pixels))//4,mesa.PUT) + cls.desc
        cls.binding = Binding(b's'*32,bytes.fromhex(ORIGINAL_SHA256),47,b'p'*32,8,7,1)
        cls.identity = dict(session=('73'*32),exe=ORIGINAL_SHA256,pid=47,instance=('70'*32),surface=8,handle=7,creation=1)

    def reader(self, data, pixels):
        return LiveSurfaceStream(io.BytesIO(data),session=b's'*32,authorize=lambda b: b==self.binding,
                                 rasterize=lambda frame:self.fail('unexpected app picture'),wallpaper=pixels)

    async def test_bridge_requires_opt_in_and_checks_actual_source_texels(self):
        with tempfile.TemporaryDirectory() as d:
            async def packet(pixels, enabled):
                reader = asyncio.StreamReader()
                reader.feed_data(self.head)
                reader.feed_data(pixels)
                reader.feed_eof()
                bridge = _Graphics(Path(d),reader,self.identity,lambda:None,app_wallpaper=enabled)
                result = await bridge._packet()
                return result,bridge.artwork_references
            result,count = await packet(self.pixels,True)
            self.assertEqual((result,count),(self.ref,1))
            result,count = await packet(self.pixels,False)
            self.assertEqual(count,0)
            self.assertEqual(result[:len(self.head)],self.head)
            self.assertEqual(hashlib.sha256(memoryview(result)[len(self.head):]).digest(),art.RGBX_SHA256)
            changed = bytearray(self.pixels)
            changed[-4] ^= 1
            _,count = await packet(changed,True)
            self.assertEqual(count,0)

    async def test_bound_application_upload_is_never_relabelled_as_wallpaper(self):
        with tempfile.TemporaryDirectory() as d:
            channel = Path(d)
            identity = channel/'binding.json'
            identity.write_text(json.dumps(self.identity))
            identity.chmod(0o600)
            reader = asyncio.StreamReader()
            reader.feed_data(binding_record(0,self.binding)+self.head)
            reader.feed_eof()
            bridge = _Graphics(channel,reader,self.identity,lambda:None,app_wallpaper=True)
            await bridge._packet()
            with self.assertRaisesRegex(SurfaceError,'unclassified upload'):
                await bridge._packet()

    def test_local_reconstruction_consumes_the_original_number_of_logical_bytes(self):
        unref = mesa._HEADER.pack(1,mesa.UNREF) + mesa._WORD.pack(7)
        prefix = self.create + self.ref + unref + self.create
        bound = Binding(b's'*32,bytes.fromhex(ORIGINAL_SHA256),47,b'p'*32,8,7,2)
        offset = len(prefix)-len(self.ref)+len(self.head)+len(self.pixels)
        stream = LiveSurfaceStream(io.BytesIO(prefix+binding_record(offset,bound)), session=b's'*32,
                                   authorize=lambda b:b==bound,rasterize=lambda f:None,wallpaper=self.pixels)
        actual = hashlib.sha256()
        while block := stream.read(65536):
            actual.update(block)
        expected = hashlib.sha256(self.create+self.head)
        expected.update(self.pixels)
        expected.update(unref+self.create)
        self.assertEqual(actual.digest(),expected.digest())
        self.assertEqual((stream.offset,stream.binding,stream.artwork_references),(offset,bound,1))

    def test_unknown_bound_or_corrupt_references_stop_the_session(self):
        for data,asset in ((self.create+self.ref,None),(self.ref,self.pixels),
                           (self.create+self.ref,b'corrupt'),
                           (self.create+self.ref[:-32]+bytes(32),self.pixels),
                           (self.create+binding_record(len(self.create),self.binding)+self.ref,self.pixels),
                           (self.create+self.ref+binding_record(len(self.create)+len(self.head)+len(self.pixels),self.binding),self.pixels)):
            with self.subTest(length=len(data),assetPresent=bool(asset)):
                reader = self.reader(data,asset)
                with self.assertRaises(ValueError):
                    while reader.read(65536):
                        pass
                self.assertFalse(reader.can_present)

