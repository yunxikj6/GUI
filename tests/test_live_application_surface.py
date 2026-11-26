import hashlib
import io
from dataclasses import replace
import unittest
from unittest.mock import patch

from cloud_render import frame_delta, gnome_commands as mesa
from cloud_render.wire import Frame, Layer, encode_message
from experiments.application_surface import Binding, SurfaceError
from experiments.live_application_surface import (
    DESKTOP, DRAW_DELTA, DRAW_PREFIX_BYTES, LiveSurfaceStream, WitnessedPut, binding_record, replacement_record, stream_witnessed,
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

    def test_cached_native_textures_follow_only_verified_draws_in_this_binding(self):
        frames = {
            i: encode_message(Frame(i, 2, 2, (Layer(0, 0, 2, 2, b'skiapictunit'),)))
            for i in (1, 2, 3)
        }
        for budget, rendered in ((32, [1, 2, 3, 2]), (16, [1, 2, 1, 3, 2])):
            with self.subTest(budget=budget), patch('experiments.live_application_surface._TEXTURE_CACHE_BYTES', budget):
                offset = len(self.create)
                records = [self.create, binding_record(offset, self.binding)]
                for serial, phase in enumerate((1, 2, None, 1, 3, 2), 1):
                    picture = frames.get(phase)
                    digest = hashlib.sha256(picture).digest() if picture else None
                    records.append(replacement_record(offset, serial, self.desc, digest=digest, picture=picture))
                    offset += len(self.raw)
                records += [mesa._HEADER.pack(1, mesa.UNREF), mesa._WORD.pack(self.binding.handle)]
                client = self.reader(io.BytesIO(b''.join(records)))
                calls = []
                def rasterize(frame):
                    calls.append(frame.sequence)
                    return bytes((frame.sequence, 0, 0, 255)) * 4
                client.rasterize = rasterize
                client.read(4096)
                for phase in (1, 2, 1, 3, 2):
                    output = client.read(4096)
                    self.assertEqual(output[-16:], bytes((phase, 0, 0, 255))*4)
                client.read(4096)
                self.assertEqual(calls, rendered)
                self.assertEqual(client.holds, 1)
                self.assertFalse(client._surfaces)

    def test_prior_rgba_never_authorizes_a_missing_original_drawing(self):
        other = encode_message(Frame(2, 2, 2, (Layer(0, 0, 2, 2, b'skiapictunit'),)))
        second = hashlib.sha256(other).digest()
        offset = len(self.create)
        stream = [self.create, binding_record(offset, self.binding)]
        for serial, digest, picture in ((1, self.digest, self.frame), (2, second, other), (3, self.digest, None)):
            stream.append(replacement_record(offset, serial, self.desc, digest=digest, picture=picture))
            offset += len(self.raw)
        client = self.reader(io.BytesIO(b''.join(stream)))
        self.assertEqual(client.read(4096), self.create)
        self.assertEqual(client.read(4096), self.local)
        self.assertEqual(client.read(4096), self.local)
        with self.assertRaises(SurfaceError):
            client.read(4096)
        self.assertFalse(client._surfaces)
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


class TwoOriginalSurfaces(unittest.TestCase):
    def setUp(self):
        self.app = Binding(b's' * 32, b'a' * 32, 43, b'p' * 32, 14, 23, 1)
        self.browser = replace(self.app, executable_sha256=b'b' * 32, pid=47,
                               process_instance=b'q' * 32, surface_id=18, handle=25, creation=2)
        self.assets = {}
        self.app_drawing = self.picture(1, b'application')
        self.browser_drawing = self.picture(2, b'browser')
        self.decisions = []
        self.source = io.BytesIO()
        self.ping = mesa._HEADER.pack(0, mesa.PING)

    def picture(self, sequence, text):
        raw = encode_message(Frame(sequence, 2, 2, (Layer(0, 0, 2, 2, b'skiapict' + text),)))
        digest = hashlib.sha256(raw).digest()
        self.assets[digest] = raw
        return digest

    def create(self, binding):
        body = mesa._RESOURCE.pack(binding.handle, 2, 67, 10, 2, 2, 1, 1, 0, 0)
        self.source.write(mesa._HEADER.pack(10, mesa.CREATE) + body)

    def unref(self, binding):
        self.source.write(mesa._HEADER.pack(1, mesa.UNREF) + mesa._WORD.pack(binding.handle))

    @staticmethod
    def descriptor(binding):
        return (binding.handle, 0, 8, 0, 0, 0, 0, 2, 2, 1, 16)

    @staticmethod
    def pixels(value):
        return bytes((value, value, value, 255)) * 4

    def put(self, binding, serial, digest):
        desc = mesa._TRANSFER.pack(*self.descriptor(binding))
        self.source.write(mesa._HEADER.pack(15, mesa.PUT) + desc + b'private original')
        self.decisions.append(WitnessedPut(binding, serial, digest))

    def forward(self, *, multiple=True):
        decisions = iter(self.decisions)
        sink = io.BytesIO()
        result = stream_witnessed(io.BytesIO(self.source.getvalue()), sink,
                                  inspect_put=lambda *_: next(decisions),
                                  fetch_picture=lambda digest, _: self.assets[digest],
                                  multiple_surfaces=multiple)
        self.assertNotIn(b'private original', sink.getvalue())
        return sink.getvalue(), result

    def client(self, raw, *, multiple=True, approve=None):
        return LiveSurfaceStream(Fragmented(raw), session=self.app.session,
                                 authorize=approve or (lambda b: b in (self.app, self.browser)),
                                 rasterize=lambda f: self.pixels(f.sequence),
                                 multiple_surfaces=multiple)

    def test_independent_revisions_damage_and_close_restore_first_window(self):
        self.create(self.app)
        self.put(self.app, 47, self.app_drawing)
        self.create(self.browser)
        self.put(self.browser, 1, None)
        self.source.write(self.ping)
        self.put(self.app, 48, self.app_drawing)
        self.put(self.browser, 2, self.browser_drawing)
        self.put(self.app, 49, None)
        self.put(self.browser, 3, self.browser_drawing)
        self.put(self.app, 50, self.app_drawing)
        self.unref(self.browser)
        raw, counts = self.forward()
        self.assertEqual((counts['drawings'], counts['pending'], counts['removedApplicationBytes']), (2, 2, 112))

        client = self.client(raw)
        client.read(4096)  # A created.
        self.assertEqual(client.read(4096)[-16:], self.pixels(1))
        first_view = client.commit
        self.assertEqual(client.binding, self.app)
        client.read(4096)  # B created.
        self.assertEqual(client.read(4096), self.ping)
        self.assertFalse(client.can_present)
        self.assertIsNone(client.binding)
        self.assertEqual(client.read(4096)[-16:], self.pixels(1))
        self.assertFalse(client.can_present)  # A's revision cannot grant B.
        self.assertEqual(client.read(4096)[-16:], self.pixels(2))
        self.assertTrue(client.can_present)
        self.assertIsNone(client.binding)  # No single-app focused text target.
        self.assertEqual(client.read(4096)[-16:], self.pixels(2))
        self.assertFalse(client.can_present)  # A is now held.
        self.assertEqual(client.read(4096)[-16:], self.pixels(1))
        self.assertTrue(client.can_present)
        self.assertEqual(mesa._HEADER.unpack_from(client.read(4096))[1], mesa.UNREF)
        self.assertEqual(client.binding, self.app)
        self.assertEqual(client.digest, self.app_drawing)
        self.assertNotEqual(client.commit, first_view)
        self.assertEqual(client.read(4096), b'')

    def test_each_window_needs_its_own_authority_and_initial_picture(self):
        self.create(self.app)
        self.put(self.app, 1, self.app_drawing)
        self.create(self.browser)
        offset = self.source.tell()
        self.put(self.browser, 1, self.app_drawing)
        raw, counts = self.forward()
        self.assertEqual(counts['drawings'], 2)  # Identical bytes still travel for each independent grant.
        client = self.client(raw)
        calls = []
        client.rasterize = lambda f: calls.append(f.sequence) or self.pixels(len(calls))
        packets = list(iter(lambda: client.read(4096), b''))
        self.assertEqual([p[-16:] for p in packets if mesa._HEADER.unpack_from(p)[1] == mesa.PUT],
                         [self.pixels(1), self.pixels(2)])
        self.assertEqual(calls, [1, 1])

        for candidate in (self.client(raw, multiple=False), self.client(raw, approve=lambda b: b == self.app)):
            with self.assertRaises(SurfaceError):
                list(iter(lambda: candidate.read(4096), b''))
            self.assertFalse(candidate.can_present)
            self.assertIsNone(candidate.binding)
        with self.assertRaises(SurfaceError):
            self.forward(multiple=False)

        # Turn the second surface's first DRAW into REUSE of A's exact digest.
        registration = binding_record(offset, self.browser)
        start_put = raw.index(registration) + len(registration)
        wrong = replacement_record(offset, 1, self.descriptor(self.browser), digest=self.app_drawing)
        candidate = self.client(raw[:start_put] + wrong)
        with self.assertRaises(SurfaceError):
            list(iter(lambda: candidate.read(4096), b''))
        self.assertFalse(candidate.can_present)

    def test_delayed_second_grant_and_handle_recycling_remain_scoped(self):
        self.create(self.app)
        self.put(self.app, 1, self.app_drawing)
        self.create(self.browser)
        self.put(self.browser, 97, None)
        self.unref(self.browser)
        recycled = replace(self.browser, creation=3)
        self.create(recycled)
        self.put(recycled, 1, self.browser_drawing)
        raw, _ = self.forward()
        client = self.client(raw, approve=lambda b: b in (self.app, self.browser, recycled))
        client.read(4096)
        client.read(4096)
        first_view = client.commit
        client.read(4096)
        self.assertEqual(mesa._HEADER.unpack_from(client.read(4096))[1], mesa.UNREF)
        self.assertTrue(client.can_present)
        self.assertNotEqual(client.commit, first_view)  # A's drawing remained exactly the same.
        client.read(4096)
        self.assertEqual(client.read(4096)[-16:], self.pixels(2))
        self.assertTrue(client.can_present)

    def test_unrecognized_or_excessive_source_lifetimes_stop_before_their_pixels(self):
        self.create(self.app)
        self.put(self.app, 3, self.app_drawing)
        self.create(self.browser)
        self.put(self.browser, 3, self.browser_drawing)
        for rogue, create in ((replace(self.app, pid=999), False),
                              (replace(self.browser, handle=28, creation=3), True)):
            with self.subTest(binding=rogue):
                end, count = self.source.tell(), len(self.decisions)
                if create:
                    self.create(rogue)
                self.put(rogue, 4, self.app_drawing)
                with self.assertRaises(SurfaceError):
                    self.forward()
                self.source.seek(end)
                self.source.truncate()
                del self.decisions[count:]


    def test_interleaved_delta_dictionary_belongs_to_its_creation(self):
        base = self.picture(1, b'A' * 40000 + b'B' * 40000)
        update = self.picture(2, b'A' * 40000 + b'B' * 40000)
        patch = frame_delta.encode_frame(self.assets[update], self.assets[base])
        with self.assertRaises(frame_delta.DeltaError):
            frame_delta.decode_frame(patch, self.assets[self.browser_drawing])
        self.create(self.app)
        self.put(self.app, 23, base)
        self.create(self.browser)
        self.put(self.browser, 1, self.browser_drawing)
        prefix, _ = self.forward()
        offset = self.source.tell()
        full = replacement_record(offset, 24, self.descriptor(self.app), digest=update, picture=self.assets[update])
        body = full[mesa._HEADER.size : mesa._HEADER.size + DRAW_PREFIX_BYTES] + patch
        raw = prefix + mesa._HEADER.pack(len(body), DRAW_DELTA) + body
        client = LiveSurfaceStream(io.BytesIO(raw), session=self.app.session, authorize=lambda b: b in
                                   (self.app, self.browser), rasterize=lambda f: self.pixels(f.sequence),
                                   picture_delta=True, multiple_surfaces=True)
        packets = list(iter(lambda: client.read(4096), b''))
        self.assertTrue(client.can_present)
        self.assertEqual(packets[-1][-16:], self.pixels(2))
