import base64
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import patch

from cloud_render.wire import Click, ClickAck, Frame, Layer, encode_message, read_message, write_message
from instabox import BridgeError
from thin_client import instabox_transport as relay


class LocalExec:
    """Run the real remote helper under the test OS, simulating one lost HTTPS result."""
    def __init__(self):
        self.lost_click = False
        self.click_ids = []

    def exec(self, argv, payload, timeout):
        req = json.loads(payload)
        r = subprocess.run(argv, input=payload, text=True, capture_output=True, timeout=timeout, check=True)
        if req['op'] == 'click':
            self.click_ids.append(req['id'])
            if not self.lost_click:
                self.lost_click = True
                raise BridgeError('network', 'fixture')
        return r.stdout


def message(seq, n):
    return Frame(seq, 320, 240, (Layer(0, 0, 320, 240, b'skiapict' + b'a' * n),))


class PrivateRelayTests(unittest.TestCase):
    def test_large_file_and_async_frame_and_idempotent_input_share_one_connection(self):
        with socket.socket() as listener:
            listener.settimeout(5)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            pair = 's' * 43
            box = LocalExec()
            frames = (message(1, 1300000), message(2, 400), message(3, 1400000))
            applied = []
            remote = []
            files = []
            def server():
                with listener.accept()[0] as peer, peer.makefile('rwb') as stream:
                    remote.append(stream.readline(65))
                    write_message(stream, frames[0])
                    write_message(stream, frames[1])
                    event = read_message(stream, expected=Click)
                    applied.append(event)
                    write_message(stream, ClickAck(event.sequence, True))
                    write_message(stream, frames[2])
                    applied.append(read_message(stream, expected=Click))  # no duplicate after retry
            def read_private_file(owner, n):
                path = Path('/tmp') / ('clrp-' + owner.session) / f'msg-{n}.bin'
                files.append(path)
                return path.open('rb')
            worker = threading.Thread(target=server, daemon=True)
            worker.start()
            with patch.object(relay.InstaBoxTransport, '_open_file', read_private_file):
                front, owner = relay.connect(box, listener.getsockname()[1], pair)
                try:
                    front.settimeout(12)
                    with front.makefile('rwb') as stream:
                        self.assertEqual(read_message(stream, expected=Frame), frames[0])
                        self.assertEqual(read_message(stream, expected=Frame), frames[1])
                        write_message(stream, Click(2, 31.5, 91.75))
                        self.assertEqual(read_message(stream, expected=ClickAck), ClickAck(2, True))
                        self.assertEqual(read_message(stream, expected=Frame), frames[2])
                finally:
                    front.close()
                    owner.close()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(applied, [Click(2, 31.5, 91.75), None])
            self.assertEqual(remote, [b'CLRP-AUTH/1 ' + pair.encode() + b'\n'])
            self.assertEqual(box.click_ids, [1, 1])
            self.assertEqual([f.name for f in files], ['msg-1.bin', 'msg-4.bin'])
            self.assertFalse(files[0].parent.exists())

    def test_invalid_digest_length_and_direction_never_feed_renderer(self):
        local, bridge = socket.socketpair()
        try:
            owner = relay.InstaBoxTransport(LocalExec(), bridge)
            raw = encode_message(ClickAck(1, True))
            base = {'id': 1, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'data': base64.b64encode(raw).decode()}
            self.assertEqual(owner._message(base, 1), raw)
            for bad in [{'sha256': '0' * 64}, {'size': 2 ** 31}, {'id': True}, {'file': True}, {'data': 'no!'}]:
                with self.subTest(bad=bad), self.assertRaises(relay.TransportError):
                    owner._message({**base, **bad}, 1)
            with patch.object(owner, '_open_file', side_effect=lambda n: io.BytesIO(raw + b'trailing')):
                with self.assertRaises(relay.TransportError):
                    owner._message({k: v for k, v in {**base, 'file': True}.items() if k != 'data'}, 1)
        finally:
            local.close()
            bridge.close()


if __name__ == '__main__':
    unittest.main()
