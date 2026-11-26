"""Expose one verified interactive source on its paired private loopback endpoint."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import zlib

from cloud_render import frame_delta, gnome_commands as mesa, gnome_wallpaper as artwork
from cloud_render.gnome_input import decode_input
from cloud_render.gnome_wire import FOCUS_STATE, SEARCH_INPUT, SELECT_ALL, FOCUS_DELETE, APP_WALLPAPER, APP_PICTURE_DELTA, APP_WALLPAPER_X99
from cloud_render.gnome_relay import DesktopService, TOKEN
from experiments.application_surface import SurfaceError
from experiments.live_application_surface import BIND, DRAW, DRAW_DELTA, DRAW_PREFIX_BYTES, HOLD, REUSE, binding_record
from experiments.original_desktop_protocol import SIZE, approved_binding, source_hello


def _private(path, predicate):
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not predicate(info.st_mode):
        raise SurfaceError('the original application channel must be owner-private')


def _metadata(path):
    _private(path, stat.S_ISREG)
    with path.open('rb') as source:
        body = source.read(2049)
    if len(body) > 2048:
        raise SurfaceError('source identity exceeds the supported size')
    return json.loads(body)


def supported_editor_features(channel):
    _private(channel, stat.S_ISDIR)
    config = _metadata(channel/'session.json')
    source_hello(config)
    try:
        definition = _metadata(channel/'editor-capabilities.json')
    except FileNotFoundError:
        try:
            _private(channel/'focus.events', stat.S_ISREG)
        except FileNotFoundError:
            return 0
        # The previous interactive source defined focus observation plus text
        # and selection. It had no way to bind a deletion to the original focus.
        return SEARCH_INPUT | SELECT_ALL
    if (definition.get('session') != config['session'] or type(definition.get('flags')) is not int or
            definition['flags'] not in (0, SEARCH_INPUT, SEARCH_INPUT | SELECT_ALL,
                                        SEARCH_INPUT | FOCUS_DELETE, SEARCH_INPUT | SELECT_ALL | FOCUS_DELETE)):
        raise SurfaceError('unsupported original editor capability record')
    if definition['flags']:
        _private(channel/'focus.events', stat.S_ISREG)
    return definition['flags']


def _compact_drawing(body, previous):
    picture = memoryview(body)[DRAW_PREFIX_BYTES:]
    digest = body[DRAW_PREFIX_BYTES-32:DRAW_PREFIX_BYTES]
    if not 0 < len(picture) <= frame_delta.MAX_FRAME or hashlib.sha256(picture).digest() != digest:
        raise SurfaceError('original drawing digest or size mismatch')
    patch = frame_delta.encode_frame(picture, previous)
    if patch is not picture:
        # Keep the original if its ordinary GVGL deflate is about as small.
        # A clear savings lets us stop the comparison before the rest is deflated.
        cutoff = len(patch) + 256 + len(patch) // 100
        compressor = zlib.compressobj(3)
        produced = 0
        for pos in range(0, len(picture), 65536):
            produced += len(compressor.compress(picture[pos:pos+65536]))
            if produced > cutoff:
                break
        else:
            produced += len(compressor.flush())
        if produced > cutoff:
            header = mesa._HEADER.pack(DRAW_PREFIX_BYTES + len(patch), DRAW_DELTA)
            return header + body[:DRAW_PREFIX_BYTES] + patch, picture
    return mesa._HEADER.pack(len(body), DRAW) + body, picture


class _Graphics:
    """Forward only whole, session-bound records from the source's private FIFO."""
    def __init__(self, channel, reader, config, eof, *, app_wallpaper=False, picture_delta=False, app_wallpaper_x99=False):
        self.channel, self.reader, self.eof = channel, reader, eof
        self.buffer = memoryview(source_hello(config))
        self.task = None
        self.app_wallpaper = app_wallpaper
        self.app_wallpaper_x99 = app_wallpaper_x99
        self.picture_delta = picture_delta
        self.previous_picture = b''
        self.bound_handle = None
        self.artwork_references = 0

    async def _packet(self):
        try:
            header = await self.reader.readexactly(mesa._HEADER.size)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise SurfaceError('truncated application graphics header') from exc
            self.eof()
            return b''
        size, op = mesa._HEADER.unpack(header)
        if op == BIND:
            if size != 120:  # The original live binding format is <Q32s32sI32sIII.
                raise SurfaceError('invalid application identity record length')
            body = await self.reader.readexactly(size)
            offset = int.from_bytes(body[:8], 'little')
            independent = approved_binding(_metadata(self.channel/'binding.json'))
            if binding_record(offset, independent) != header + body:
                raise SurfaceError('graphics binding and independent Wayland witness disagree')
            self.previous_picture = b''
            self.bound_handle = independent.handle
            return header + body
        if op in (DRAW, HOLD, REUSE):
            if not 60 <= size <= (32 << 20) + 104:
                raise SurfaceError('invalid native drawing size')
        elif op == mesa.PUT:
            descriptor = await self.reader.readexactly(mesa._TRANSFER.size)
            handle, *_, count = mesa._TRANSFER.unpack(descriptor)
            if handle == self.bound_handle:
                raise SurfaceError('unclassified upload for the original application')
            if count > 32 << 20 or size != 11 + (count + 3)//4:
                raise SurfaceError('invalid original texture length')
            pixels = await self.reader.readexactly(count)
            if self.app_wallpaper and self.artwork_references < artwork.MAX_REFERENCES:
                reference = artwork.reference_for_upload(descriptor, pixels, x99=self.app_wallpaper_x99)
                if reference is not None:
                    self.artwork_references += 1
                    return reference
            return header + descriptor + pixels
        elif op == mesa.RENDERER:
            if not 0 < size <= 64:
                raise SurfaceError('invalid native renderer record')
        elif op in mesa._LENGTHS or op == mesa.SUBMIT:
            if op in mesa._LENGTHS and size != mesa._LENGTHS[op]:
                raise SurfaceError('invalid Mesa command header')
            size *= 4
            if size > 32 << 20:
                raise SurfaceError('excessive Mesa command size')
        else:
            raise SurfaceError('unsupported application-aware GNOME command')
        body = await self.reader.readexactly(size)
        if op == DRAW and self.picture_delta:
            handle_at = DRAW_PREFIX_BYTES - 32 - mesa._TRANSFER.size
            if len(body) <= DRAW_PREFIX_BYTES or mesa._WORD.unpack_from(body, handle_at)[0] != self.bound_handle:
                raise SurfaceError('original drawing outside an active application binding')
            packet, self.previous_picture = await asyncio.to_thread(_compact_drawing, body, self.previous_picture)
            return packet
        if op == mesa.UNREF and mesa._WORD.unpack(body)[0] == self.bound_handle:
            self.previous_picture = b''
            self.bound_handle = None
        return header + body

    async def read(self, size):
        if not self.buffer:
            if self.task is None:
                self.task = asyncio.create_task(self._packet())
            # The production block packer imposes short coalescing deadlines.
            # A cancelled partial block must not discard part of a source packet.
            self.buffer = memoryview(await asyncio.shield(self.task))
            self.task = None
        data = self.buffer[:size].tobytes()
        self.buffer = self.buffer[len(data):]
        return data


class _Pointer:
    def __init__(self, channel, events, *, editing, focus_delete):
        self.channel, self.events = channel, events
        self.editing = editing
        self.focus_delete = focus_delete
        self.fd = None
        self.sequence = 0
        self.pending = None

    async def open(self):
        deadline = asyncio.get_running_loop().time() + 90
        while self.fd is None:
            try:
                self.fd = os.open(self.channel/'input.fifo', os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno != errno.ENXIO or asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(.05)
        self.events.feed_data(b'{"event":"ready","width":1280,"height":900}\n')

    def write(self, command):
        op, _ = decode_input(command, size=SIZE)
        allowed = (b'M', b'L', b'T', b'A') if self.editing else (b'M', b'L')
        if self.focus_delete:
            allowed += (b'B', b'D')
        if op not in allowed or self.pending is not None or self.fd is None:
            raise SurfaceError('unsupported or unacknowledged native pointer input')
        self.pending = command

    async def drain(self):
        command, self.pending = self.pending, None
        if self.fd is None or command is None:
            raise SurfaceError('incomplete native input')
        remaining = memoryview(command)
        while remaining:
            try:
                written = os.write(self.fd, remaining)
            except BlockingIOError:
                # The largest UTF-8 commit can exceed PIPE_BUF by its header.
                # Only this paired control task writes here; continue exactly
                # where this write stopped, inside the relay's bounded deadline.
                await asyncio.sleep(.005)
                continue
            if not written:
                raise SurfaceError('private input pipe closed')
            remaining = remaining[written:]
        self.sequence += 1
        while True:
            try:
                ack = _metadata(self.channel/'last-input.json')
            except FileNotFoundError:
                ack = None
            if ack is not None:
                number = ack.get('sequence')
                if type(number) is not int or not 0 < number <= self.sequence:
                    raise SurfaceError('native input acknowledgement out of order')
                if number == self.sequence:
                    applied = ack.get('applied')
                    if type(applied) is not bool or (not applied and command[:1] not in (b'T', b'A', b'B', b'D')):
                        raise SurfaceError('invalid original input result')
                    event = dict(event='input', sequence=number, applied=applied)
                    self.events.feed_data(json.dumps(event).encode() + b'\n')
                    return
            await asyncio.sleep(.01)

    async def follow_focus(self):
        # Only this source writes its journal. Keep one open file description so
        # replacing its path cannot splice another window's epochs into the stream.
        path = self.channel / 'focus.events'
        while True:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                break
            except FileNotFoundError:
                await asyncio.sleep(.025)
        with os.fdopen(fd, 'rb', buffering=0) as source:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SurfaceError('focus observations must come from an owner-private regular file')
            expected, partial = 1, b''
            while True:
                chunk = source.read(FOCUS_STATE.size * 64)
                if not chunk:
                    await asyncio.sleep(.025)
                    continue
                data = partial + chunk
                complete = len(data) - len(data) % FOCUS_STATE.size
                for revision, active in FOCUS_STATE.iter_unpack(data[:complete]):
                    if revision != expected or active not in (0, 1):
                        raise SurfaceError('invalid original editor focus sequence')
                    expected += 1
                    event = dict(event='focus', revision=revision, active=bool(active))
                    self.events.feed_data(json.dumps(event).encode() + b'\n')
                partial = data[complete:]
                await asyncio.sleep(0)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _Peer:
    def __init__(self, stdout, stdin, finished):
        self.stdout, self.stdin, self.finished = stdout, stdin, finished

    async def wait(self):
        await self.finished.wait()
        return 0


@asynccontextmanager
async def interactive_fifos(channel, *, size=SIZE, wallpaper=False, search_input=False, focus_delete=False, app_wallpaper=False, picture_delta=False, app_wallpaper_x99=False):
    if size != SIZE or wallpaper or (focus_delete and not search_input) or (app_wallpaper_x99 and not app_wallpaper):
        raise SurfaceError('unsupported application desktop configuration')
    _private(channel, stat.S_ISDIR)
    for name in ('graphics.fifo','input.fifo'):
        _private(channel/name, stat.S_ISFIFO)
    flags = supported_editor_features(channel)
    if (search_input and not flags & SEARCH_INPUT) or (focus_delete and not flags & FOCUS_DELETE):
        raise SurfaceError('source lacks the requested native input capability')
    config = _metadata(channel/'session.json')
    source_hello(config)
    finished = asyncio.Event()
    events = asyncio.StreamReader()
    reader = asyncio.StreamReader(limit=128 << 10)
    source_fd = os.open(channel/'graphics.fifo', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(source_fd, 'rb', buffering=0) as pipe:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe)
        def eof():
            finished.set()
            events.feed_eof()
        graphics = _Graphics(channel, reader, config, eof, app_wallpaper=app_wallpaper, picture_delta=picture_delta,
                             app_wallpaper_x99=app_wallpaper_x99)
        pointer = _Pointer(channel, events, editing=search_input, focus_delete=focus_delete)
        opening = asyncio.create_task(pointer.open())
        def opened(task):
            if not task.cancelled() and task.exception() is not None:
                events.set_exception(task.exception())
        opening.add_done_callback(opened)
        focus = asyncio.create_task(pointer.follow_focus()) if search_input else None
        if focus:
            focus.add_done_callback(opened)
        try:
            yield _Peer(graphics, pointer, finished), events
        finally:
            tasks = [task for task in (opening, focus, graphics.task) if task]
            for task in tasks:
                task.cancel()
            # Deliver input EOF even if service shutdown cancels this task.
            pointer.close()
            transport.close()
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve(channel, port, token):
    # Each source directory is one ephemeral live desktop, including its
    # independent native binding and input acknowledgement sequence.
    finished = asyncio.Event()
    claimed = False
    @asynccontextmanager
    async def source(**options):
        nonlocal claimed
        if claimed:
            raise SurfaceError('the private application session has ended')
        try:
            async with interactive_fifos(channel, **options) as endpoint:
                claimed = True
                yield endpoint
        finally:
            if claimed:
                # The serving task may now cancel authenticated sockets. Release
                # the source FIFOs before allowing it to initiate that shutdown.
                finished.set()
    async def capabilities():
        deadline = asyncio.get_running_loop().time() + 45
        while not (channel/'session.json').is_file():
            if asyncio.get_running_loop().time() >= deadline:
                raise SurfaceError('application source did not initialize')
            await asyncio.sleep(.05)
        return supported_editor_features(channel) | APP_WALLPAPER | APP_PICTURE_DELTA | APP_WALLPAPER_X99

    service = DesktopService(token, source, features=capabilities)
    closing = False
    async def accept(reader, writer):
        # A socket accepted just before close() can enter its callback later.
        if closing:
            writer.close()
            return
        await service.accept(reader, writer)

    server = await asyncio.start_server(accept, '127.0.0.1', port, limit=1024, backlog=4)
    try:
        print(f'原厂应用私有桌面端口 {server.sockets[0].getsockname()[1]}。', flush=True)
        await finished.wait()
    finally:
        # Python 3.12 waits for every accepted connection in wait_closed().
        # Cancel pending logins and the source connection before awaiting it.
        closing = True
        server.close()
        await service.close()
        await server.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('channel', type=Path)
    parser.add_argument('--port', type=int, default=17326)
    args = parser.parse_args()
    token = os.environ.pop('CLOUDWORKSPACE_GNOME_TOKEN', '')
    if not TOKEN.fullmatch(token):
        parser.error('请设置 43 位随机 CLOUDWORKSPACE_GNOME_TOKEN。')
    if not 0 < args.port <= 65535:
        parser.error('无效的私有端口。')
    try:
        asyncio.run(serve(args.channel, args.port, token))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
