import io
import socket
import struct
import threading
import unittest

from experiments import gnome_command_stream as stream


def packet(kind, payload):
    return struct.pack('<II', len(payload) // 4, kind) + payload


class GnomeCommands(unittest.TestCase):
    def test_files_display_fence_precedes_pixels_only_on_the_source_return_path(self):
        name = b'nautilus\0'
        upload_target = packet(stream.CREATE, struct.pack('<10I', 17, 2, 1, 138, 2, 2, 1, 1, 0, 0))
        descriptor = struct.pack('<11I', 17, 0, 8, 0, 0, 0, 0, 2, 2, 1, 16)
        get = packet(stream.GET, descriptor)
        wait = packet(stream.WAIT, struct.pack('<II', 17, 1))
        startup = struct.pack('<II', len(name), stream.RENDERER) + name + upload_target
        pixels = bytes(range(16))
        fence = packet(stream.WAIT, struct.pack('<I', 0))
        local, gpu = socket.socketpair()
        with local, gpu:
            gpu.sendall(pixels + fence)
            recording, returned = io.BytesIO(), io.BytesIO()
            stream.copy_commands(io.BytesIO(startup + get + wait), local, recording=recording,
                                 respond=returned.write, renderer=b'nautilus')
            self.assertEqual(recording.getvalue(), startup + get + wait)
            self.assertEqual(returned.getvalue(), fence + pixels)
        for bad in (b'', packet(stream.WAIT, struct.pack('<II', 18, 1))):
            with self.subTest(fence=bad), self.assertRaises(stream.StreamError):
                local, gpu = socket.socketpair()
                with local, gpu:
                    gpu.sendall(pixels)
                    stream.copy_commands(io.BytesIO(startup + get + bad), local,
                                         respond=lambda _: self.fail('unfenced data'), renderer=b'nautilus')
        local, gpu = socket.socketpair()
        with local, gpu, self.assertRaises(stream.StreamError):
            stream.copy_commands(io.BytesIO(startup + packet(stream.PUT, descriptor + pixels)), local,
                                 recording=io.BytesIO(), renderer=b'nautilus')

    def test_input_recording_excludes_pixels_returned_by_gpu(self):
        source_name = b'gnome-shell\0'
        startup = struct.pack('<II', len(source_name), stream.RENDERER) + source_name
        target = packet(stream.CREATE, struct.pack('<10I', 17, 2, 2, 138, 2, 2, 1, 1, 0, 0))
        descriptor = struct.pack('<11I', 17, 0, 8, 0, 0, 0, 0, 2, 2, 1, 16)
        query = packet(stream.GET, descriptor)
        commands = startup + target + query
        received = []
        pixels = b'scanout response'
        self.assertEqual(len(pixels), 16)
        local, gpu = socket.socketpair()
        with local, gpu:
            def render():
                with gpu.makefile('rb') as requests:
                    received.append(requests.read(len(commands)))
                    gpu.sendall(pixels)
            worker = threading.Thread(target=render, daemon=True)
            worker.start()
            recording, replies, scanouts = io.BytesIO(), [], []
            stream.copy_commands(io.BytesIO(commands), local, recording=recording,
                                 respond=replies.append, scanout=lambda desc, data: scanouts.append(data))
            worker.join(2)
        self.assertEqual(received, [commands])
        self.assertEqual(recording.getvalue(), commands)
        self.assertEqual(replies, [pixels])
        self.assertEqual(scanouts, replies)

    def test_unaligned_texture_upload_preserves_exact_following_command(self):
        upload = b'abcde'
        desc = struct.pack('<11I', 1, 0, 5, 0, 0, 0, 0, 5, 1, 1, len(upload))
        raw = struct.pack('<II', 13, stream.PUT) + desc + upload + packet(stream.VERSION, struct.pack('<I', 2))
        data = io.BytesIO(raw)
        self.assertEqual(stream._request(data)[2], desc + upload)
        self.assertEqual(stream._request(data)[2], struct.pack('<I', 0))
        self.assertIsNone(stream._request(data))

    def test_rejects_oversize_and_non_inline_inputs(self):
        for kind, length in [(stream.GET, 12), (stream.SUBMIT, 1 << 27), (12, 0)]:
            with self.subTest(kind=kind), self.assertRaises(stream.StreamError):
                stream._request(io.BytesIO(struct.pack('<II', length, kind)))
