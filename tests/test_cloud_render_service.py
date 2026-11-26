import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_render.service import AUTH_PREFIX, Session, _COMMIT, _FIELD_CLICK, _FIELD_HITS
from cloud_render.wire import (CONTROL, SHIFT, Click, ClickAck, Frame, InputAck, KeyPress,
                               Layer, TextCommit, async_read_message, encode_message)


class CloudRenderServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = SimpleNamespace(is_closed=lambda: False, mouse=SimpleNamespace(click=AsyncMock()))
        self.page.keyboard = SimpleNamespace(press=AsyncMock(), insert_text=AsyncMock(), up=AsyncMock())
        self.original_edit = AsyncMock(return_value=True)
        self.focus_id = 1
        self.can_edit = True
        self.selection = (0, 0)
        self.editor_point = None

        async def evaluate_handle(_expression):
            remembered = self.focus_id, self.selection
            hits = {}
            async def matches(expression, purpose=None):
                if expression == _FIELD_HITS:
                    hits[purpose] = self.editor_point
                    return None
                if expression == _FIELD_CLICK:
                    seq, x, y = purpose
                    return (self.editor_point == hits.get(seq) == (x, y)
                            and (self.focus_id, self.selection) == remembered)
                if expression == _COMMIT:
                    return await self.original_edit(purpose)
                return (self.focus_id, self.selection) == remembered and (purpose != "edit" or self.can_edit)
            return SimpleNamespace(evaluate=matches, dispose=AsyncMock())

        self.page.evaluate_handle = evaluate_handle
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

    async def test_keys_and_text_commit_are_ordered_and_never_replayed(self):
        reader, writer = await self.connect()
        current = await async_read_message(reader, Frame)
        keys = [("Tab", SHIFT, "Shift+Tab"), ("z", SHIFT | CONTROL, "Control+Shift+KeyZ"),
                ("Enter", 0, "Enter")]
        for name, mask, dispatch in keys:
            event = encode_message(KeyPress(current.sequence, name, mask))
            writer.write(event)
            self.assertEqual(await async_read_message(reader), InputAck(current.sequence, True))
            updated = await async_read_message(reader, Frame)
            self.assertEqual(current.layers, updated.layers)
            self.assertGreater(updated.sequence, current.sequence)
            self.page.keyboard.press.assert_awaited_with(dispatch)
            writer.write(event)
            self.assertEqual(await async_read_message(reader), InputAck(current.sequence, False))
            current = await async_read_message(reader, Frame)

        text = "云端✓👩🏽‍💻\n\tControl+z"
        body = encode_message(TextCommit(current.sequence, text))
        writer.write(body + body)
        self.assertEqual(await async_read_message(reader), InputAck(current.sequence, True))
        once = await async_read_message(reader, Frame)
        self.assertEqual(await async_read_message(reader), InputAck(current.sequence, False))
        twice = await async_read_message(reader, Frame)
        self.assertGreater(twice.sequence, once.sequence)
        self.assertEqual(self.page.keyboard.press.await_count, len(keys))
        self.original_edit.assert_awaited_once_with(text)
        self.page.mouse.click.assert_not_awaited()

    async def test_text_rejects_changed_focus_and_noneditable_target(self):
        reader, writer = await self.connect()
        first = await async_read_message(reader, Frame)
        self.focus_id = 2
        writer.write(encode_message(TextCommit(first.sequence, "wrong field")))
        self.assertEqual(await async_read_message(reader), InputAck(first.sequence, False))
        second = await async_read_message(reader, Frame)
        self.can_edit = False
        writer.write(encode_message(TextCommit(second.sequence, "button")))
        self.assertEqual(await async_read_message(reader), InputAck(second.sequence, False))
        await async_read_message(reader, Frame)
        self.original_edit.assert_not_awaited()

    async def test_composition_can_span_visual_revisions_but_is_consumed_once(self):
        reader, writer = await self.connect()
        first = await async_read_message(reader, Frame)
        self.picture = b'skiapict-caret'
        await async_read_message(reader, Frame)
        self.picture = b'skiapict-animation'
        animated = await async_read_message(reader, Frame)
        self.assertGreater(animated.sequence, first.sequence + 1)
        body = encode_message(TextCommit(first.sequence, "晴"))
        writer.write(body)
        self.assertEqual(await async_read_message(reader), InputAck(first.sequence, True))
        after = await async_read_message(reader, Frame)
        self.assertGreater(after.sequence, animated.sequence)
        writer.write(encode_message(TextCommit(animated.sequence, "duplicate")))
        self.assertEqual(await async_read_message(reader), InputAck(animated.sequence, False))
        self.original_edit.assert_awaited_once_with("晴")

    async def test_selection_moved_before_commit_and_reconnected_viewer_rejects_old_sequence(self):
        reader, writer = await self.connect()
        first = await async_read_message(reader, Frame)
        self.selection = (0, 4)
        writer.write(encode_message(TextCommit(first.sequence, "wrong selection")))
        self.assertEqual(await async_read_message(reader), InputAck(first.sequence, False))
        await async_read_message(reader, Frame)
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if not self.session.busy:
                break
            await asyncio.sleep(.01)
        reader, new_writer = await self.connect()
        await async_read_message(reader, Frame)
        new_writer.write(encode_message(TextCommit(first.sequence, "wrong connection")))
        self.assertEqual(await async_read_message(reader), InputAck(first.sequence, False))
        self.original_edit.assert_not_awaited()

    async def test_click_on_animated_editor_is_bound_to_original_hit_and_current_target(self):
        self.editor_point = (5, 5)
        reader, writer = await self.connect()
        original = await async_read_message(reader, Frame)
        self.picture = b'skiapict-animation'
        await async_read_message(reader, Frame)
        writer.write(encode_message(Click(original.sequence, 5, 5)))
        self.assertEqual(await async_read_message(reader), InputAck(original.sequence, True))
        first = await async_read_message(reader, Frame)
        self.picture = b'skiapict-another-animation'
        self.editor_point = None  # a newly opened overlay intercepts the point
        await async_read_message(reader, Frame)
        writer.write(encode_message(Click(first.sequence, 5, 5)))
        self.assertEqual(await async_read_message(reader), InputAck(first.sequence, False))
        self.page.mouse.click.assert_awaited_once_with(5, 5)
