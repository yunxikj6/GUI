import base64
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import patch

from cloud_render.wire import Frame, Layer, read_message, write_message
from instabox import BridgeError
from thin_client import instabox_transport as relay


class DropOrCorrupt:
    """Drop a completed poll and reset receipt, and corrupt a warm delta in transit."""
    def __init__(self, corrupt_reset=False):
        self.requests = []
        self.dropped = set()
        self.corrupt_reset = corrupt_reset
        self.observed = []

    def exec(self, argv, payload, timeout):
        req = json.loads(payload)
        result = subprocess.run(argv, input=payload, capture_output=True, text=True, check=True, timeout=timeout)
        self.requests.append((req['op'], req.get('after'), req.get('reset', False)))
        reply = json.loads(result.stdout)
        if req['op'] == 'poll' and req['after'] == 1 and reply.get('kind') == 'message':
            reset = req.get('reset', False)
            self.observed.append((reset, reply['id'], reply['size'], reply['sha256']))
            if reset not in self.dropped:
                self.dropped.add(reset)
                raise BridgeError('network', 'fixture')
            if not reset:
                assert reply['codec'] == 'cdz1' and 'data' in reply
                data = bytearray(base64.b64decode(reply['data']))
                data[-1] ^= 0x7f
                reply['data'] = base64.b64encode(data).decode('ascii')
            elif self.corrupt_reset:
                reply['sha256'] = '0' * 64
        return json.dumps(reply)


class ReuseRecovery(unittest.TestCase):
    def exercise(self, corrupt_reset):
        asset = hashlib.shake_256(b'font data').digest(250000)
        frames = [Frame(seq, 320, 240, (Layer(0, 0, 320, 240, b'skiapict' + bytes([seq]) + asset + asset),))
                  for seq in (1, 2)]
        with socket.create_server(('127.0.0.1', 0)) as listener:
            box = DropOrCorrupt(corrupt_reset)
            def app():
                with listener.accept()[0] as peer, peer.makefile('rwb') as pipe:
                    pipe.readline()
                    for frame in frames:
                        write_message(pipe, frame)
                    peer.shutdown(socket.SHUT_WR)
                    pipe.read(1)
            task = threading.Thread(target=app, daemon=True)
            task.start()
            def read_file(owner, n):
                return (Path('/tmp') / ('clrp-' + owner.session) / f'msg-{n}.bin').open('rb')
            with patch.object(relay.InstaBoxTransport, '_open_file', read_file):
                front, owner = relay.connect(box, listener.getsockname()[1], 'x' * 43)
                try:
                    front.settimeout(10)
                    with front.makefile('rb') as pipe:
                        self.assertEqual(read_message(pipe), frames[0])
                        self.assertEqual(read_message(pipe), None if corrupt_reset else frames[1])
                        if not corrupt_reset:
                            # Let the final poll retire frame two before closing the local connection.
                            self.assertIsNone(read_message(pipe))
                finally:
                    front.close()
                    owner.close()
            task.join(2)
            self.assertFalse(task.is_alive())
            self.assertFalse((Path('/tmp') / ('clrp-' + owner.session)).exists())
            self.assertEqual(box.dropped, {True, False})
            self.assertEqual(set(t[1] for t in box.observed), {2})
            self.assertEqual(len(set(t[3] for t in box.observed if not t[0])), 1)
            self.assertEqual(len(set(t[3] for t in box.observed if t[0])), 1)
            ordinary, reset = (next(t for t in box.observed if t[0] is state) for state in (False, True))
            self.assertLess(ordinary[2], reset[2] // 4)
            self.assertEqual(any(r[1] == 2 for r in box.requests), not corrupt_reset)
            self.assertFalse(any(r[0] in ('click', 'input') for r in box.requests))

    def test_remote_retry_and_missing_frame_reset_are_atomic(self):
        self.exercise(False)

    def test_corrupt_recovery_never_delivers_partial_frame_or_acks_it(self):
        self.exercise(True)
