"""Private, ephemeral CLRP transport for InstaBox HTTPS exec and file reads.

Each exec invokes this same stdlib-only source with one bounded JSON request on
stdin. The detached process owns one paired loopback connection. Its Unix socket
and pending message files are private to the session. No app or shell commands,
cloud credentials, target hosts, or file paths come from the control requests.
"""
import asyncio
import base64
from collections import deque
from contextlib import suppress
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import sys
import time

if __package__:
    from .frame_delta import encode_frame


HEADER = struct.Struct('!4sBBHI')
MAX_PAYLOAD = 32 * 1024 * 1024
INLINE = 64 * 1024
MAX_TEXT = 4096
# JSON metadata plus one base64-encoded committed-text envelope.
CONTROL_LIMIT = 6144
REPLY_LIMIT = 96 * 1024
IDLE = 180
SOCKET = 'control.sock'
OWNER = b'CLRP HTTPS relay 1\n'


class RelayError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise RelayError(code)


def input_bytes(encoded, *, click_only):
    # This source runs standalone inside /exec. Check transport direction,
    # envelope structure and bounds here. The cloud app's wire decoder owns
    # editing key and modifier semantics before dispatching to the real UI.
    require(isinstance(encoded, str) and 0 < len(encoded) <= 4 * ((HEADER.size + 4 + MAX_TEXT + 2) // 3),
            'invalid-input')
    raw = base64.b64decode(encoded, validate=True)
    require(len(raw) >= HEADER.size + 4, 'invalid-input')
    magic, version, kind, flags, length = HEADER.unpack_from(raw)
    require(magic == b'CLRP' and version == 1 and not flags and length == len(raw) - HEADER.size,
            'invalid-input')
    require(struct.unpack_from('!I', raw, HEADER.size)[0] != 0, 'invalid-input')
    if kind == 2:
        require(length == 12, 'invalid-click')
        x, y = struct.unpack_from('!ff', raw, HEADER.size + 4)
        require(math.isfinite(x) and math.isfinite(y) and 0 <= x < 8192 and 0 <= y < 8192, 'invalid-click')
    elif not click_only and kind == 4:
        require(length == 6 and raw[-2] <= 3 and raw[-1] > 0, 'invalid-key')
    elif not click_only and kind == 5:
        require(5 <= length <= MAX_TEXT + 4, 'invalid-text')
        raw[HEADER.size + 4:].decode('utf-8', 'strict')
    else:
        raise RelayError('invalid-input')
    return raw


def check_request(req):
    require(isinstance(req, dict) and isinstance(req.get('op'), str), 'invalid-request')
    op = req['op']
    extras = {'start': {'port', 'pair'}, 'poll': {'after'}, 'click': {'id', 'data'},
              'input': {'id', 'data'}, 'close': set()}
    extras['desktop'] = {'id', 'data'}
    optional = {'start': {'codec', 'protocol'}, 'poll': {'reset'}}
    require(op in extras and set(req) - optional.get(op, set()) == {'op', 'session'} | extras[op],
            'invalid-request')
    require(isinstance(req['session'], str) and re.fullmatch('[0-9a-f]{32}', req['session']), 'invalid-session')
    if op == 'start':
        require('protocol' not in req or (req['protocol'] == 'gnome' and 'codec' not in req), 'invalid-protocol')
        require('codec' not in req or req['codec'] == 'cdz1', 'invalid-codec')
        require(type(req['port']) is int and 1 <= req['port'] <= 65535, 'invalid-port')
        require(isinstance(req['pair'], str) and re.fullmatch('[A-Za-z0-9_-]{43}', req['pair']), 'invalid-pair')
    if op == 'poll':
        require('reset' not in req or req['reset'] is True, 'invalid-reset')
        require(type(req['after']) is int and 0 <= req['after'] < 2 ** 53, 'invalid-cursor')
    if op == 'desktop':
        require(type(req['id']) is int and 1 <= req['id'] <= 0xffffffff and
                isinstance(req['data'], str) and 0 < len(req['data']) <= 28, 'invalid-desktop-input')
        raw = base64.b64decode(req['data'], validate=True)
        require(17 <= len(raw) <= 21 and HEADER.unpack_from(raw) == (b'GVGL', 1, 2, 0, len(raw) - HEADER.size)
                and struct.unpack_from('!I', raw, HEADER.size)[0] == req['id'], 'invalid-desktop-input')
        command = raw[16:]
        if command[:1] in (b'M', b'L', b'R') and len(command) == 5:
            x, y = struct.unpack_from('!HH', command, 1)
            require(x < 800 and y < 520, 'invalid-desktop-input')
        else:
            require(command == b'S' or (command[:1] == b'K' and len(command) == 2 and 1 <= command[1] <= 15),
                    'invalid-desktop-input')
        return raw
    if op in ('click', 'input'):
        require(type(req['id']) is int and 1 <= req['id'] < 2 ** 53, 'invalid-input-id')
        return input_bytes(req['data'], click_only=op == 'click')


def session_dir(session):
    return Path('/tmp') / ('clrp-' + session)


def assert_owner(root):
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'session-owner')
    marker = root / 'owner'
    metadata = marker.lstat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == info.st_uid and metadata.st_size == len(OWNER)
            and marker.read_bytes() == OWNER, 'session-owner')


def clean(root):
    """Unlink only this program's entries, without following paths or subdirectories."""
    assert_owner(root)
    for entry in root.iterdir():
        if entry.name == SOCKET or re.fullmatch(r'msg-[1-9][0-9]{0,15}\.bin(?:\.part)?', entry.name):
            entry.unlink(missing_ok=True)
    if set(p.name for p in root.iterdir()) == {'owner'}:
        (root / 'owner').unlink()
        root.rmdir()


def message_file(root, index):
    return root / f'msg-{index}.bin'


class Relay:
    def __init__(self, root, start):
        self.root = root
        self.start = start
        self.writer = None
        self.messages = deque()
        self.queued_bytes = 0
        self.acked = self.published = self.offered = 0
        self.input_id = 0
        self.input_digest = None
        self.input_flushed = False
        self.input_lock = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.closed = False
        self.last_request = time.monotonic()
        self.controls = set()
        self.previous = b''

    async def receive(self):
        try:
            reader, self.writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.start['port']), 5)
            desktop = self.start.get('protocol') == 'gnome'
            prefix = 'GNOME-AUTH/1 ' if desktop else 'CLRP-AUTH/1 '
            self.writer.write((prefix + self.start['pair'] + '\n').encode('ascii'))
            await self.writer.drain()
            while True:
                header = await reader.readexactly(HEADER.size)
                magic, version, kind, flags, size = HEADER.unpack(header)
                if desktop:
                    require(magic == b'GVGL' and version == 1 and flags == 0 and
                            ((kind == 1 and 72 < size <= 72 + 1024 * 1024 + 4096)
                             or (kind in (3, 4, 5, 6) and size == {3: 5, 4: 4, 5: 44, 6: 1}[kind])),
                            'upstream-protocol')
                    require(self.published or kind in (1, 4, 6), 'upstream-protocol')
                else:
                    require(magic == b'CLRP' and version == 1 and flags == 0 and
                            ((kind == 1 and 10 <= size <= MAX_PAYLOAD) or (kind == 3 and size == 5)),
                            'upstream-protocol')
                    require(self.published or kind == 1, 'upstream-protocol')
                total = HEADER.size + size
                async with self.changed:
                    await self.changed.wait_for(lambda: len(self.messages) < 8 and
                                                self.queued_bytes + total <= 2 * (MAX_PAYLOAD + HEADER.size))
                index = self.published + 1
                receipt = {'kind': 'message', 'id': index, 'size': total}
                if self.start.get('codec') and kind == 1:
                    # The raw frame stays private until it is acknowledged. It
                    # can supply a standalone retry even if the viewer lost its base.
                    receipt['frame'] = header + await asyncio.wait_for(reader.readexactly(size), 30)
                elif total <= INLINE:
                    payload = await asyncio.wait_for(reader.readexactly(size), 30)
                    raw = header + payload
                    if kind == 3:
                        seq, applied = struct.unpack('!IB', payload)
                        require(seq != 0 and applied <= 1, 'upstream-protocol')
                    receipt.update(data=base64.b64encode(raw).decode('ascii'), sha256=hashlib.sha256(raw).hexdigest())
                else:
                    target = message_file(self.root, index)
                    part = target.with_suffix('.bin.part')
                    digest = hashlib.sha256(header)
                    with part.open('xb') as file:
                        file.write(header)
                        remain = size
                        while remain:
                            chunk = await asyncio.wait_for(reader.readexactly(min(remain, 65536)), 30)
                            file.write(chunk)
                            digest.update(chunk)
                            remain -= len(chunk)
                    part.replace(target)
                    receipt.update(file=True, sha256=digest.hexdigest())
                async with self.changed:
                    self.messages.append(receipt)
                    self.queued_bytes += total
                    self.published = index
                    self.ready.set()
                    self.changed.notify_all()
        except (OSError, EOFError, RelayError, asyncio.TimeoutError):
            pass
        finally:
            self.closed = True
            self.ready.set()
            if self.writer:
                self.writer.close()
            async with self.changed:
                self.changed.notify_all()

    def offer_frame(self, head, reset):
        if 'offer' not in head or (reset and not head.get('standalone')):
            frame = head['frame']
            packet = encode_frame(frame, b'' if reset else self.previous)
            receipt = {'kind': 'message', 'id': head['id'], 'size': len(packet),
                       'sha256': hashlib.sha256(packet).hexdigest()}
            if packet is not frame:
                receipt['codec'] = 'cdz1'
                receipt['frameSha256'] = hashlib.sha256(frame).hexdigest()
                receipt['frameSize'] = len(frame)
            target = message_file(self.root, head['id'])
            if len(packet) > INLINE:
                part = target.with_suffix('.bin.part')
                try:
                    with part.open('xb') as file:
                        file.write(packet)
                    part.replace(target)
                finally:
                    part.unlink(missing_ok=True)
                receipt['file'] = True
            else:
                receipt['data'] = base64.b64encode(packet).decode('ascii')
                target.unlink(missing_ok=True)
            head['offer'] = receipt
            head['standalone'] = reset or not self.previous or packet is frame
        return head['offer']

    async def poll(self, after, reset=False):
        async with self.changed:
            require(self.acked <= after <= self.offered, 'invalid-cursor')
            if reset:
                require(self.start.get('codec') and after == self.acked and self.messages and
                        self.offered == self.messages[0]['id'] and 'frame' in self.messages[0], 'invalid-reset')
            if after > self.acked:
                head = self.messages[0]
                require(head['id'] == after, 'invalid-cursor')
                if head.get('offer', head).get('file'):
                    message_file(self.root, after).unlink()
                if 'frame' in head:
                    self.previous = head['frame']
                self.messages.popleft()
                self.queued_bytes -= head['size']
                self.acked = after
                self.changed.notify_all()
            if not self.messages and not self.closed:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.changed.wait_for(lambda: self.messages or self.closed), 2.5)
            # A concurrent caller can retire this cursor while the poll is asleep.
            require(after == self.acked, 'invalid-cursor')
            if self.messages:
                self.offered = self.messages[0]['id']
                if 'frame' in self.messages[0]:
                    return self.offer_frame(self.messages[0], reset)
                return self.messages[0]
            return {'kind': 'closed' if self.closed else 'wait'}

    async def send_input(self, index, raw):
        async with self.input_lock:
            # Only a session-keyed fingerprint survives this request. A text
            # commit can contain a private conversation or a pasted credential.
            digest = hmac.digest(self.start['pair'].encode('ascii'), raw, 'sha256')
            if index == self.input_id:
                require(hmac.compare_digest(digest, self.input_digest), 'input-conflict')
                require(self.input_flushed, 'unconfirmed-input')
                return {'id': index}
            require(index == self.input_id + 1, 'invalid-input-id')
            require(self.published and not self.closed, 'upstream-closed')
            # Mark consumed before writing to the transport. An interrupted write
            # has an unknown application outcome and must never be attempted twice.
            self.input_id, self.input_digest, self.input_flushed = index, digest, False
            try:
                self.writer.write(raw)
                await asyncio.wait_for(self.writer.drain(), 5)
                self.input_flushed = True
            except (OSError, asyncio.TimeoutError):
                self.closed = True
                self.writer.close()
                raise RelayError('unconfirmed-input') from None
            return {'id': index}

    async def control(self, reader, writer):
        task = asyncio.current_task()
        self.controls.add(task)
        close = False
        try:
            require(len(self.controls) <= 16, 'busy')
            line = await asyncio.wait_for(reader.readline(), 3)
            require(len(line) <= CONTROL_LIMIT, 'invalid-request')
            req = json.loads(line)
            raw = check_request(req)
            require(hmac.compare_digest(self.start['session'], req['session']), 'invalid-session')
            self.last_request = time.monotonic()
            op = req['op']
            result = {}
            if op == 'start':
                require(self.start['port'] == req['port'] and hmac.compare_digest(self.start['pair'], req['pair']),
                        'session-conflict')
                require(req.get('codec') == self.start.get('codec') and
                        req.get('protocol') == self.start.get('protocol'), 'session-conflict')
                await asyncio.wait_for(self.ready.wait(), 35)
                require(self.published, 'upstream-unavailable')
            elif op == 'poll':
                result = await self.poll(req['after'], req.get('reset', False))
            elif op in ('click', 'input', 'desktop'):
                require((op == 'desktop') == (self.start.get('protocol') == 'gnome'), 'wrong-input-protocol')
                result = await self.send_input(req['id'], raw)
            else:
                close = True
            reply = {'ok': True, **result}
        except RelayError as exc:
            reply = {'ok': False, 'error': str(exc)}
        except (EOFError, OSError, ValueError, asyncio.TimeoutError):
            reply = {'ok': False, 'error': 'control-failed'}
        try:
            writer.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
            await asyncio.wait_for(writer.drain(), 3)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            self.controls.discard(task)
            if close:
                self.stopping.set()

    async def run(self, listener):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        source = asyncio.create_task(self.receive())
        try:
            async with await asyncio.start_unix_server(self.control, sock=listener, limit=CONTROL_LIMIT):
                while not self.stopping.is_set():
                    remaining = IDLE - (time.monotonic() - self.last_request)
                    if remaining <= 0:
                        break
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), min(remaining, 10))
        finally:
            source.cancel()
            for task in self.controls.copy():
                task.cancel()
            await asyncio.gather(source, *self.controls, return_exceptions=True)


def launch(root, req):
    os.umask(0o077)
    root.mkdir(mode=0o700)
    try:
        (root / 'owner').write_bytes(OWNER)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with listener:
            listener.bind(str(root / SOCKET))
            listener.listen(16)
            listener.setblocking(False)
            if os.fork() == 0:
                try:
                    os.setsid()
                    keep = listener.fileno()
                    null = os.open(os.devnull, os.O_RDWR)
                    for std in (0, 1, 2):
                        os.dup2(null, std)
                    for fd in os.listdir('/proc/self/fd'):
                        n = int(fd)
                        if n > 2 and n != keep:
                            with suppress(OSError):
                                os.close(n)
                    asyncio.run(Relay(root, req).run(listener))
                finally:
                    with suppress(OSError, RelayError):
                        clean(root)
                    os._exit(0)
    except Exception:
        with suppress(OSError, RelayError):
            clean(root)
        raise


def execute(req):
    check_request(req)
    root = session_dir(req['session'])
    if req['op'] == 'start':
        try:
            launch(root, req)
        except FileExistsError:
            pass  # A retried start must rendezvous with the same owner and upstream.
    # Concurrent starts can observe mkdir before the creator has published its
    # owner marker and listener. Only the first process creates an upstream.
    deadline = time.monotonic() + (2 if req['op'] == 'start' else 0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(40 if req['op'] == 'start' else 8)
        while True:
            try:
                assert_owner(root)
                control.connect(str(root / SOCKET))
                break
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                if time.monotonic() < deadline:
                    time.sleep(.02)
                    continue
                if req['op'] == 'close':
                    if not root.exists():
                        return {'ok': True}
                    # SIGKILL can leave a dead Unix socket and unpublished
                    # files behind. Never retire a socket still being bound.
                    if isinstance(exc, ConnectionRefusedError) and time.time() - (root / 'owner').stat().st_mtime > 5:
                        clean(root)
                        require(not root.exists(), 'cleanup-pending')
                        return {'ok': True}
                raise RelayError('session-not-found') from None
        control.sendall(json.dumps(req, separators=(',', ':')).encode() + b'\n')
        with control.makefile('rb') as stream:
            line = stream.readline(REPLY_LIMIT + 1)
        require(line.endswith(b'\n') and len(line) <= REPLY_LIMIT, 'invalid-reply')
        reply = json.loads(line)
    if req['op'] == 'close' and reply['ok']:
        deadline = time.monotonic() + 3
        while root.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        require(not root.exists(), 'cleanup-pending')
    return reply


def main():
    try:
        body = sys.stdin.buffer.read(CONTROL_LIMIT + 1)
        require(len(body) <= CONTROL_LIMIT, 'invalid-request')
        result = execute(json.loads(body))
    except RelayError as exc:
        result = {'ok': False, 'error': str(exc)}
    except (TypeError, ValueError, OSError, TimeoutError):
        result = {'ok': False, 'error': 'control-failed'}
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
