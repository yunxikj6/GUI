import hashlib
import struct
import unittest
import zlib

from cloud_render import frame_delta as codec


def digest(data):
    return hashlib.sha256(data).digest()


class FrameDelta(unittest.TestCase):
    def test_reuse_across_frames_and_inside_first_frame_preserves_every_byte(self):
        picture = hashlib.shake_256(b'original glyphs').digest(300000)
        first = b'CLRP-a' + picture + picture + picture
        second = b'CLRP-b different header' + picture + b'dynamic region' + picture
        cold = codec.encode_frame(first)
        self.assertLess(len(cold), len(first) // 2)
        self.assertEqual(codec.decode_frame(cold), first)
        warm = codec.encode_frame(second, first)
        self.assertLess(len(warm), len(second) // 10)
        self.assertEqual(codec.decode_frame(warm, first), second)
        for wrong in (b'', b'another session'):
            with self.assertRaises(codec.DeltaError):
                codec.decode_frame(warm, wrong)
        self.assertEqual(codec.encode_frame(b'short'), b'short')

    def test_damaged_truncated_and_bomb_streams_do_not_reach_renderer(self):
        target = b'q' * 1000
        packet = codec.encode_frame(target)
        broken = bytearray(packet)
        broken[-1] ^= 0x40
        magic, size, length, zipped, base, sha = codec._HEADER.unpack_from(packet)
        wrong_hash = codec._HEADER.pack(magic, size, length, zipped, base, bytes(32)) + packet[codec._HEADER.size:]
        def make(body, declared):
            comp = zlib.compress(body)
            return codec._HEADER.pack(magic, size, declared, len(comp), base, sha) + comp
        examples = [
            packet[:-1], packet + b'!', broken, wrong_hash, make(b'q' * 750000, 1005),
            make(codec._COPY.pack(1, 0, 1000), 9), make(codec._COPY.pack(2, 0, 1000), 9),
            make(struct.pack('!BI', 0, 999) + b'!', 6), make(struct.pack('!BI', 3, 1), 5),
        ]
        for bad in examples:
            with self.subTest(length=len(bad)), self.assertRaises(codec.DeltaError):
                codec.decode_frame(bad)

    def test_expansion_is_bounded_in_cpu_and_per_copy_work(self):
        base = b'x' * 50000
        for total, instructions in (
                (1000, codec._COPY.pack(1, 0, 993) + codec._COPY.pack(1, 0, 1) * 7),
                (40000, codec._COPY.pack(1, 0, 40000))):
            comp = zlib.compress(instructions)
            packet = codec._HEADER.pack(b'CDZ1', total, len(instructions), len(comp),
                                        digest(base), digest(base[:total])) + comp
            with self.assertRaises(codec.DeltaError):
                codec.decode_frame(packet, base)
