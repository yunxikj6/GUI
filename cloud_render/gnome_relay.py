"""Pair one private viewer with a fresh, dedicated GNOME Shell and its drawing stream."""
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
from functools import partial
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys

from . import gnome_wire as wire
from .gnome_input import DEFAULT_SIZE, checked_size, decode_input


TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


@asynccontextmanager
async def open_source(session_mode='ubuntu', *, size=DEFAULT_SIZE, wallpaper=False, search_input=False, focus_delete=False):
    width, height = checked_size(size)
    options = [] if size == DEFAULT_SIZE else ['--size', f'{width}x{height}']
    if wallpaper:
        options.append('--wallpaper')
    if search_input:
        options.append('--search-input')
    if focus_delete:
        options.append('--focus-delete')
    read_fd, write_fd = os.pipe()
    events = asyncio.StreamReader(limit=2048)
    transport = proc = None
    try:
        with os.fdopen(read_fd, 'rb', buffering=0) as pipe:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(events), pipe)
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'cloud_render.gnome_source', '--event-fd', str(write_fd),
                    '--session-mode', session_mode, *options,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, pass_fds=(write_fd,), start_new_session=True,
                    cwd=Path(__file__).resolve().parents[1],
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
            finally:
                os.close(write_fd)
                write_fd = -1
            yield proc, events
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if transport:
            transport.close()
        if proc:
            proc.stdin.close()
            try:
                # The source closes its several dedicated process groups in
                # order after stdin EOF. Let that cleanup finish before signaling.
                await asyncio.wait_for(proc.wait(), 15)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 12)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()


class _SourceLink:
    def __init__(self, source, events, reader, writer, size, features=0):
        self.features = features
        self.search_input = bool(features & wire.SEARCH_INPUT)
        self.focus_revision = 0
        self.salt = secrets.token_bytes(32)
        self.last_applied = True
        self.size = size
        self.source = source
        self.events = events
        self.reader = reader
        self.writer = writer
        self.output = asyncio.Lock()
        self.ready = asyncio.Event()
        self.pending = None
        self.sequence = 0
        self.last_command = None

    async def send(self, data):
        async with self.output:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), 30)

    async def drawing(self):
        index = total = 0
        chain = wire.ZERO
        digest = hashlib.sha256()
        while True:
            first = await self.source.stdout.read(65536)
            if not first:
                break
            data = bytearray(first)
            cutoff = asyncio.get_running_loop().time() + .09
            while len(data) < wire.MAX_RAW:
                left = cutoff - asyncio.get_running_loop().time()
                if left <= 0:
                    break
                try:
                    part = await asyncio.wait_for(self.source.stdout.read(min(65536, wire.MAX_RAW - len(data))), left)
                except TimeoutError:
                    break
                if not part:
                    break
                data.extend(part)
            index += 1
            total += len(data)
            digest.update(data)
            block, chain = await asyncio.to_thread(wire.pack_block, data, index, chain)
            await self.send(block)
        return wire.FINAL.pack(index, total, digest.digest())

    async def observations(self):
        frames = 0
        last_notice = {}
        while line := await self.events.readline():
            if len(line) > 2048:
                raise wire.DesktopError('桌面源事件超过上限。')
            result = json.loads(line)
            if not isinstance(result, dict):
                raise wire.DesktopError('源端桌面事件无效。')
            kind = result.get('event')
            if kind == 'frame':
                if (type(result.get('sequence')) is not int or result['sequence'] != frames + 1 or
                        not isinstance(result.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', result['sha256'])):
                    raise wire.DesktopError('源端帧确认无效。')
                frames += 1
            elif kind == 'ready' and not self.ready.is_set():
                if (result.get('width'), result.get('height')) != self.size:
                    raise wire.DesktopError('源端桌面尺寸不受支持。')
                await self.send(wire.packet(wire.READY, wire.DIMENSIONS.pack(*self.size)))
                self.ready.set()
            elif kind == 'input' and self.pending and not self.pending.done():
                if type(result.get('sequence')) is not int or result['sequence'] != self.sequence:
                    raise wire.DesktopError('源端输入回执次序不符。')
                applied = result.get('applied', True)
                if type(applied) is not bool or (not applied and not self.search_input):
                    raise wire.DesktopError('原厂输入法操作结果无效。')
                self.pending.set_result(applied)
            elif kind == 'focus' and self.search_input:
                revision, active = result.get('revision'), result.get('active')
                if (type(revision) is not int or revision != self.focus_revision + 1 or
                        revision > 0xffffffff or type(active) is not bool):
                    raise wire.DesktopError('原厂输入焦点序号无效。')
                self.focus_revision = revision
                await self.send(wire.packet(wire.FOCUS, wire.FOCUS_STATE.pack(revision, active)))
            elif kind == 'unavailable' and result.get('application') in wire.UNAVAILABLE_APPS:
                app = result['application']
                now = asyncio.get_running_loop().time()
                if now - last_notice.get(app, float('-inf')) >= 1.0:
                    last_notice[app] = now
                    code = wire.UNAVAILABLE_APPS.index(app) + 1
                    await self.send(wire.packet(wire.UNAVAILABLE, bytes([code])))
            else:
                raise wire.DesktopError('源端桌面事件无效。')
        await asyncio.wait_for(self.source.wait(), 5)

    async def controls(self):
        last_action = 0.
        while msg := await wire.read_async(self.reader, {wire.INPUT}):
            body = msg[1]
            seq, = wire.SEQUENCE.unpack_from(body)
            command = body[wire.SEQUENCE.size:]
            kind, _ = decode_input(command, size=self.size)
            needed = {b'A': wire.SELECT_ALL, b'T': wire.SEARCH_INPUT,
                      b'B': wire.FOCUS_DELETE, b'D': wire.FOCUS_DELETE}.get(kind)
            if needed and not self.features & needed:
                raise wire.DesktopError('当前会话未协商原厂文字输入。')
            if self.features & wire.FOCUS_DELETE and command in (b'K\x08', b'K\x09'):
                raise wire.DesktopError('删除操作需要原厂焦点编号。')
            fingerprint = hmac.digest(self.salt, command, 'sha256')
            if seq == self.sequence and seq and hmac.compare_digest(fingerprint, self.last_command):
                await self.send(wire.packet(wire.ACK, wire.INPUT_ACK.pack(seq, self.last_applied)))
                continue
            if seq != self.sequence + 1:
                raise wire.DesktopError('桌面输入重复或次序不符。')
            await asyncio.wait_for(self.ready.wait(), 45)
            # Only one outstanding action can reach the original desktop. Limit
            # even a paired client that floods it with pointer motion.
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0., last_action + 1 / 60 - loop.time()))
            last_action = loop.time()
            self.sequence = seq
            self.pending = loop.create_future()
            self.source.stdin.write(command)
            await asyncio.wait_for(self.source.stdin.drain(), 5)
            self.last_applied = await asyncio.wait_for(self.pending, 10)
            if not self.last_applied and kind not in (b'T', b'A', b'B', b'D'):
                raise wire.DesktopError('原桌面导航操作未经确认。')
            self.pending = None
            self.last_command = fingerprint
            await self.send(wire.packet(wire.ACK, wire.INPUT_ACK.pack(seq, self.last_applied)))

    async def run(self):
        draw = asyncio.create_task(self.drawing())
        metadata = asyncio.create_task(self.observations())
        inputs = asyncio.create_task(self.controls())
        source = asyncio.gather(draw, metadata, self.source.wait())
        try:
            done, _ = await asyncio.wait({source, inputs}, return_when=asyncio.FIRST_COMPLETED)
            if inputs in done:
                await inputs
            else:
                final, _, code = await source
                if code or self.pending is not None or not self.ready.is_set():
                    raise wire.DesktopError('源端桌面意外结束。')
                await self.send(wire.packet(wire.END, final))
        finally:
            for task in (source, draw, metadata, inputs):
                task.cancel()
            await asyncio.gather(source, draw, metadata, inputs, return_exceptions=True)


_GNOME_FEATURES = wire.WALLPAPER | wire.SEARCH_INPUT | wire.SELECT_ALL | wire.FOCUS_DELETE


def _valid_features(flags):
    return (type(flags) is int and flags >= 0 and not flags & ~(_GNOME_FEATURES | wire.APP_WALLPAPER | wire.APP_PICTURE_DELTA) and
            (not flags & (wire.SELECT_ALL | wire.FOCUS_DELETE) or bool(flags & wire.SEARCH_INPUT)))


class DesktopService:
    def __init__(self, token, source_factory=open_source, *, features=_GNOME_FEATURES):
        if not callable(features) and not _valid_features(features):
            raise ValueError('invalid desktop source capabilities')
        self.features = features
        if not TOKEN.fullmatch(token):
            raise ValueError('无效的云桌面配对码。')
        self.expected = wire.AUTH + token.encode('ascii') + b'\n'
        self.expected_size = wire.AUTH_SIZE + token.encode('ascii') + b'\n'
        self.expected_query = wire.QUERY + token.encode('ascii') + b'\n'
        self.source_factory = source_factory
        self.busy = False
        self.active = set()

    async def enabled_features(self):
        # The source can finish booting after the private server starts. Resolve
        # its bound capability record only after authenticating the caller.
        flags = await self.features() if callable(self.features) else self.features
        if not _valid_features(flags):
            raise ValueError('invalid desktop source capabilities')
        return flags

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        owned = False
        try:
            if len(self.active) > 16:
                return
            login = await asyncio.wait_for(reader.readexactly(len(self.expected)), 5)
            if hmac.compare_digest(login, self.expected_query):
                writer.write(wire.FEATURES.pack(wire.FEATURE_MAGIC, await self.enabled_features()))
                await writer.drain()
                return
            options = {}
            features = 0
            if hmac.compare_digest(login, self.expected):
                size = DEFAULT_SIZE
            elif hmac.compare_digest(login, self.expected_size):
                raw = await asyncio.wait_for(reader.readexactly(wire.SESSION.size), 5)
                width, height, offered = wire.SESSION.unpack(raw)
                if not _valid_features(offered) or offered & ~(await self.enabled_features()):
                    return
                if offered & wire.SEARCH_INPUT:
                    options['search_input'] = True
                    features = offered & (wire.SEARCH_INPUT | wire.SELECT_ALL | wire.FOCUS_DELETE)
                    if features & wire.FOCUS_DELETE:
                        options['focus_delete'] = True
                if offered & wire.APP_PICTURE_DELTA:
                    options['picture_delta'] = True
                if offered & wire.APP_WALLPAPER:
                    options['app_wallpaper'] = True
                if offered & wire.WALLPAPER:
                    options['wallpaper'] = True
                size = checked_size((width, height))
            else:
                return
            if self.busy:
                return
            self.busy = owned = True
            if size != DEFAULT_SIZE:
                options['size'] = size
            factory = self.source_factory(**options)
            async with factory as (source, observations):
                link = _SourceLink(source, observations, reader, writer, size, features)
                await link.run()
        except (OSError, EOFError, ValueError, TimeoutError):
            pass
        finally:
            if owned:
                self.busy = False
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.active.discard(task)

    async def close(self):
        tasks = tuple(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def listen(port, token, session_mode):
    service = DesktopService(token, partial(open_source, session_mode))
    server = await asyncio.start_server(service.accept, '127.0.0.1', port, limit=1024, backlog=8)
    try:
        async with server:
            print(f'云桌面私有绘图端口 {server.sockets[0].getsockname()[1]}。', flush=True)
            await server.serve_forever()
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description='运行专用 GNOME 桌面，转发原始 GPU 绘图指令。')
    parser.add_argument('--port', type=int, default=17325)
    parser.add_argument('--session-mode', choices=('ubuntu', 'gnome'), default='ubuntu',
                        help='默认运行 Ubuntu 原厂会话。gnome 供纯 GNOME 环境诊断。')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('无效端口。')
    token = os.environ.pop('CLOUDWORKSPACE_GNOME_TOKEN', None)
    if token is None:
        if not sys.stderr.isatty():
            parser.error('后台运行需要设置 CLOUDWORKSPACE_GNOME_TOKEN。')
        token = secrets.token_urlsafe(32)
        print('本次云桌面配对码 ' + token, file=sys.stderr)
    if not TOKEN.fullmatch(token):
        parser.error('无效的云桌面配对码。')
    try:
        asyncio.run(listen(args.port, token, args.session_mode))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f'无法启动云桌面绘图服务（{type(exc).__name__}）。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
