"""Carry one paired CLRP connection through InstaBox's private exec and file APIs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from cloud_render.wire import Click, MAX_PAYLOAD, WireError, encode_message, read_message
from instabox import BridgeError, NoRedirect


HELPER = Path(__file__).resolve().parents[1] / 'cloud_render' / 'instabox_relay.py'
INLINE_LIMIT = 64 * 1024


class TransportError(OSError):
    pass


class InstaBoxTransport:
    def __init__(self, box, bridge):
        self.box = box
        self.bridge = bridge
        self.session = secrets.token_hex(16)
        self.helper = HELPER.read_text(encoding='utf-8')
        self.stop = threading.Event()
        self.close_lock = threading.Lock()
        self.started = False

    def _call(self, op, **fields):
        payload = json.dumps({'op': op, 'session': self.session, **fields}, separators=(',', ':'))
        # start, click, close and poll are each idempotent for the exact request.
        for attempt in range(2):
            try:
                raw = self.box.exec(['python3', '-I', '-B', '-c', self.helper], payload, timeout=45 if op == 'start' else 15)
                break
            except BridgeError as exc:
                if attempt or exc.code != 'network':
                    raise TransportError('InstaBox 中转请求失败。') from None
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            raise TransportError('InstaBox 中转响应无效。') from None
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise TransportError('云端绘图中转拒绝了本次请求。')
        return result

    def _open_file(self, msg_id):
        path = f'/tmp/clrp-{self.session}/msg-{msg_id}.bin'
        query = urllib.parse.urlencode({'path': path})
        request = urllib.request.Request(self.box.base + self.box.path + '/files/content?' + query,
                                         headers={'X-API-Key': self.box.key, 'Accept': 'application/octet-stream'})
        # A redirect must never receive the account credential or return a message from another origin.
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=30)

    def _message(self, result, msg_id):
        size = result.get('size')
        digest = result.get('sha256')
        if (type(result.get('id')) is not int or result['id'] != msg_id or type(size) is not int or
                not 12 <= size <= MAX_PAYLOAD + 12 or not isinstance(digest, str) or
                not re.fullmatch(r'[a-f0-9]{64}', digest)):
            raise TransportError('云端绘图消息的编号、长度或摘要无效。')
        if result.get('file') is True and 'data' not in result:
            data = self._read_file(msg_id, size)
        else:
            chunk = result.get('data')
            if ('file' in result or size > INLINE_LIMIT or not isinstance(chunk, str) or
                    len(chunk) != ((size + 2) // 3) * 4):
                raise TransportError('云端绘图数据编码无效。')
            try:
                data = base64.b64decode(chunk, validate=True)
            except (binascii.Error, ValueError):
                raise TransportError('云端绘图数据编码无效。') from None
        if (len(data) != size or hashlib.sha256(data).hexdigest() != digest or data[:5] != b'CLRP\x01' or
                data[5] not in (1, 3) or data[6:8] != b'\0\0' or int.from_bytes(data[8:12], 'big') != size - 12):
            raise TransportError('云端绘图数据校验失败。')
        return data

    def _read_file(self, msg_id, size):
        # Complete and verify one bounded cloud spool file before exposing any
        # byte to the renderer. The remote peer retains the file until next poll.
        for attempt in range(2):
            try:
                with self._open_file(msg_id) as source:
                    data = bytearray()
                    while len(data) < size and not self.stop.is_set():
                        block = source.read(min(65536, size - len(data)))
                        if not block:
                            break
                        data.extend(block)
                    if len(data) == size and not source.read(1):
                        return data
            except (OSError, http.client.HTTPException):
                pass
            if attempt or self.stop.is_set():
                break
        raise TransportError('未能完整读取云端绘图数据。')

    def _upstream(self):
        try:
            with self.bridge.makefile('rb') as stream:
                number = 0
                while not self.stop.is_set():
                    click = read_message(stream, expected=Click)
                    if click is None:
                        break
                    number += 1
                    data = base64.b64encode(encode_message(click)).decode('ascii')
                    ack = self._call('click', id=number, data=data)
                    if type(ack.get('id')) is not int or ack['id'] != number:
                        raise TransportError('云端绘图中转的输入编号不一致。')
        except (OSError, WireError, EOFError):
            pass
        finally:
            self.close()

    def _downstream(self):
        try:
            cursor = 0
            while not self.stop.is_set():
                result = self._call('poll', after=cursor)
                kind = result.get('kind')
                if kind == 'wait':
                    self.stop.wait(0.1)
                elif kind == 'closed':
                    break
                elif kind == 'message':
                    self.bridge.sendall(self._message(result, cursor + 1))
                    cursor += 1
                else:
                    raise TransportError('云端绘图消息类型无效。')
        except (OSError, WireError, EOFError):
            pass
        finally:
            self.close()

    def start(self, port, pairing):
        self.started = True  # A timed-out start may still have allocated its private directory.
        self._call('start', port=port, pair=pairing)
        for loop in (self._upstream, self._downstream):
            threading.Thread(target=loop, daemon=True).start()

    def close(self):
        with self.close_lock:
            if self.stop.is_set():
                return
            self.stop.set()
            try:
                self.bridge.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.bridge.close()
            if self.started:
                try:
                    self._call('close')
                except TransportError:
                    print('无法确认云端中转已立即关闭，服务将按闲置期限自动清理。', file=sys.stderr)


def connect(box, cloud_port, pairing):
    """Open the upstream with the pairing credential. Return an already-paired local socket and owner."""
    if type(cloud_port) is not int or not 1 <= cloud_port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', pairing):
        raise ValueError('云端绘图端口或配对码无效。')
    front, bridge = socket.socketpair()
    front.settimeout(90)
    try:
        transport = InstaBoxTransport(box, bridge)
        try:
            transport.start(cloud_port, pairing)
        except BaseException:
            transport.close()
            raise
    except BaseException:
        bridge.close()
        front.close()
        raise
    return front, transport
