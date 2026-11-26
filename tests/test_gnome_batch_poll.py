import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cloud_render import gnome_wire as wire
from cloud_render import instabox_relay as helper
from instabox import BridgeError
from thin_client.gnome_transport import _Provider
from thin_client.instabox_transport import TransportError


def inline(index, raw):
    return {'kind': 'message', 'id': index, 'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'data': base64.b64encode(raw).decode('ascii')}


class BatchRelay(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_and_partly_acknowledged_offers_keep_needed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = helper.Relay(root, {'protocol': 'gnome'})
            rows = [inline(1, wire.packet(wire.READY, wire.DIMENSIONS.pack(800, 520)))]
            for index in range(2, 5):
                raw = bytes([index]) * (helper.INLINE + 1)
                helper.message_file(root, index).write_bytes(raw)
                rows.append({'kind': 'message', 'id': index, 'size': len(raw),
                             'file': True, 'sha256': hashlib.sha256(raw).hexdigest()})
            relay.messages.extend(rows)
            relay.queued_bytes = sum(x['size'] for x in rows)
            relay.closed = True
            offer = await relay.poll(0, batch=True)
            self.assertEqual(offer, await relay.poll(0, batch=True))
            self.assertEqual(offer['messages'], rows)
            self.assertEqual((await relay.poll(1, batch=True))['messages'], rows[1:])
            self.assertTrue(helper.message_file(root, 2).exists())
            self.assertEqual((await relay.poll(2, batch=True))['messages'], rows[2:])
            self.assertFalse(helper.message_file(root, 2).exists())
            self.assertTrue(helper.message_file(root, 3).exists())
            with self.assertRaises(helper.RelayError):
                await relay.poll(1, batch=True)
            self.assertEqual(await relay.poll(4, batch=True), {'kind': 'closed'})
            self.assertEqual(relay.queued_bytes, 0)
            self.assertFalse(list(root.iterdir()))

    async def test_reply_count_bytes_and_encoded_size_stay_bounded(self):
        ready = wire.packet(wire.READY, wire.DIMENSIONS.pack(800, 520))
        for large_size in (0, helper.INLINE, 1024 * 1024 + 100):
            relay = helper.Relay(Path('/unused'), {'protocol': 'gnome'})
            for index in range(1, 66):
                if large_size > helper.INLINE:
                    row = {'id': index, 'size': large_size, 'kind': 'message', 'file': True}
                else:
                    row = inline(index, bytes(large_size) if large_size else ready)
                relay.messages.append(row)
            old = await relay.poll(0)
            self.assertEqual(old['id'], 1)
            self.assertEqual(old['kind'], 'message')
            reply = await relay.poll(0, batch=True)
            self.assertLess(len(json.dumps({'ok': True, **reply}, separators=(',', ':'))),
                            helper.REPLY_LIMIT)
            n = len(reply['messages'])
            self.assertLessEqual(n, helper.BATCH_COUNT)
            self.assertLessEqual(sum(row['size'] for row in reply['messages']), helper.BATCH_BYTES)
            self.assertEqual(n, 1 if large_size == helper.INLINE else
                             (helper.BATCH_BYTES // large_size if large_size else helper.BATCH_COUNT))

    async def test_batch_only_for_a_negotiated_gnome_link(self):
        with self.assertRaises(helper.RelayError):
            await helper.Relay(Path('/unused'), {}).poll(0, batch=True)
        base = dict(op='poll', after=0, session='a' * 32)
        for extra in ({'batch': 1}, {'reset': True, 'batch': True}):
            with self.assertRaises(helper.RelayError):
                helper.check_request({**base, **extra})


class BatchClient(unittest.TestCase):
    def test_lost_poll_retries_same_cursor_and_preserves_order(self):
        ready = wire.packet(wire.READY, wire.DIMENSIONS.pack(800, 520))
        notice = wire.packet(wire.UNAVAILABLE, b'\x01')

        class Provider:
            def __init__(self, batch):
                self.batch = batch
                self.polls = []

            def exec(self, args, payload, timeout):
                req = json.loads(payload)
                if req['op'] == 'poll':
                    self.polls.append((req['after'], req.get('batch')))
                    if len(self.polls) == 1:
                        raise BridgeError('network', 'lost reply')
                    if req['after'] == 2:
                        result = {'kind': 'closed'}
                    elif self.batch:
                        result = {'kind': 'batch', 'messages': [inline(1, ready), inline(2, notice)]}
                    else:
                        index = req['after'] + 1
                        result = inline(index, (ready, notice)[index - 1])
                else:
                    result = {'batch': True} if self.batch else {}
                return json.dumps({'ok': True, **result})

        for batch in (True, False):
            box = Provider(batch)
            peer = _Provider(box, 17325, 't' * 43, (800, 520))
            try:
                self.assertEqual(peer.receive(), (wire.READY, ready[wire.HEADER.size:]))
                self.assertEqual(peer.receive(), (wire.UNAVAILABLE, b'\x01'))
                self.assertIsNone(peer.receive())
                self.assertEqual(box.polls, [(0, True), (0, True), (2, True)] if batch
                                 else [(0, None), (0, None), (1, None), (2, None)])
            finally:
                peer.close()

    def test_reject_out_of_order_or_corrupt_data_before_its_cursor_advances(self):
        message = inline(1, wire.packet(wire.UNAVAILABLE, b'\x01'))

        class Provider:
            def __init__(self, rows):
                self.rows = rows

            def exec(self, args, payload, timeout):
                if json.loads(payload)['op'] == 'poll':
                    return json.dumps({'ok': True, 'kind': 'batch', 'messages': self.rows})
                return '{"ok":true,"batch":true}'

        for rows in ([{**message, 'id': 2}], [message, {**message, 'id': 3}],
                     [{**message, 'sha256': '2' * 64}], [message] * 17):
            peer = _Provider(Provider(rows), 17325, 't' * 43, (800, 520))
            try:
                with self.assertRaises(TransportError):
                    peer.receive()
                self.assertEqual(peer.cursor, 0)
            finally:
                peer.close()
