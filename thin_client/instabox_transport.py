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

from cloud_render.wire import Click, INPUT_TYPES, MAX_PAYLOAD, WireError, encode_message, read_message
from cloud_render.frame_delta import DeltaError, decode_frame
from instabox import BridgeError, open_provider_request


HELPER = Path(__file__).resolve().parents[1] / 'cloud_render' / 'instabox_relay.py'
INLINE_LIMIT = 64 * 1024


class TransportError(OSError):
    pass


class InvalidPayload(TransportError, ValueError):
    """The remote response failed validation, even if the transport stayed open."""


class RequestOutcomeUnknown(TransportError):
    """The provider may have accepted an input whose HTTP responses were lost."""


class InstaBoxControl:
    """One authenticated provider helper and its session-private transfer files."""

    def __init__(self, box):
        self.box = box
        self.session = secrets.token_hex(16)
        # Isolated provider exec cannot import our package. Supply only these
        # fixed product sources, identically on every idempotent control call.
        codec = HELPER.with_name('frame_delta.py').read_text(encoding='utf-8')
        self.helper = codec + '\n' + HELPER.read_text(encoding='utf-8')
        self.stop = threading.Event()
        self.close_lock = threading.Lock()
        self.started = False

    def _call(self, op, **fields):
        payload = json.dumps({'op': op, 'session': self.session, **fields}, separators=(',', ':'))
        # Input retries reuse the same session, input number and exact envelope.
        for attempt in range(2):
            try:
                raw = self.box.exec(['python3', '-I', '-B', '-c', self.helper], payload, timeout=45 if op == 'start' else 15)
                break
            except BridgeError as exc:
                if attempt or exc.code != 'network' or (self.stop.is_set() and op != 'close'):
                    if op == 'desktop' and exc.code == 'network' and not self.stop.is_set():
                        raise RequestOutcomeUnknown('未收到此次操作的中转响应。') from None
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
        return open_provider_request(request, 30)

    def verified_bytes(self, result, msg_id, max_size=MAX_PAYLOAD + 12):
        size = result.get('size')
        digest = result.get('sha256')
        if (type(result.get('id')) is not int or result['id'] != msg_id or type(size) is not int or
                not 12 <= size <= max_size or not isinstance(digest, str) or
                not re.fullmatch(r'[a-f0-9]{64}', digest)):
            raise InvalidPayload('云端绘图消息的编号、长度或摘要无效。')
        if result.get('file') is True and 'data' not in result:
            data = self._read_file(msg_id, size)
        else:
            chunk = result.get('data')
            if ('file' in result or size > INLINE_LIMIT or not isinstance(chunk, str) or
                    len(chunk) != ((size + 2) // 3) * 4):
                raise InvalidPayload('云端绘图数据编码无效。')
            try:
                data = base64.b64decode(chunk, validate=True)
            except (binascii.Error, ValueError):
                raise InvalidPayload('云端绘图数据编码无效。') from None
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise InvalidPayload('云端绘图传输内容校验失败。')
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


class InstaBoxTransport(InstaBoxControl):
    def __init__(self, box, bridge):
        super().__init__(box)
        self.bridge = bridge

    def _message(self, result, msg_id, previous=b''):
        data = self.verified_bytes(result, msg_id)
        size = len(data)
        if 'codec' in result:
            if (result['codec'] != 'cdz1' or type(result.get('frameSize')) is not int or
                    not 12 <= result['frameSize'] <= MAX_PAYLOAD + 12 or
                    not isinstance(result.get('frameSha256'), str) or
                    not re.fullmatch(r'[a-f0-9]{64}', result['frameSha256'])):
                raise TransportError('云端绘图复用协议无效。')
            try:
                data = decode_frame(data, previous)
            except DeltaError as exc:
                raise TransportError('未能从本次会话重建完整绘图。') from exc
            size = result['frameSize']
            if (len(data) != size or data[5:6] != b'\x01' or
                    hashlib.sha256(data).hexdigest() != result['frameSha256']):
                raise TransportError('云端重建后的完整绘图校验失败。')
        if (data[:5] != b'CLRP\x01' or
                data[5] not in (1, 3) or data[6:8] != b'\0\0' or int.from_bytes(data[8:12], 'big') != size - 12):
            raise TransportError('云端绘图数据校验失败。')
        return data

    def _upstream(self):
        try:
            with self.bridge.makefile('rb') as stream:
                number = 0
                while not self.stop.is_set():
                    event = read_message(stream, expected=INPUT_TYPES)
                    if event is None:
                        break
                    number += 1
                    data = base64.b64encode(encode_message(event)).decode('ascii')
                    ack = self._call('click' if isinstance(event, Click) else 'input', id=number, data=data)
                    del event, data
                    if type(ack.get('id')) is not int or ack['id'] != number:
                        raise TransportError('云端绘图中转的输入编号不一致。')
        except (OSError, WireError, EOFError):
            pass
        finally:
            self.close()

    def _downstream(self):
        try:
            cursor = 0
            previous = b''
            while not self.stop.is_set():
                result = self._call('poll', after=cursor)
                kind = result.get('kind')
                if kind == 'wait':
                    self.stop.wait(0.1)
                elif kind == 'closed':
                    break
                elif kind == 'message':
                    try:
                        data = self._message(result, cursor + 1, previous)
                    except TransportError:
                        if result.get('codec') != 'cdz1' or self.stop.is_set():
                            raise
                        # Same unacknowledged frame, with no dictionary. Never
                        # reissue an application action or accept a changed target.
                        reset = self._call('poll', after=cursor, reset=True)
                        data = self._message(reset, cursor + 1)
                        if (reset.get('kind') != 'message' or len(data) != result.get('frameSize') or
                                hashlib.sha256(data).hexdigest() != result.get('frameSha256')):
                            raise TransportError('云端恢复的绘图与原消息不符。')
                    self.bridge.sendall(data)
                    if data[5] == 1:
                        previous = data
                    cursor += 1
                else:
                    raise TransportError('云端绘图消息类型无效。')
        except (OSError, WireError, EOFError):
            pass
        finally:
            self.close()

    def start(self, port, pairing):
        self.started = True  # A timed-out start may still have allocated its private directory.
        self._call('start', port=port, pair=pairing, codec='cdz1')
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
