import hashlib
import io
import unittest

from cloud_render import gnome_commands as mesa
from cloud_render.wire import Frame, Layer, encode_message
from experiments.application_surface import Binding, SurfaceError
from experiments.live_application_surface import (
    DESKTOP, LiveSurfaceStream, WitnessedPut, binding_record, replacement_record, stream_witnessed,
)


class Fragmented(io.BytesIO):
    def read(self, size):
        return super().read(min(size, 3))


class IncrementalOriginalSurface(unittest.TestCase):
    def setUp(self):
        self.binding = Binding(b's'*32, b'e'*32, 84, b'p'*32, 19, 23, 1)
        self.create = mesa._HEADER.pack(10, mesa.CREATE) + mesa._RESOURCE.pack(23, 2, 67, 10, 2, 2, 1, 1, 0, 0)
        self.desc = (23, 0, 8, 0, 0, 0, 0, 2, 2, 1, 16)
        self.header = mesa._HEADER.pack(15, mesa.PUT)
        self.raw = self.header + mesa._TRANSFER.pack(*self.desc) + b'cloud-data123456'
        self.frame = encode_message(Frame(1, 2, 2, (Layer(0, 0, 2, 2, b'skiapictunit'),)))
        self.digest = hashlib.sha256(self.frame).digest()
        self.pixels = b'xyz\xff' * 4
        self.local = self.header + mesa._TRANSFER.pack(*self.desc) + self.pixels

    def reader(self, stream):
        return LiveSurfaceStream(stream, session=self.binding.session,
                                 authorize=lambda b:b == self.binding,
                                 rasterize=lambda frame:self.pixels)

    def test_fragmented_private_channel_reconstructs_reuse_and_withheld_revision(self):
        original = self.create + self.raw*4
        decisions = iter([None, self.digest, None, self.digest])
        def inspect(offset, ordinal, desc, create, pixels):
            self.assertEqual((desc,create,bytes(pixels)), (self.desc,1,self.raw[-16:]))
            return WitnessedPut(self.binding, ordinal, next(decisions))
        sink = io.BytesIO()
        metrics = stream_witnessed(io.BytesIO(original),sink,inspect_put=inspect,fetch_picture=lambda *_:self.frame)
        self.assertEqual(metrics,{'originalBytes':len(original),'removedApplicationBytes':64,'drawings':1,'pending':2})
        self.assertNotIn(self.raw[-16:],sink.getvalue())
        client = self.reader(Fragmented(sink.getvalue()))
        self.assertEqual(b''.join(iter(lambda:client.read(11),b'')),self.create+self.local*2)
        self.assertTrue(client.can_present)
        self.assertEqual((client.drawings,client.reuses,client.holds), (1,1,2))

    def test_stale_offset_generated_before_update_and_disallowed_reuse_never_present(self):
        base=self.create+binding_record(len(self.create),self.binding)
        hold=replacement_record(len(self.create),7,self.desc)
        next_offset=len(self.create)+len(self.raw)
        invalid=[hold,
                 replacement_record(next_offset,6,self.desc,digest=self.digest,picture=self.frame),
                 replacement_record(next_offset,8,self.desc,digest=self.digest),
                 replacement_record(next_offset,8,self.desc,digest=b'x'*32,picture=self.frame)]
        for op in invalid:
            with self.subTest(length=len(op)):
                client=self.reader(Fragmented(base+hold+op))
                self.assertEqual(client.read(4096),self.create)
                with self.assertRaises(SurfaceError):client.read(4096)
                self.assertFalse(client.can_present)
                with self.assertRaises(SurfaceError):client.read(3)

    def test_rejects_raw_payload_before_read_and_rebind_after_unwitnessed_put(self):
        base=self.create+binding_record(len(self.create),self.binding)
        stream=io.BytesIO(base+self.raw);client=self.reader(stream)
        self.assertEqual(client.read(4096),self.create)
        with self.assertRaises(SurfaceError):client.read(4096)
        self.assertEqual(stream.read(),self.raw[-16:])
        sink=io.BytesIO();decisions=iter([DESKTOP,WitnessedPut(self.binding,2,self.digest)])
        with self.assertRaises(SurfaceError):
            stream_witnessed(io.BytesIO(self.create+self.raw*2),sink,inspect_put=lambda *_:next(decisions),
                             fetch_picture=lambda *_:self.frame)
        self.assertEqual(sink.getvalue(),self.create+self.raw)
        sink=io.BytesIO()
        with self.assertRaises(SurfaceError):
            stream_witnessed(io.BytesIO(self.create+self.raw),sink,inspect_put=lambda *_:None,
                             fetch_picture=lambda *_:self.frame)
        self.assertEqual(sink.getvalue(),self.create)

    def test_missing_record_and_changed_process_are_rejected_before_local_drawing(self):
        off=len(self.create)
        binding=Binding(self.binding.session,self.binding.executable_sha256,42,self.binding.process_instance,
                        self.binding.surface_id,self.binding.handle,1)
        for stream in (binding_record(off,binding),binding_record(off+1,self.binding),
                       binding_record(off,self.binding)+replacement_record(off+len(self.raw),11,self.desc)):
            client=self.reader(Fragmented(self.create+stream));client.read(4096)
            with self.assertRaises(SurfaceError):client.read(4096)
            self.assertFalse(client.can_present)

    def test_long_stream_holds_gaps_and_reuses_only_the_current_drawing(self):
        data = io.BytesIO(self.create + binding_record(len(self.create), self.binding))
        data.seek(0, 2)
        offset = len(self.create)
        ping = mesa._HEADER.pack(0, mesa.PING)
        for serial in range(1, 5001):
            if serial % 5:
                digest = self.digest
            else:
                digest = None
            picture = self.frame if serial == 1 else None
            data.write(replacement_record(offset, serial, self.desc, digest=digest, picture=picture))
            data.write(ping)
            offset += len(self.raw) + len(ping)
        data.seek(0)
        calls = []
        client = self.reader(data)
        client.rasterize = lambda frame: calls.append(frame.sequence) or self.pixels
        self.assertEqual(client.read(1024), self.create)
        for serial in range(1, 5001):
            if serial % 5:
                self.assertEqual(client.read(1024), self.local)
            self.assertEqual(client.read(1024), ping)
            self.assertEqual(client.can_present, bool(serial % 5))
        self.assertEqual(client.read(1024), b'')
        self.assertEqual((client.drawings, client.reuses, client.holds), (1, 3999, 1000))
        self.assertEqual(calls, [1])
