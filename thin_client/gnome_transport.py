"""Read an authenticated original desktop command stream and control its dedicated source."""
import base64
from collections import deque
from contextlib import suppress
import hashlib
import io
import re
import socket
import threading

from cloud_render import gnome_wire as wire
from cloud_render.gnome_input import encode_key, encode_pointer
from .instabox_transport import InstaBoxControl, TransportError


DOWNSTREAM = {wire.BLOCK, wire.ACK, wire.READY, wire.END, wire.UNAVAILABLE}


class _Provider(InstaBoxControl):
    def __init__(self, box, port, pairing):
        super().__init__(box)
        self.cursor = 0
        try:
            self._call('start', port=port, pair=pairing, protocol='gnome')
        except BaseException:
            self.close()
            raise

    def receive(self):
        while not self.stop.is_set():
            result = self._call('poll', after=self.cursor)
            kind = result.get('kind')
            if kind == 'wait':
                continue
            if kind == 'closed':
                return None
            if kind != 'message' or 'codec' in result:
                raise TransportError('云桌面中转消息类型无效。')
            raw = self.verified_bytes(result, self.cursor + 1, wire.MAX_PACKET + wire.HEADER.size)
            data = io.BytesIO(raw)
            message = wire.read(data, DOWNSTREAM)
            if not message or data.read(1):
                raise TransportError('云桌面中转消息边界无效。')
            self.cursor += 1
            return message
        return None

    def input(self, number, message):
        ack = self._call('desktop', id=number, data=base64.b64encode(message).decode('ascii'))
        if type(ack.get('id')) is not int or ack['id'] != number:
            raise TransportError('云桌面中转回执不匹配。')

    def close(self):
        with self.close_lock:
            if self.stop.is_set():
                return
            self.stop.set()
        # The remote worker also has a bounded inactivity timeout.
        self._call('close')


class _Forward:
    def __init__(self, port, pairing):
        self.peer = socket.create_connection(('127.0.0.1', port), timeout=8)
        try:
            self.peer.sendall(wire.AUTH + pairing.encode('ascii') + b'\n')
            self.peer.settimeout(None)
            self.reader = self.peer.makefile('rb')
        except BaseException:
            self.peer.close()
            raise

    def receive(self):
        return wire.read(self.reader, DOWNSTREAM)

    def input(self, number, message):
        self.peer.sendall(message)

    def close(self):
        with suppress(OSError):
            self.peer.shutdown(socket.SHUT_RDWR)
        self.peer.close()
        self.reader.close()


class DesktopStream:
    """Blocking read(n) for the local GPU thread. Send controls from a separate thread."""
    def __init__(self, peer):
        self.peer = peer
        self.changed = threading.Condition()
        self.commands = deque()
        self.unavailable = deque(maxlen=8)
        self.input_lock = threading.Lock()
        self.ready = self.ended = self.closed = False
        self.problem = None
        self.sent = self.confirmed = 0
        self.thread = threading.Thread(target=self._receive, name='GNOME drawing stream', daemon=True)
        self.thread.start()

    def _receive(self):
        index = total = 0
        previous = wire.ZERO
        digest = hashlib.sha256()
        try:
            while True:
                msg = self.peer.receive()
                if msg is None:
                    raise TransportError('桌面绘图连接中断。请重新连接以建立新的桌面绘图上下文。')
                kind, body = msg
                if kind == wire.BLOCK:
                    raw, previous = wire.unpack_block(body, index + 1, previous)
                    with self.changed:
                        self.changed.wait_for(lambda: self.closed or len(self.commands) < 2)
                        if self.closed:
                            return
                        self.commands.append(memoryview(raw))
                        self.changed.notify_all()
                    index += 1
                    total += len(raw)
                    digest.update(raw)
                else:
                    with self.changed:
                        if self.closed:
                            return
                        if kind == wire.READY:
                            if self.ready or wire.DIMENSIONS.unpack(body) != (800, 520):
                                raise TransportError('云桌面尺寸或就绪状态不符。')
                            self.ready = True
                        elif kind == wire.ACK:
                            seq, applied = wire.INPUT_ACK.unpack(body)
                            if seq != self.sent or seq != self.confirmed + 1 or applied != 1:
                                raise TransportError('原桌面未确认此次操作。')
                            self.confirmed = seq
                        elif kind == wire.UNAVAILABLE:
                            if not 1 <= body[0] <= len(wire.UNAVAILABLE_APPS):
                                raise TransportError('云端图面提示无效。')
                            app = wire.UNAVAILABLE_APPS[body[0] - 1]
                            if not self.unavailable or self.unavailable[-1] != app:
                                self.unavailable.append(app)
                        else:
                            if not self.ready or self.confirmed != self.sent or wire.FINAL.unpack(body) != (index, total, digest.digest()):
                                raise TransportError('云桌面绘图末尾校验失败。')
                            self.ended = True
                        self.changed.notify_all()
                        if self.ended:
                            return
        except (OSError, ValueError, EOFError) as exc:
            with self.changed:
                if not self.closed:
                    self.problem = TransportError('云桌面传输中断或校验失败。请重新连接。')
                    self.problem.__cause__ = exc
                self.changed.notify_all()
        finally:
            with suppress(OSError):
                self.peer.close()

    def read(self, size):
        if type(size) is not int or size < 0:
            raise ValueError('请指定非负的读取长度。')
        if not size:
            return b''
        with self.changed:
            self.changed.wait_for(lambda: self.commands or self.closed or self.ended or self.problem)
            if self.problem:
                raise self.problem
            if self.closed or not self.commands:
                return b''
            first = self.commands[0]
            data = bytes(first[:size])
            if len(data) == len(first):
                self.commands.popleft()
                self.changed.notify_all()
            else:
                self.commands[0] = first[len(data):]
            return data

    def _input(self, command):
        with self.input_lock:
            with self.changed:
                self.changed.wait_for(lambda: self.ready or self.closed or self.ended or self.problem, 45)
                if not self.ready or self.closed or self.ended or self.problem:
                    raise TransportError('云桌面尚未就绪或已断开。')
                number = self.sent + 1
                if number > 0xffffffff:
                    raise TransportError('输入序号已用尽。请重新连接。')
                self.sent = number
            try:
                self.peer.input(number, wire.packet(wire.INPUT, wire.SEQUENCE.pack(number) + command))
                with self.changed:
                    self.changed.wait_for(lambda: self.confirmed == number or self.closed or self.ended or self.problem, 25)
                    if self.confirmed != number:
                        raise TransportError('原桌面没有确认操作。')
            except (OSError, ValueError):
                with suppress(OSError):
                    self.close()
                raise TransportError('无法确认操作是否已执行。请重新连接后检查桌面。') from None
            return True

    def send_super(self):
        return self._input(b'S')

    def move(self, x, y):
        return self._input(encode_pointer(b'M', x, y))

    def click(self, x, y, button='left'):
        if button not in ('left', 'right'):
            raise ValueError('只支持左键或右键。')
        return self._input(encode_pointer(b'L' if button == 'left' else b'R', x, y))

    def key(self, name):
        return self._input(encode_key(name))

    def pop_unavailable(self):
        """Return an application whose surface needs a separate renderer, or None."""
        with self.changed:
            return self.unavailable.popleft() if self.unavailable else None

    def close(self):
        with self.changed:
            if self.closed:
                return
            self.closed = True
            self.commands.clear()
            self.changed.notify_all()
        try:
            self.peer.close()
        finally:
            if threading.current_thread() is not self.thread:
                self.thread.join(35)
                if self.thread.is_alive():
                    raise TransportError('本机读取线程尚未结束，云端中转会在闲置超时后清理。')

    def __enter__(self):
        return self

    def __exit__(self, typ, value, traceback):
        self.close()


def _credentials(port, pairing):
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(pairing, str) or not re.fullmatch('[A-Za-z0-9_-]{43}', pairing):
        raise ValueError('云桌面端口或配对码无效。')


def connect(box, port, pairing):
    _credentials(port, pairing)
    return DesktopStream(_Provider(box, port, pairing))


def connect_forwarded(port, pairing):
    _credentials(port, pairing)
    return DesktopStream(_Forward(port, pairing))
