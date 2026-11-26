import io
from pathlib import Path
import struct
import tempfile
import unittest

from thin_client import virgl_windows as vr


def msg(opcode, body=b''):
    length = len(body) if opcode == vr.RENDERER else (len(body) + 3) // 4
    return vr.HEADER.pack(length, opcode) + body


START = msg(vr.RENDERER, b'gnome-shell')
DISPLAY = (1, 2, 2, 138, 4, 2, 1, 1, 0, 0)
DESC = (1, 0, 0, 0, 0, 0, 0, 4, 2, 1, 32)


class Chunked(io.BytesIO):
    def read(self, n):
        return super().read(min(n, 3))


class Gpu:
    def __init__(self):
        self.resources = {}
        self.submissions = []
        self.uploads = []

    def start_context(self, name):
        self.renderer = name

    def create(self, args):
        self.resources[args[0]] = args

    def unref(self, handle):
        del self.resources[handle]

    def submit(self, body):
        self.submissions.append(body)

    def transfer(self, desc, payload, scanout):
        if payload is not None:
            self.uploads.append(payload)
        if scanout:
            return bytes([23]) * desc[-1]


class StreamTests(unittest.TestCase):
    def test_files_window_replays_locally_and_removes_per_row_padding(self):
        args = (17, 2, 1, 138, 3, 2, 1, 1, 0, 0)
        desc = (17, 0, 64, 0, 0, 0, 0, 3, 2, 1, 128)
        start = msg(vr.RENDERER, b'nautilus') + msg(vr.CREATE, vr.CREATE_ARGS.pack(*args))
        read = msg(vr.GET, vr.TRANSFER.pack(*desc))
        class PaddedGpu(Gpu):
            def transfer(self, desc, payload, scanout):
                return bytes(range(128)) if scanout else None
        gpu = PaddedGpu()
        frames = []
        vr._play(Chunked(start + read), gpu, lambda *args: frames.append(args), renderer=b'nautilus')
        self.assertEqual(gpu.renderer, b'nautilus')
        self.assertEqual(frames, [(3, 2, bytes(range(12)) + bytes(range(64, 76)))])
        with self.assertRaises(vr.VirglError):
            vr._play(Chunked(start + read), gpu, lambda *_: None)
        with self.assertRaises(vr.VirglError):
            upload = msg(vr.PUT, vr.TRANSFER.pack(*desc) + bytes(128))
            vr._play(Chunked(start + upload), gpu, lambda *_: None, renderer=b'nautilus')

    def test_fragmented_inline_upload_and_only_full_display_readback(self):
        stream = bytearray(START + msg(vr.VERSION, vr.WORD.pack(0)))
        stream += msg(vr.CREATE, vr.CREATE_ARGS.pack(*DISPLAY))
        # Mesa emits staging buffers with array_size 0. Even an unaligned
        # number of upload bytes is transmitted without trailing padding.
        stream += msg(vr.CREATE, vr.CREATE_ARGS.pack(2, 0, 64, 16, 55, 1, 1, 0, 0, 0))
        stream += msg(vr.PUT, vr.TRANSFER.pack(2, 0, 0, 0, 8, 0, 0, 3, 1, 1, 3) + b'abc')
        stream += msg(vr.SUBMIT, vr.WORD.pack(0))
        stream += msg(vr.GET, vr.TRANSFER.pack(1, 0, 0, 0, 0, 0, 0, 1, 1, 1, 4))
        stream += msg(vr.GET, vr.TRANSFER.pack(*DESC)) + msg(vr.UNREF, vr.WORD.pack(1))
        gpu, outputs = Gpu(), []
        vr._play(Chunked(stream), gpu, lambda w, h, data: outputs.append((w, h, data)))
        self.assertEqual(gpu.uploads, [b'abc'])
        self.assertEqual(gpu.submissions, [vr.WORD.pack(0)])
        self.assertEqual(outputs, [(4, 2, bytes([23]) * 32)])
        self.assertEqual(set(gpu.resources), {2})

    def test_budget_for_desktop_surfaces_keeps_allocation_bounds(self):
        def cost(fmt, width, height, last_level=0):
            return vr._Resource.checked(vr.CREATE_ARGS.pack(2, 2, fmt, 138, width, height, 1, 1, last_level, 0)).budget
        for fmt in (2, 20, 67, 134):
            self.assertEqual(cost(fmt, 1920, 1080), 1920 * 1080 * 8)
            self.assertEqual(cost(fmt, 1, 1), 4096)
            self.assertEqual(cost(fmt, 65, 16), 2 * 512 * 16)
            self.assertGreater(cost(fmt, 128, 128, 1), cost(fmt, 128, 128))
            with self.assertRaises(vr.VirglError):
                cost(fmt, 8192, 8192)
        self.assertEqual(cost(104, 128, 128), 128 * 128 * 16)

    def test_malformed_streams_do_not_present(self):
        make = msg(vr.CREATE, vr.CREATE_ARGS.pack(*DISPLAY))
        cases = [b'', b'x', msg(vr.CAPS), msg(vr.RENDERER, b'other'), START*2,
                 START + msg(vr.VERSION, vr.WORD.pack(1)),
                 START + vr.HEADER.pack(vr.MAX_MESSAGE, vr.SUBMIT),
                 START + vr.HEADER.pack(11, vr.PUT) + vr.TRANSFER.pack(*DESC),
                 START + vr.HEADER.pack(15, vr.CREATE),
                 START + make + make,
                 START + msg(vr.GET, vr.TRANSFER.pack(*DESC)),
                 START + make + msg(vr.GET, vr.TRANSFER.pack(1, 0, 0, 0, 0, 0, 0, 4, 2, 1, vr.MAX_MESSAGE + 1)),
                 START + make + msg(vr.GET, vr.TRANSFER.pack(1, 0, 0, 0, 1, 0, 0, 4, 2, 1, 32)),
                 START + msg(vr.CREATE, vr.CREATE_ARGS.pack(8, 2, 2, 138, 8192, 8192, 1, 1, 0, 0)),
                 START + msg(vr.UNREF, vr.WORD.pack(8)), START + vr.HEADER.pack(0, 99)]
        for index, case in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(vr.VirglError):
                vr._play(Chunked(case), Gpu(), lambda *_: self.fail('unexpected scanout'))

    def test_runtime_requires_exact_official_dependency_set(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(vr.VirglError):
                vr._runtime(path)
            (path / 'other.dll').write_bytes(b'invalid')
            with self.assertRaises(vr.VirglError):
                vr._runtime(path)


if __name__ == '__main__':
    unittest.main()
