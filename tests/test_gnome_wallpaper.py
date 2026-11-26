import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from cloud_render import gnome_wallpaper as art
from thin_client import virgl_windows as gpu


class OriginalArtwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.asset = art.decoded_asset(root / 'thin_client/vendor/ubuntu-noble/wallpaper.rgb.xz')
        cls.desc = art.TRANSFER.pack(31, 0, art.WIDTH * 4, 0, 0, 0, 0, art.WIDTH, art.HEIGHT, 1, len(cls.asset))
        cls.reference = art.put_reference(cls.desc + cls.asset)

    def test_reconstructs_the_original_put_without_changing_its_descriptor(self):
        self.assertEqual(len(self.reference), 84)
        self.assertEqual(hashlib.sha256(self.asset).digest(), art.RGBX_SHA256)
        opcode, body = next(gpu._requests(io.BytesIO(self.reference), wallpaper=self.asset))
        self.assertEqual(opcode, gpu.PUT)
        self.assertEqual(body[:art.TRANSFER.size], self.desc)
        self.assertEqual(hashlib.sha256(memoryview(body)[art.TRANSFER.size:]).digest(), art.RGBX_SHA256)
        changed = bytearray(body)
        changed[-3] ^= 1
        self.assertIsNone(art.put_reference(changed))
        changed[-3] ^= 1
        changed[16] = 1  # This is now a partial update at x=1.
        self.assertIsNone(art.put_reference(changed))

    def test_rejects_unknown_references_and_corrupt_or_missing_assets(self):
        def forbidden(encoded, artwork):
            with self.assertRaises(gpu.VirglError):
                next(gpu._requests(io.BytesIO(encoded), wallpaper=artwork))
        forbidden(self.reference, None)
        forbidden(self.reference, b'wrong local artwork')
        forbidden(self.reference[:8] + self.desc + bytes(32), self.asset)
        forbidden(art.HEADER.pack(900, art.REFERENCE), self.asset)
        bad = bytearray(self.reference)
        bad[8 + 16] = 1
        forbidden(bad, self.asset)
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / 'wallpaper.rgb.xz'
            with self.assertRaises(FileNotFoundError):
                art.decoded_asset(sample)
            sample.write_bytes(b'broken')
            with self.assertRaises(ValueError):
                art.decoded_asset(sample)

    def test_reference_expansion_budget_is_per_session(self):
        messages = self.reference * (art.MAX_REFERENCES + 1)
        stream = gpu._requests(io.BytesIO(messages), wallpaper=self.asset)
        for _ in range(art.MAX_REFERENCES):
            self.assertEqual(next(stream)[0], gpu.PUT)
        with self.assertRaises(gpu.VirglError):
            next(stream)
        self.assertEqual(next(gpu._requests(io.BytesIO(self.reference), wallpaper=self.asset))[0], gpu.PUT)
