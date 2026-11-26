import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_render.service import AUTH_PREFIX, Session
from cloud_render.wire import Click, ClickAck, Frame, Layer, async_read_message, encode_message


class CloudRenderServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = SimpleNamespace(is_closed=lambda: False, mouse=SimpleNamespace(click=AsyncMock()))
        self.picture = b'skiapict-a'

        async def capture(page, sequence):
            self.assertIs(page, self.page)
            return Frame(sequence, 40, 30, [Layer(0, 0, 40, 30, self.picture)])

        self.session = Session(self.page, capture, 'a' * 43, poll_seconds=0.1)
        self.server = await asyncio.start_server(self.session.accept, '127.0.0.1', 0)
        self.writers = []

    async def asyncTearDown(self):
        self.server.close()
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        await self.session.close()
        await self.server.wait_closed()

    async def connect(self, token=b'a' * 43):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
        self.writers.append(writer)
        writer.write(AUTH_PREFIX + token + b'\n')
        return reader, writer

    async def test_pairing_and_exclusive_input(self):
        intruder, _ = await self.connect(b'b' * 43)
        self.assertEqual(await intruder.read(), b'')
        main, _ = await self.connect()
        self.assertIsInstance(await async_read_message(main), Frame)
        second, _ = await self.connect()
        self.assertEqual(await second.read(), b'')
        self.page.mouse.click.assert_not_awaited()

    async def test_click_ack_then_frame_and_old_click_is_never_applied_twice(self):
        reader, writer = await self.connect()
        frame = await async_read_message(reader, Frame)
        click = encode_message(Click(frame.sequence, 5, 6))
        writer.write(click)
        self.assertEqual(await async_read_message(reader), ClickAck(frame.sequence, True))
        confirmed = await async_read_message(reader, Frame)
        self.assertGreater(confirmed.sequence, frame.sequence)
        writer.write(click)
        self.assertEqual(await async_read_message(reader), ClickAck(frame.sequence, False))
        self.assertGreater((await async_read_message(reader, Frame)).sequence, confirmed.sequence)
        self.page.mouse.click.assert_awaited_once_with(5, 6)

    async def test_unreported_cloud_change_does_not_receive_click_from_old_view(self):
        reader, writer = await self.connect()
        frame = await async_read_message(reader, Frame)
        self.picture = b'skiapict-changed'
        writer.write(encode_message(Click(frame.sequence, 2, 5)))
        self.assertEqual(await async_read_message(reader), ClickAck(frame.sequence, False))
        self.assertEqual((await async_read_message(reader, Frame)).layers[0].skp, self.picture)
        self.page.mouse.click.assert_not_awaited()

    async def test_idle_cloud_updates_do_not_require_user_input(self):
        reader, _ = await self.connect()
        a = await async_read_message(reader, Frame)
        self.picture = b'skiapict-b'
        b = await asyncio.wait_for(async_read_message(reader, Frame), 2)
        self.assertEqual(b.sequence, a.sequence + 1)
        self.assertEqual(b.layers[0].skp, self.picture)
