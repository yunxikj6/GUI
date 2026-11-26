import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from cloud_render.wire import Frame, Layer, encode_message
from experiments.application_surface import ApplicationSurfaces, Binding, Commit, CommitTracker, SurfaceError, SurfaceStamp, capture_stable
from experiments.application_surface_diff import compare
from experiments.replay_application_surface import with_native_controls


class WindowTexture(unittest.TestCase):
    def setUp(self):
        self.session = b's'*32
        self.binding = Binding(self.session, b'e'*32, 7, b'p'*32, 31, 24, 2)
        self.full = (24, 0, 16, 0, 0, 0, 0, 4, 3, 1, 48)
        self.asset = encode_message(Frame(1, 4, 3, (Layer(0, 0, 4, 3, b'skiapictfixture'),)))
        self.digest = hashlib.sha256(self.asset).digest()
        self.commit = Commit(self.binding, 13, self.digest)
        self.lookups = {(self.session, 24, 2): self.binding}
        self.commits = {b't'*32: self.commit}
        self.renderer = Mock(return_value=b'abc\xff'*12)
        self.gate = ApplicationSurfaces(self.session, lambda *key: self.lookups.get(key),
                                        lambda session, t: self.commits.get(t), lambda *_: self.asset, self.renderer)
        self.gate.observe_created((23, 2, 67, 10, 4, 3, 1, 1, 0, 0))
        self.gate.observe_created((24, 2, 67, 10, 4, 3, 1, 1, 0, 0))

    def test_partial_damage_reconstructs_complete_verified_original_surface(self):
        self.assertFalse(self.gate.can_present)
        small = (24, 0, 16, 0, 2, 1, 0, 2, 1, 1, 16)
        descriptor, pixels = self.gate.local_texture(small, b't'*32)
        self.assertEqual(descriptor, self.full)
        self.assertEqual(pixels, b'abc\xff'*12)
        self.assertEqual(self.renderer.call_args.args[0].width, 4)
        self.assertTrue(self.gate.can_present)
        with self.assertRaises(SurfaceError):
            self.gate.check_raw_upload(small)
        self.gate.wait_for_picture(24)
        self.assertFalse(self.gate.can_present)

    def test_no_dimensions_or_reused_handle_authorization(self):
        with self.assertRaises(SurfaceError):
            self.gate.local_texture((23, *self.full[1:]), b't'*32)
        self.gate.observe_unreferenced(24)
        self.gate.observe_created((24, 2, 67, 10, 4, 3, 1, 1, 0, 0))
        with self.assertRaises(SurfaceError):
            self.gate.local_texture(self.full, b't'*32)
        self.renderer.assert_not_called()

    def test_changed_or_missing_authority_and_old_serial_cannot_retarget(self):
        wrong = Binding(self.session, b'e'*32, 7, b'p'*32, 32, 24, 2)
        self.gate.local_texture(self.full, b't'*32)
        for commit in (None, Commit(wrong, 14, self.digest), Commit(self.binding, 12, self.digest),
                       Commit(self.binding, 13, b'x'*32), Commit(self.binding, 14, b'x'*32)):
            self.commits[b't'*32] = commit
            with self.assertRaises(SurfaceError):
                self.gate.local_texture(self.full, b't'*32)
            self.assertFalse(self.gate.can_present)
        self.assertEqual(self.renderer.call_count, 1)

    def test_gap_and_reordered_notifications_cannot_authorize_stale_draw(self):
        tracker = CommitTracker(self.binding)
        stamp = lambda n: SurfaceStamp(self.binding, n)
        tracker.synchronize(stamp(31))
        old = tracker.checkpoint()
        tracker.notify(stamp(33))  # 32 missing
        self.assertIsNone(tracker.checkpoint())
        tracker.notify(stamp(32))  # late, cannot fill the gap retroactively
        self.assertIsNone(tracker.checkpoint())
        with self.assertRaises(SurfaceError):
            tracker.synchronize(stamp(32))
        tracker.synchronize(stamp(33))
        self.assertNotEqual(old, tracker.checkpoint())

        calls = []
        async def checkpoint():
            return tracker.checkpoint()
        async def draw():
            calls.append(True)
            if len(calls) == 1:
                tracker.notify(stamp(34))
            return Frame(1, 4, 3, (Layer(0, 0, 4, 3, b'skiapictfixture'),))
        commit, wire = asyncio.run(capture_stable(self.binding, checkpoint, draw))
        self.assertEqual((commit.serial, wire, len(calls)), (34, self.asset, 2))

    def test_long_sequence_recovers_only_from_an_authoritative_read(self):
        tracker = CommitTracker(self.binding)
        tracker.synchronize(SurfaceStamp(self.binding, 1))
        missing = duplicate = recoveries = 0
        for serial in range(2, 50002):
            stamp = SurfaceStamp(self.binding, serial)
            if serial % 353 == 0:
                missing += 1
                continue
            tracker.notify(stamp)
            if serial % 197 == 0:
                duplicate += 1
                tracker.notify(stamp)
                self.assertIsNone(tracker.checkpoint())
            if tracker.checkpoint() is None:
                # A clean subsequent event cannot overwrite the gap marker.
                if serial % 7 == 0:
                    tracker.synchronize(stamp)
                    recoveries += 1
            else:
                self.assertEqual(tracker.checkpoint()[1], stamp)
        self.assertGreater(min(missing, duplicate, recoveries), 100)

    def test_continuous_activity_keeps_surface_pending(self):
        tracker = CommitTracker(self.binding)
        tracker.synchronize(SurfaceStamp(self.binding, 1))
        async def checkpoint():
            return tracker.checkpoint()
        async def draw():
            tracker.notify(SurfaceStamp(self.binding, tracker.high_water + 1))
            return Frame(1, 4, 3, (Layer(0, 0, 4, 3, b'skiapictfixture'),))
        self.assertIsNone(asyncio.run(capture_stable(self.binding, checkpoint, draw)))

    def test_grouped_difference_is_exact_inside_and_outside_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a.png', Path(tmp)/'b.png'
            im = Image.new('RGB', (4, 4)); im.save(a)
            im.putpixel((1, 1), (3, 6, 9)); im.putpixel((3, 3), (0, 0, 6)); im.save(b)
            report = compare(a, b, [(0, 0, 2, 2)])
            self.assertEqual(report['regions'][0]['meanAbsoluteChannelError'], 1.5)
            self.assertEqual(report['regions'][0]['differentPixels'], 1)
            self.assertEqual(report['outsideAllRegions']['differentPixels'], 1)
            self.assertEqual(report['desktop']['differentPixels'], 2)


class NativeCaptionSource(unittest.TestCase):
    def test_original_layers_require_matching_process_viewport_and_browser_quads(self):
        page = Frame(2, 96, 40, (Layer(0, 0, 96, 40, b'skiapictpage'),))
        controls = Frame(1, 64, 36, (Layer(0, 0, 64, 36, b'skiapictbg'),
                                     Layer(0, 0, 32, 36, b'skiapictleft'),
                                     Layer(32, 0, 32, 36, b'skiapictright')))
        style = dict(visible=True, dpr=1, viewport=[96, 40],
                     titlebarRect=dict(left=0, top=0, right=32, height=36))
        proofs = []
        for i, layer in enumerate(controls.layers):
            x, w = 32 + layer.x, layer.width
            proofs.append(dict(sourceName='ChromeNodeCaptionButtonContainer' if i == 0 else 'ChromeNodeCaptionButton',
                               quad=[x, 0, x+w, 0, x+w, 36, x, 36], bytes=len(layer.skp),
                               sha256=hashlib.sha256(layer.skp).hexdigest()))
        receipt = dict(pid=13, actualStyle=style, layers=proofs)
        joined = with_native_controls(page, controls, receipt, {'pid':13}, style)
        self.assertEqual(joined.layers[:1], page.layers)
        self.assertEqual([(layer.x, layer.skp) for layer in joined.layers[1:]],
                         [(32, b'skiapictbg'), (32, b'skiapictleft'), (64, b'skiapictright')])
        cases = [dict(pid=14), dict(actualStyle={**style, 'dpr':2}),
                 dict(layers=[*proofs[:2], {**proofs[2], 'quad': proofs[1]['quad']}]),
                 dict(layers=[*proofs[:2], {**proofs[2], 'sha256': '0'*64}])]
        for change in cases:
            with self.subTest(change=change.keys()), self.assertRaises(SurfaceError):
                with_native_controls(page, controls, {**receipt, **change}, {'pid':13}, style)
