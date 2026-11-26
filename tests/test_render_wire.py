import asyncio
import io
import struct
import unittest

from cloud_render import wire


class Fragmented(io.BytesIO):
    """A stream with short reads and writes, including inside the envelope."""
    def read(self, size):
        return super().read(min(size, 3))

    def write(self, data):
        return super().write(data[:4])


# Bodies here test transport framing only. The local Skia renderer decodes SKP.
PICTURE = b"skiapict123"


class RenderWire(unittest.TestCase):
    def test_fragmented_ordered_scene_and_click(self):
        frame = wire.Frame(9, 640, 480, [
            wire.Layer(-1.5, 2, 642.5, 478, PICTURE),
            wire.Layer(42, 29, 15, 20, PICTURE + b"layer2")])
        click = wire.Click(9, 11.5, 23)
        stream = Fragmented()
        wire.write_message(stream, frame)
        wire.write_message(stream, click)
        stream.seek(0)
        self.assertEqual(wire.read_message(stream, expected=wire.Frame), frame)
        decoded = wire.read_message(stream, expected=wire.Click)
        self.assertEqual(decoded, click)
        self.assertIsNone(wire.validate_click(decoded, frame))
        self.assertIsNone(wire.read_message(stream))

    def test_golden_bytes_for_other_runtimes(self):
        out = io.BytesIO()
        wire.write_message(out, wire.Frame(1, 64, 32, [wire.Layer(0, 0, 64, 32, PICTURE)]))
        self.assertEqual(out.getvalue(), bytes.fromhex(
            "434c5250 01 01 0000 00000029 00000001 0040 0020 0001 "
            "00000000 00000000 42800000 42000000 0000000b") + PICTURE)
        click = bytes.fromhex("434c5250 01 02 0000 0000000c 00000001 3f800000 40000000")
        self.assertEqual(wire.read_message(io.BytesIO(click)), wire.Click(1, 1, 2))

    def test_ack_is_independent_of_frames_and_rejects_undefined_status(self):
        allowed = (wire.Frame, wire.ClickAck)
        scene = wire.Frame(10, 64, 32, [wire.Layer(0, 0, 64, 32, PICTURE)])
        stream = Fragmented()
        for message in (scene, wire.ClickAck(9, False), wire.ClickAck(10, True)):
            wire.write_message(stream, message)
        stream.seek(0)
        self.assertEqual(wire.read_message(stream, expected=allowed), scene)
        self.assertEqual(wire.read_message(stream, expected=allowed), wire.ClickAck(9, False))
        self.assertEqual(wire.read_message(stream, expected=allowed), wire.ClickAck(10, True))
        success = bytes.fromhex("434c5250 01 03 0000 00000005 0000000a 01")
        self.assertEqual(wire.encode_message(wire.ClickAck(10, True)), success)
        for status in (2, 255):
            with self.assertRaisesRegex(wire.WireError, "status"):
                wire.read_message(io.BytesIO(success[:-1] + bytes([status])), expected=allowed)
        with self.assertRaises(wire.WireError):
            wire.ClickAck(10, 1)
        click = io.BytesIO(wire.encode_message(wire.Click(10, 1, 1)))
        with self.assertRaises(wire.WireError):
            wire.read_message(click, expected=allowed)
        self.assertEqual(click.tell(), 12)

    def test_keyboard_input_wire_bytes_and_unicode_boundaries(self):
        golden = bytes.fromhex("434c5250 01 04 0000 00000006 00000009 01 01")
        self.assertEqual(wire.encode_message(wire.KeyPress(9, 'Tab', wire.SHIFT)), golden)
        self.assertEqual(wire.read_message(Fragmented(golden), wire.INPUT_TYPES), wire.KeyPress(9, 'Tab', 1))
        for value in ('界\t面\n😀e\u0301', '😀' * 1024):
            data = value.encode('utf-8')
            expected = struct.pack("!4sBBHII", b"CLRP", 1, 5, 0, 4 + len(data), 9) + data
            message = wire.TextCommit(9, value)
            self.assertEqual(wire.encode_message(message), expected)
            self.assertEqual(wire.read_message(Fragmented(expected), wire.INPUT_TYPES), message)
        self.assertIs(wire.InputAck, wire.ClickAck)

    def test_input_validation_rejects_invalid_commands_and_text_without_echoing_it(self):
        for key, modifiers in [('a', 0), ('F4', 4), ('Shift+Tab', 0), ('c', 2), ('a', True),
                               ('Tab', 255), ('Enter', 3), ([], 0)]:
            with self.subTest(key=key, modifiers=modifiers), self.assertRaises(wire.WireError):
                wire.KeyPress(9, key, modifiers)
        for bad in ('x' * 4097, '😀' * 1025, '', '\x7f', '\x00', '\x0d', '\ud800'):
            with self.assertRaises(wire.WireError):
                wire.TextCommit(9, bad)
        for kind, body in ((4, b'\0\0\0\x01\0\0'), (4, b'\0\0\0\x01\0\xff'),
                           (5, b'\0\0\0\x01\xff'), (5, b'\0\0\0\x01\xed\xa0\x80')):
            header = struct.pack('!4sBBHI', b'CLRP', 1, kind, 0, len(body))
            with self.assertRaises(wire.WireError):
                wire.read_message(Fragmented(header + body), expected=wire.INPUT_TYPES)
        for kind, length in ((4, 5), (4, 7), (5, 4), (5, 4101)):
            data = struct.pack('!4sBBHI', b'CLRP', 1, kind, 0, length)
            stream = io.BytesIO(data + b'do not read body')
            with self.assertRaises(wire.WireError):
                wire.read_message(stream, expected=wire.INPUT_TYPES)
            self.assertEqual(stream.tell(), len(data))

    def test_every_incomplete_message_is_rejected(self):
        stream = io.BytesIO()
        wire.write_message(stream, wire.Frame(10, 80, 60, [wire.Layer(0, 0, 80, 60, PICTURE)]))
        body = stream.getvalue()
        for size in range(1, len(body)):
            with self.subTest(size=size), self.assertRaises(wire.WireError):
                wire.read_message(Fragmented(body[:size]))
        longer = body[:8] + struct.pack("!I", len(body) - 12 + 1) + body[12:] + b"X"
        with self.assertRaisesRegex(wire.WireError, "trailing"):
            wire.read_message(io.BytesIO(longer))

    def test_bad_headers_and_lengths_before_body_read(self):
        def env(*, magic=b"CLRP", version=1, kind=1, flags=0, size=50):
            return struct.pack("!4sBBHI", magic, version, kind, flags, size)
        for data in (env(magic=b"BAD!"), env(version=0), env(kind=4), env(kind=3), env(flags=1),
                     env(size=wire.MAX_PAYLOAD+1), env(kind=2, size=200), env(size=0)):
            with self.subTest(header=data), self.assertRaises(wire.WireError):
                wire.read_message(io.BytesIO(data))
        stream = io.BytesIO(env() + b"body must remain unread")
        with self.assertRaises(wire.WireError):
            wire.read_message(stream, expected=wire.Click)
        self.assertEqual(stream.tell(), 12)
        scene = struct.pack("!IHHH", 3, 80, 60, 1)
        layer = struct.pack("!ffffI", 0, 0, 80, 60, wire.MAX_PICTURE+1)
        stream = io.BytesIO(env(size=wire.MAX_PAYLOAD) + scene + layer + b"data")
        with self.assertRaisesRegex(wire.WireError, "picture length"):
            wire.read_message(stream)
        self.assertEqual(stream.read(), b"data")

    def test_coordinates_and_sequence_before_dispatch(self):
        scene = wire.Frame(2, 800, 600, [wire.Layer(0, 0, 800, 600, PICTURE)])
        for click in (wire.Click(1, 0, 0), wire.Click(3, 2, 3),
                      wire.Click(2, 800, 0), wire.Click(2, 0, 600)):
            with self.subTest(click=click), self.assertRaises(wire.WireError):
                wire.validate_click(click, scene)
        for bad in (-1, 8193, float("inf"), float("nan"), 10 ** 1000, False):
            with self.subTest(bad=bad), self.assertRaises(wire.WireError):
                wire.Click(2, bad, 0)
        for bad in (0, -1, 2**32, True):
            with self.subTest(bad=bad), self.assertRaises(wire.WireError):
                wire.Click(bad, 0, 0)

    def test_bounded_geometry_and_picture_type(self):
        for width in (0, 1e-100, 32769, float("nan")):
            with self.subTest(width=width), self.assertRaises(wire.WireError):
                wire.Layer(0, 0, width, 60, PICTURE)
        for bad in (b"", b"%PDF-1.7", b"\x89PNG\r\n\x1a\n", b"<script>"):
            with self.subTest(payload=bad), self.assertRaises(wire.WireError):
                wire.Layer(0, 0, 20, 20, bad)
        one = wire.Layer(0, 0, 20, 20, PICTURE)
        for width, height, count in ((0, 20, 1), (8193, 20, 1), (8192, 8192, 1),
                                     (20, 20, 0), (20, 20, wire.MAX_LAYERS+1)):
            with self.subTest(width=width, height=height, count=count), self.assertRaises(wire.WireError):
                wire.Frame(1, width, height, (one,) * count)


class AsyncRenderWire(unittest.IsolatedAsyncioTestCase):
    async def test_reads_whole_message_and_reports_clean_end(self):
        messages = (wire.Frame(3, 20, 10, [wire.Layer(0, 0, 20, 10, PICTURE)]),
                    wire.Click(3, 5.0, 9.25), wire.ClickAck(3, True),
                    wire.KeyPress(3, 'a', wire.CONTROL), wire.TextCommit(4, '原版'))
        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(wire.encode_message(message) for message in messages))
        reader.feed_eof()
        for expected in messages:
            self.assertEqual(await wire.async_read_message(reader, type(expected)), expected)
        self.assertIsNone(await wire.async_read_message(reader))

    async def test_invalid_header_is_rejected_without_waiting_for_any_payload(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!4sBBHI", b"CLRP", 1, 1, 0, wire.MAX_PAYLOAD + 1))
        with self.assertRaises(wire.WireError):
            await asyncio.wait_for(wire.async_read_message(reader), .5)
        for fragment in (b"C", wire.encode_message(wire.Click(1, 2, 3))[:-1]):
            reader = asyncio.StreamReader()
            reader.feed_data(fragment)
            reader.feed_eof()
            with self.assertRaisesRegex(wire.WireError, "truncated"):
                await wire.async_read_message(reader)
