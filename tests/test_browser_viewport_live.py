import asyncio
import threading
import unittest
from pathlib import Path

from cloud_render import browser_viewport_service as service
from cloud_render.browser_surface import SystemBrowserWindow
from cloud_render.chromium_capture import CaptureError
from cloud_render.wire import Click, Frame, InputAck, KeyPress, Layer, async_read_message, encode_message
from experiments.live_browser_viewport import present_viewport


def frame(sequence, body=b'A'):
    return Frame(sequence, 8, 6, (Layer(0, 0, 8, 6, b'skiapict' + body),))


class CloudWindow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Page:
            def is_closed(self):
                return False
        self.owner = SystemBrowserWindow(45, 14, 'one', 'loader1')
        self.body = b'A'
        self.source = service.OfflineViewport(Page(), 't' * 43, browser_pid=45, executable=Path('/usr/bin/chromium'))

        async def capture(page, seq, *, browser_pid, executable):
            return self.owner, frame(seq, self.body)
        self.source.capture = capture
        self.source.poll_seconds = .03
        self.socket = await self.enterAsyncContext(service._listen(self.source, 0))

    async def connect(self, token):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.socket.sockets[0].getsockname()[1])
        self.addCleanup(writer.close)
        writer.write(b'CLRP-AUTH/1 ' + token + b'\n')
        await writer.drain()
        return reader, writer

    async def test_external_page_is_read_only_and_publishes_only_new_drawings(self):
        denied, _ = await self.connect(b'z' * 43)
        self.assertEqual(await denied.read(1), b'')
        reader, writer = await self.connect(b't' * 43)
        first = await async_read_message(reader, expected=Frame)
        self.assertEqual(first.layers, frame(1).layers)
        writer.write(encode_message(Click(first.sequence, 1, 2)))
        self.assertEqual(await async_read_message(reader, expected=InputAck), InputAck(1, False))
        self.assertEqual((await async_read_message(reader, expected=Frame)).sequence, 2)
        self.body = b'B'
        updated = await asyncio.wait_for(async_read_message(reader, expected=Frame), 2)
        self.assertEqual(updated, frame(3, b'B'))
        self.owner = SystemBrowserWindow(45, 14, 'one', 'changed-loader')
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        with self.assertRaisesRegex(CaptureError, 'CaptureError'):
            await self.source.wait_for_viewer()

    async def test_normal_paired_eof_and_unpaired_rejection_have_distinct_ownership(self):
        denied, _ = await self.connect(b'z' * 43)
        self.assertEqual(await denied.read(1), b'')
        self.assertFalse(self.source.viewer_closed.is_set())
        reader, writer = await self.connect(b't' * 43)
        await async_read_message(reader, expected=Frame)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(self.source.wait_for_viewer(), 2)
        self.assertIsNone(self.source.failure_kind)

    async def test_actions_require_the_painted_scene_even_with_same_document(self):
        applied = []

        class Navigation:
            async def apply(_, window, action):
                applied.append((window, action))
                return True

        self.source.navigation = Navigation()
        reader, writer = await self.connect(b't' * 43)
        await async_read_message(reader, expected=Frame)
        # The source is already different when this input arrives.
        self.body = b'B'
        writer.write(encode_message(KeyPress(1, 'End')))
        self.assertFalse((await async_read_message(reader, expected=InputAck)).applied)
        fresh = await async_read_message(reader, expected=Frame)
        self.assertFalse(applied)
        writer.write(encode_message(KeyPress(fresh.sequence, 'End')))
        self.assertTrue((await async_read_message(reader, expected=InputAck)).applied)
        await async_read_message(reader, expected=Frame)
        self.assertEqual(applied, [(self.owner, KeyPress(fresh.sequence, 'End'))])


class NativePresentation(unittest.TestCase):
    def test_only_a_new_press_and_release_after_actual_paint_can_send_a_click(self):
        actions = []
        sent = threading.Event()

        class Session:
            changed = threading.Condition()
            disconnected = False
            frame = frame(1)

            def send_input(self, event, *, wait_for_scene):
                actions.append(event)
                sent.set()

            def close(self):
                self.disconnected = True

        class Window:
            width, height = 8, 6
            turns = 0

            def __init__(self, title, w, h, receive):
                self.receive = receive

            def present(self, pixels, on_painted):
                self.on_painted = on_painted
                self.receive(('press', 3, 4))
                return True

            def pump(self):
                self.turns += 1
                if self.turns == 1:
                    self.on_painted()
                    self.receive(('click', 3, 4, 'left'))
                    self.receive(('press', 3, 4))
                    self.receive(('pointer-cancel',))
                    self.receive(('click', 3, 4, 'left'))
                    self.receive(('key', 'Enter'))
                    self.receive(('press', 2, 4))
                    self.receive(('click', 3, 4, 'left'))
                    self.receive(('press', 3, 4))
                    self.receive(('click', 3, 4, 'left'))
                return not sent.is_set() and self.turns < 50

            def close(self):
                pass

        present_viewport(Session(), Window, lambda picture: bytes(picture.width * picture.height * 4))
        self.assertEqual(actions, [Click(1, 3, 4)])
