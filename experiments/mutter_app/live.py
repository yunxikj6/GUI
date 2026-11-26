"""Forward a live, actively witnessed original compositor stream on private FIFOs."""

import argparse
import hashlib
import json
import os
import queue
import secrets
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image

from experiments.application_surface import Binding
from experiments.live_application_surface import DESKTOP, WitnessedPut, stream_witnessed

from .capture import business_digest, publish
from .controls import ORIGINAL_SHA256, require


def wait_for(fn, message, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise RuntimeError(message)


def initialize(channel):
    channel.mkdir(mode=0o700)
    for name in ('graphics.fifo', 'input.fifo'):
        os.mkfifo(channel / name, 0o600)
    (channel / 'focus.events').touch(mode=0o600, exist_ok=False)
    session = secrets.token_hex(32)
    publish(channel / 'editor-capabilities.json', {'session': session, 'flags': 2 | 4 | 8})
    # The bridge watches this final rename before negotiating source capabilities.
    publish(channel / 'session.json', {'session': session, 'exe': ORIGINAL_SHA256})


class LiveInput:
    """Drain the original renderer even while the private endpoint is pairing or proving a commit."""

    def __init__(self, incoming):
        self.blocks = queue.Queue(maxsize=2048)  # up to 256 MiB, with backpressure
        self.pending = bytearray()
        self.finished = False

        def pump():
            try:
                while block := os.read(incoming.fileno(), 128 << 10):
                    self.blocks.put(block)
            except OSError as exc:
                self.blocks.put(exc)
            finally:
                self.blocks.put(None)

        threading.Thread(target=pump, daemon=True).start()

    def read(self, size):
        while len(self.pending) < size and not self.finished:
            block = self.blocks.get(timeout=180)
            if isinstance(block, Exception):
                raise block
            if block is None:
                self.finished = True
            else:
                self.pending.extend(block)
        out = bytes(self.pending[:size])
        del self.pending[:size]
        return out

    def drain_disconnected(self, timeout):
        # The original compositor still uses its GPU until the session owner
        # stops it. Keep its upstream reader alive after the viewer has left.
        self.pending.clear()
        deadline = time.monotonic() + timeout
        while not self.finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('original compositor did not stop after disconnect')
            try:
                block = self.blocks.get(timeout=remaining)
            except queue.Empty:
                raise RuntimeError('original compositor did not stop after disconnect') from None
            if isinstance(block, Exception):
                raise block
            self.finished = block is None


class Journal:
    """Match explicit socket-offset classifications and completed Mutter SHM/GL scopes."""

    def __init__(self, path):
        self.path = path
        self.file = None
        self.position = 0
        self.fragment = b''
        self.origins = {}
        self.peers = {}
        self.commits = set()
        self.roles = {}
        self.tops = {}
        self.ids = {}
        self.cursors = set()
        self.access = None

    def update(self):
        if self.file is None:
            if not self.path.is_file():
                return
            self.file = self.path.open('rb')
        while chunk := self.file.read(64 << 10):
            lines = (self.fragment + chunk).split(b'\n')
            self.fragment = lines.pop()
            require(len(self.fragment) < 512, 'invalid partial source witness')
            for line in lines:
                require(0 < len(line) < 512, 'invalid source witness record')
                self.position += len(line) + 1
                require(self.position <= 16 << 20, 'live witness exceeds source limit')
                self.record(line)

    def record(self, raw):
        fields = raw.decode('ascii').split()
        name = fields.pop(0)
        if name == 'XDG_SURFACE':
            client, pid, xdg, surface = map(int, fields)
            self.roles[client, xdg] = (pid, surface)
        elif name == 'TOPLEVEL':
            client, xdg, top = map(int, fields)
            self.tops[client, top] = xdg
        elif name == 'APP_ID':
            client, top, label = fields
            self.ids[int(client), int(top)] = label
        elif name == 'CURSOR':
            self.cursors.add(tuple(map(int, fields)))
        elif name == 'PEER':
            *peer, serial = map(int, fields)
            require(serial not in self.peers, 'duplicate original attach')
            self.peers[serial] = tuple(peer)
        elif name == 'COMMIT':
            self.commits.add(tuple(map(int, fields)))
        elif name == 'ORIGIN':
            offset, kind, *desc = fields
            require(int(offset) not in self.origins, 'duplicate source offset')
            self.origins[int(offset)] = (kind, list(map(int, desc)), None)
        elif name == 'ACCESS':
            require(self.access is None, 'overlapping native GL observation')
            self.access = {'serial': int(fields[3]), 'writes': []}
        elif name in ('WIRE', 'SHM', 'COGL_REGION', 'COGL_CREATE', 'GL', 'END'):
            access = self.access
            require(access is not None and access['serial'] == int(fields[0]), 'unexpected native access')
            if name == 'SHM':
                access['shm'] = (int(fields[1]), int(fields[2]), fields[3])
            elif name in ('COGL_REGION', 'COGL_CREATE'):
                access['cogl'] = fields[1]
            elif name == 'GL':
                access['gl'] = (int(fields[1]), int(fields[2]))
            elif name == 'WIRE':
                access['writes'].append((int(fields[1]), list(map(int, fields[2:]))))
            else:
                for offset, desc in access.pop('writes'):
                    require(offset not in self.origins, 'duplicate native transfer')
                    self.origins[offset] = ('SHM', desc, dict(access, witnessByte=self.position))
                self.access = None

    def at(self, offset):
        self.update()
        return self.origins.pop(offset, None)


class Inspector:
    def __init__(self, out, channel, *, interactive=False):
        self.interactive = interactive
        self.out = out
        self.channel = channel
        self.session = bytes.fromhex(json.loads((channel / 'session.json').read_text())['session'])
        self.journal = Journal(out / 'owner.log')
        self.binding = None
        self.image = Image.new('RGBA', (1090, 760))
        self.pictures = {}
        self.phase = None
        self.revision = 0
        self.last_picture = None
        self.cached_asset = None
        self.requested = None
        self.request_time = 0.0
        self.counts = {'shell': 0, 'cursor': 0, 'unapproved': 0}
        self.counts.update({'approved': 0} if interactive else {'before': 0, 'after': 0})

    def state(self, name):
        record = wait_for(
            lambda: (self.out / (name + '.json')).is_file(), 'no authenticated ' + name + ' drawing', 120
        )
        if record:
            state = json.loads((self.out / (name + '.json')).read_text())
            self.pictures[bytes.fromhex(state['pictureSha256'])] = self.out / (name + '.clrp')
            self.phase = (name, state)

    def asset(self, digest, limit):
        if self.interactive:
            raw = self.cached_asset
            require(raw is not None and len(raw) <= limit, 'invalid active original drawing')
            return raw
        path = self.pictures[digest]
        require(path.stat().st_size <= limit, 'live picture too large')
        return path.read_bytes()

    def manual_scene(self, source_digest, journal_byte):
        """Use the original drawing only if it matches this physical application commit."""
        scenes = self.out / 'scenes.json'
        approvals = json.loads(scenes.read_text()) if scenes.is_file() else {}
        if source_digest not in approvals:
            now = time.monotonic()
            if self.requested != source_digest or now - self.request_time > 2:
                self.requested, self.request_time = source_digest, now
                publish(
                    self.out / 'requested-scene.json',
                    {'businessRgbSha256': source_digest, 'witnessByte': journal_byte},
                )
            if not scenes.exists():
                wait_for(scenes.is_file, 'original application did not supply its first drawing', 75)
            pending = self.out / 'pending-scene.json'
            in_progress = json.loads(pending.read_text()) if pending.is_file() else {}
            # A matching drawing can only authorize a commit at or beyond its start.
            # Waiting on an earlier PUT would also delay every later compositor command.
            start = in_progress.get('witnessByteStart')
            if (
                in_progress.get('businessRgbSha256') == source_digest
                and type(start) is int
                and 0 <= start <= journal_byte
            ):
                until = time.monotonic() + 1.5
                while time.monotonic() < until and source_digest not in approvals:
                    time.sleep(0.03)
                    approvals = json.loads(scenes.read_text())
            else:
                approvals = json.loads(scenes.read_text())
        state = approvals.get(source_digest)
        if state is None or journal_byte < state['witnessByteStart']:
            return None, None
        digest = bytes.fromhex(state['pictureSha256'])
        if digest != self.last_picture:
            try:
                asset = self.out / 'drawings' / (digest.hex() + '.clrp')
                with asset.open('rb') as stream:
                    require(
                        os.fstat(stream.fileno()).st_size == state['bytes'] <= 32 * 1024**2 + 12,
                        'invalid source drawing size',
                    )
                    self.cached_asset = stream.read(state['bytes'])
            except FileNotFoundError:
                # It may have been evicted after reading the atomically published index.
                return None, None
            require(
                len(self.cached_asset) == state['bytes']
                and hashlib.sha256(self.cached_asset).digest() == digest,
                'invalid original recording in source cache',
            )
        return 'approved', digest

    def inspect(self, offset, ordinal, desc, creation, pixels):
        origin = wait_for(lambda: self.journal.at(offset), 'Mutter did not classify the exact outgoing PUT')
        kind, record, access = origin
        handle, level, stride, layer_stride, x, y, z, w, h, depth, size = desc
        require(record == [handle, x, y, w, h, size], 'source provided a different Mesa descriptor')
        if kind == 'SHELL':
            self.counts['shell'] += 1
            return DESKTOP
        require(kind == 'SHM', 'a foreign client SHM transfer lacks an authorized drawing route')
        serial = access['serial']
        client, pid, ticks, device, inode, surface, buffer_id = self.journal.peers[serial]
        require(buffer_id > 0, 'unknown Wayland buffer')
        require(
            (client, pid, surface, serial) in self.journal.commits
            and access.get('cogl')
            and access.get('gl', (None, None))[1] == 3553
            and 'shm' in access,
            'incomplete active SHM ownership',
        )
        wait_for(
            lambda: (self.out / 'identity.json').is_file(), 'original private application pipe unavailable'
        )
        who = json.loads((self.out / 'identity.json').read_text())
        require(
            who['binarySha256'] == ORIGINAL_SHA256
            and who['target'] == 'app://-/index.html'
            and (pid, ticks, device, inode) == (who['pid'], who['startTicks'], who['device'], who['inode']),
            'private process and Wayland peer do not match',
        )
        if (client, surface) in self.journal.cursors:
            require(w <= 128 and h <= 128 and size <= 128 * 128 * 4, 'unrecognized native cursor')
            self.counts['cursor'] += 1
            return DESKTOP
        mains = [
            self.journal.roles[c, self.journal.tops[c, t]]
            for (c, t), app in self.journal.ids.items()
            if c == client and app == 'chatgpt'
        ]
        require(mains == [(pid, surface)], 'ambiguous original window or popup')
        require(
            (level, z, depth, stride) == (0, 0, 1, 4360)
            and layer_stride in (0, size)
            and x + w <= 1090
            and y + h <= 760
            and 0 < w <= 1090
            and 0 < h <= 760
            and stride * (h - 1) + w * 4 <= len(pixels) == size <= stride * h,
            'unsupported application texture',
        )
        if self.binding is None:
            require((x, y, w, h) == (0, 0, 1090, 760), 'first live transfer is partial')
            instance = hashlib.sha256(
                self.session + struct.pack('!QQQQQ', pid, ticks, device, inode, client)
            ).digest()
            self.binding = Binding(
                self.session, bytes.fromhex(ORIGINAL_SHA256), pid, instance, surface, handle, creation
            )
            self.gl = access['gl'][0]
            publish(
                self.channel / 'binding.json',
                {
                    'session': self.session.hex(),
                    'exe': ORIGINAL_SHA256,
                    'pid': pid,
                    'instance': instance.hex(),
                    'surface': surface,
                    'handle': handle,
                    'creation': creation,
                },
            )
        require(
            (self.binding.handle, self.binding.creation, self.gl) == (handle, creation, access['gl'][0]),
            'original graphics resource changed',
        )
        self.image.paste(Image.frombytes('RGBA', (w, h), bytes(pixels), 'raw', 'RGBA', stride), (x, y))
        require(
            access['shm'] == (4360, 760, hashlib.sha256(self.image.tobytes('raw', 'BGRA')).hexdigest()),
            'source SHM and observed Mesa data differ',
        )
        native_hash = hashlib.sha256(
            self.image.crop((1026, 0, 1090, 36)).convert('RGB').tobytes()
        ).hexdigest()
        name = digest = None
        if native_hash == '1d33223cedfb68917cbf807cd1664b20a46466ad61093dd292c6d0da70aec0a7':
            source_digest = business_digest(self.image)
            if self.interactive:
                name, digest = self.manual_scene(source_digest, access['witnessByte'])
            else:
                if self.phase is None:
                    self.state('before')
                elif self.phase[0] == 'before' and (self.out / 'input-applied.json').is_file():
                    self.state('after')
                phase, state = self.phase
                if (
                    access['witnessByte'] >= state['witnessByteStart']
                    and source_digest == state['businessRgbSha256']
                ):
                    name, digest = phase, bytes.fromhex(state['pictureSha256'])
        self.counts[name or 'unapproved'] += 1
        if digest is not None:
            receipt = {'offset': offset, 'ordinal': ordinal, 'serial': serial}
            if self.interactive:
                if digest != self.last_picture:
                    self.revision += 1
                    self.last_picture = digest
                receipt.update(revision=self.revision, pictureSha256=digest.hex())
                publish(self.out / 'presented-scene.json', receipt)
            else:
                publish(self.out / ('forwarded-' + name + '.json'), receipt)
        return WitnessedPut(self.binding, serial, digest)


def stream(out, channel, *, interactive=False):
    reader = LiveInput(sys.stdin.buffer)
    inspector = Inspector(out, channel, interactive=interactive)
    require(stat.S_ISFIFO((channel / 'graphics.fifo').lstat().st_mode), 'private graphics FIFO required')
    try:
        with (channel / 'graphics.fifo').open('wb') as sink:
            result = stream_witnessed(
                reader, sink, inspect_put=inspector.inspect, fetch_picture=inspector.asset
            )
    except BrokenPipeError:
        if not interactive:
            raise
        wait_for((out / 'controller-closed').is_file, 'graphics connection lost while input was open', 2)
        reader.drain_disconnected(30)
        result = {'controllerClosed': True, 'lastRevision': inspector.revision}
    publish(out / 'live-source.json', dict(result, classification=inspector.counts))


def pointer_command(event, xid):
    require(len(event) == 5 and event[:1] in (b'L', b'M'), 'expected a pointer event')
    x, y = struct.unpack('!HH', event[1:])
    require(x < 1280 and y < 900, 'pointer is outside the real cloud desktop')
    command = ['xdotool', 'mousemove', '--window', xid, str(x), str(y)]
    if event[:1] == b'L':
        command += ['click', '1']
    return command, x, y


def receive_click(out, channel, *, interactive=False):
    require(stat.S_ISFIFO((channel / 'input.fifo').lstat().st_mode), 'private input FIFO required')
    xid = (out / 'nested-window').read_text().strip()
    with (channel / 'input.fifo').open('rb') as src:
        sequence = 0
        while op := src.read(1):
            if op in (b'L', b'M'):
                event = op + src.read(4)
                command, x, y = pointer_command(event, xid)
            else:
                require(interactive, 'proof mode expects just a pointer click')
                from cloud_render.gnome_input import command_size, decode_input

                header = op + (src.read(6) if op == b'T' else b'')
                size = command_size(header)
                require(size is not None, 'incomplete editor command')
                event = header + src.read(size - len(header))
                opcode, args = decode_input(event, size=(1280, 900))
                if opcode in (b'T', b'A', b'B', b'D') or (
                    opcode == b'K' and args in (('BackSpace',), ('Delete',))
                ):
                    from .editor import deliver

                    applied = opcode != b'K' and deliver(out, event)
                    sequence += 1
                    publish(channel / 'last-input.json', {'sequence': sequence, 'applied': applied})
                    continue
                require(opcode == b'K', 'unsupported application navigation')
                command = ['xdotool', 'key', '--clearmodifiers', args[0]]
            require(
                interactive or event[:1] == b'L' and (out / 'forwarded-before.json').is_file(),
                'expected a single pointer click on the presented original page',
            )
            subprocess.run(command, timeout=4, check=True)
            sequence += 1
            publish(channel / 'last-input.json', {'sequence': sequence, 'applied': True})
            if not interactive:
                publish(out / 'input-applied.json', {'x': x, 'y': y, 'button': 1})
                return
    if interactive:
        (out / 'controller-closed').touch()
    else:
        raise RuntimeError('controller closed without clicking the original window')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['open', 'stream', 'input'])
    parser.add_argument('channel', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    if args.action == 'open':
        initialize(args.channel)
    else:
        require(args.source is not None, 'source directory required')
        (stream if args.action == 'stream' else receive_click)(
            args.source, args.channel, interactive=args.interactive
        )
