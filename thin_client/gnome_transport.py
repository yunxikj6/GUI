"""Read an authenticated original desktop command stream and control its dedicated source."""
import base64
from collections import deque
from contextlib import suppress
import hashlib
import io
import re
import secrets
import socket
import threading

from cloud_render import gnome_wire as wire
from cloud_render.instabox_relay import BATCH_BYTES, BATCH_COUNT, BATCH_INLINE
from cloud_render.gnome_input import DEFAULT_SIZE, checked_size, encode_key, encode_pointer, encode_select_all, encode_text
from .instabox_transport import InstaBoxControl, InvalidPayload, RequestOutcomeUnknown, TransportError


_FOCUS_REJECTED = object()

DOWNSTREAM = {wire.BLOCK, wire.ACK, wire.READY, wire.END, wire.UNAVAILABLE, wire.FOCUS}


class _Provider(InstaBoxControl):
    def __init__(self, box, port, pairing, size, wallpaper=False, search_input=False):
        super().__init__(box)
        self.cursor = 0
        self.pending = deque()
        try:
            dimensions = {} if size == DEFAULT_SIZE else dict(width=size[0], height=size[1])
            if search_input:
                dimensions['searchInput'] = True
                dimensions['selectAll'] = True
            if wallpaper:
                dimensions['wallpaper'] = True
            started = self._call('start', port=port, pair=pairing, protocol='gnome', **dimensions)
            self.search_input = started.get('searchInput') is True and search_input
            self.can_select_all = self.search_input and started.get('selectAll') is True
            self.batch = started.get('batch') is True
        except BaseException:
            self.close()
            raise

    def receive(self):
        while not self.stop.is_set():
            if not self.pending:
                options = {'batch': True} if self.batch else {}
                result = self._call('poll', after=self.cursor, **options)
                kind = result.get('kind')
                if kind == 'wait':
                    continue
                if kind == 'closed':
                    return None
                if self.batch:
                    items = result.get('messages')
                    if kind != 'batch' or not isinstance(items, list) or not 1 <= len(items) <= BATCH_COUNT:
                        raise InvalidPayload('云桌面批量绘图回执无效。')
                    total = inline = 0
                    for index, item in enumerate(items, self.cursor + 1):
                        if (not isinstance(item, dict) or type(item.get('id')) is not int
                                or item['id'] != index or type(item.get('size')) is not int
                                or not 12 <= item['size'] <= wire.MAX_PACKET + wire.HEADER.size):
                            raise InvalidPayload('云桌面批量绘图的编号或尺寸无效。')
                        total += item['size']
                        if 'data' in item:
                            if not isinstance(item['data'], str):
                                raise InvalidPayload('云桌面批量绘图的数据编码无效。')
                            inline += len(item['data'])
                    if total > BATCH_BYTES or inline > BATCH_INLINE:
                        raise InvalidPayload('云桌面批量绘图超过下载上限。')
                    self.pending.extend(items)
                else:
                    self.pending.append(result)
            item = self.pending[0]
            if item.get('kind') != 'message' or 'codec' in item:
                raise InvalidPayload('云桌面中转消息类型无效。')
            raw = self.verified_bytes(item, self.cursor + 1, wire.MAX_PACKET + wire.HEADER.size)
            data = io.BytesIO(raw)
            message = wire.read(data, DOWNSTREAM)
            if not message or data.read(1):
                raise InvalidPayload('云桌面中转消息边界无效。')
            self.pending.popleft()
            self.cursor += 1
            return message
        return None

    def input(self, number, message):
        ack = self._call('desktop', id=number, data=base64.b64encode(message).decode('ascii'))
        if type(ack.get('id')) is not int or ack['id'] != number:
            raise InvalidPayload('云桌面中转回执不匹配。')
        if 'confirmed' in ack and type(ack['confirmed']) is not bool:
            raise InvalidPayload('原桌面操作确认无效。')
        if ack.get('applied', True) is not True:
            if not self.search_input or ack.get('applied') is not False or not ack.get('confirmed'):
                raise InvalidPayload('云端输入确认无效。')
            return _FOCUS_REJECTED
        return ack.get('confirmed', False)

    def close(self):
        with self.close_lock:
            if self.stop.is_set():
                return
            self.stop.set()
        # The remote worker also has a bounded inactivity timeout.
        self._call('close')


class _Forward:
    def __init__(self, port, pairing, size, wallpaper=False, search_input=False):
        flags = 0
        requested = (wire.WALLPAPER if wallpaper else 0) | ((wire.SEARCH_INPUT | wire.SELECT_ALL) if search_input else 0)
        if requested:
            with socket.create_connection(('127.0.0.1', port), timeout=5) as query, query.makefile('rb') as reply:
                query.sendall(wire.QUERY + pairing.encode('ascii') + b'\n')
                capabilities = reply.read(wire.FEATURES.size)
                if len(capabilities) == wire.FEATURES.size:
                    magic, offered = wire.FEATURES.unpack(capabilities)
                    if magic == wire.FEATURE_MAGIC:
                        flags = offered & requested
        if not flags & wire.SEARCH_INPUT:
            flags &= ~wire.SELECT_ALL
        self.search_input = bool(flags & wire.SEARCH_INPUT)
        self.can_select_all = bool(flags & wire.SELECT_ALL)
        self.peer = socket.create_connection(('127.0.0.1', port), timeout=8)
        try:
            extended = bool(flags) or size != DEFAULT_SIZE
            prefix = wire.AUTH_SIZE if extended else wire.AUTH
            dimensions = wire.SESSION.pack(*size, flags) if extended else b''
            self.peer.sendall(prefix + pairing.encode('ascii') + b'\n' + dimensions)
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
    def __init__(self, peer, *, size=DEFAULT_SIZE):
        self.size = checked_size(size)
        self.peer = peer
        self.changed = threading.Condition()
        self.commands = deque()
        self.unavailable = deque(maxlen=8)
        self.input_lock = threading.Lock()
        self.ready = self.ended = self.closed = False
        self.problem = None
        self.focus_namespace = secrets.randbits(128) + 1
        self.focus_revision = 0
        self.focus_active = False
        self.last_applied = True
        self.early_results = {}
        self.sent = self.confirmed = self.ordered_ack = 0
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
                            if self.ready or wire.DIMENSIONS.unpack(body) != self.size:
                                raise InvalidPayload('云桌面尺寸或就绪状态不符。')
                            self.ready = True
                        elif kind == wire.FOCUS:
                            rev, active = wire.FOCUS_STATE.unpack(body)
                            if (not getattr(self.peer, 'search_input', False) or
                                    rev != self.focus_revision + 1 or active not in (0, 1)):
                                raise InvalidPayload('原桌面搜索焦点无效。')
                            self.focus_revision, self.focus_active = rev, bool(active)
                        elif kind == wire.ACK:
                            seq, applied = wire.INPUT_ACK.unpack(body)
                            if (seq != self.ordered_ack + 1 or seq > self.sent or applied not in (0, 1) or
                                    (not applied and not getattr(self.peer, 'search_input', False))):
                                raise InvalidPayload('原桌面未确认此次操作。')
                            expected = self.early_results.pop(seq, None)
                            if expected is not None and bool(applied) != expected:
                                raise InvalidPayload('原桌面两次输入回执不一致。')
                            self.ordered_ack = seq
                            if seq >= self.confirmed:
                                self.confirmed = seq
                                self.last_applied = bool(applied)
                        elif kind == wire.UNAVAILABLE:
                            if not 1 <= body[0] <= len(wire.UNAVAILABLE_APPS):
                                raise InvalidPayload('云端图面提示无效。')
                            app = wire.UNAVAILABLE_APPS[body[0] - 1]
                            if not self.unavailable or self.unavailable[-1] != app:
                                self.unavailable.append(app)
                        else:
                            if not self.ready or self.ordered_ack != self.sent or wire.FINAL.unpack(body) != (index, total, digest.digest()):
                                raise InvalidPayload('云桌面绘图末尾校验失败。')
                            self.ended = True
                        self.changed.notify_all()
                        if self.ended:
                            return
        except (OSError, ValueError, EOFError) as exc:
            with self.changed:
                if not self.closed:
                    problem = InvalidPayload if isinstance(exc, ValueError) else TransportError
                    self.problem = problem('云桌面传输中断或校验失败。请重新连接。')
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
                # The fast provider reply can arrive before the drawing-ordered
                # receipt. Bound the decisions retained for the second check.
                if not self.changed.wait_for(lambda: len(self.early_results) < 64 or self.closed or self.ended or self.problem, 25):
                    raise TransportError('等待原桌面输入回执超时。')
                if self.closed or self.ended or self.problem:
                    raise TransportError('云桌面输入连接已断开。')
                number = self.sent + 1
                if number > 0xffffffff:
                    raise TransportError('输入序号已用尽。请重新连接。')
                self.sent = number
            try:
                try:
                    applied = self.peer.input(number, wire.packet(wire.INPUT, wire.SEQUENCE.pack(number) + command))
                except RequestOutcomeUnknown:
                    # HTTPS may lose both responses to the same idempotent
                    # write. The independently ordered, authenticated source
                    # ACK still determines its outcome, without another write.
                    applied = False
                with self.changed:
                    if applied is True or applied is _FOCUS_REJECTED:
                        decision = applied is True
                        if self.ordered_ack == number and self.last_applied != decision:
                            raise InvalidPayload('原桌面两次输入回执不一致。')
                        if self.ordered_ack < number:
                            self.early_results[number] = decision
                        self.confirmed = number
                        self.last_applied = decision
                        self.changed.notify_all()
                    self.changed.wait_for(lambda: self.confirmed == number or self.closed or self.ended or self.problem, 25)
                    if self.confirmed != number:
                        raise TransportError('原桌面没有确认操作。')
                    return self.last_applied
            except InvalidPayload:
                raise
            except (OSError, ValueError):
                with suppress(OSError):
                    self.close()
                raise TransportError('无法确认操作是否已执行。请重新连接后检查桌面。') from None

    def text_focus(self):
        with self.changed:
            if self.focus_active and self.ready and not (self.closed or self.ended or self.problem):
                return (self.focus_namespace << 32) | self.focus_revision
            return None

    def commit_text(self, revision, text):
        if type(revision) is not int or self.text_focus() != revision:
            return False
        return self._input(encode_text(revision & 0xffffffff, text))

    def select_all(self, revision):
        if (not getattr(self.peer, 'can_select_all', False) or
                type(revision) is not int or self.text_focus() != revision):
            return False
        return self._input(encode_select_all(revision & 0xffffffff))

    def send_super(self):
        return self._input(b'S')

    def move(self, x, y):
        return self._input(encode_pointer(b'M', x, y, size=self.size))

    def click(self, x, y, button='left'):
        if button not in ('left', 'right'):
            raise ValueError('只支持左键或右键。')
        return self._input(encode_pointer(b'L' if button == 'left' else b'R', x, y, size=self.size))

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


def connect(box, port, pairing, *, size=DEFAULT_SIZE, wallpaper=False, search_input=False):
    _credentials(port, pairing)
    return DesktopStream(_Provider(box, port, pairing, checked_size(size), wallpaper, search_input), size=size)


def connect_forwarded(port, pairing, *, size=DEFAULT_SIZE, wallpaper=False, search_input=False):
    _credentials(port, pairing)
    return DesktopStream(_Forward(port, pairing, checked_size(size), wallpaper, search_input), size=size)
