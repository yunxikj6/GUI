"""Copy an owned GNOME Shell's Mesa draw commands to an independent virgl renderer.

Use dedicated, private virgl_test_server processes for capture and replay. This
experiment accepts only Mesa's original inline-resource vtest dialect. A replay
uses commands and uploaded resources alone. Readbacks never enter the recording.
"""
from __future__ import annotations

import os
import socket
import struct

_HEADER = struct.Struct('<II')
_TRANSFER = struct.Struct('<11I')
_RESOURCE = struct.Struct('<10I')
_WORD = struct.Struct('<I')
_MAX_MESSAGE = 32 << 20
_MAX_ARCHIVE = 512 << 20

CAPS, CREATE, UNREF, GET, PUT, SUBMIT, WAIT, RENDERER, CAPS2, PING, VERSION = range(1, 12)
_LENGTHS = {CAPS: 0, CREATE: 10, UNREF: 1, GET: 11, WAIT: 2, CAPS2: 0, PING: 0, VERSION: 1}


class StreamError(ValueError):
    pass


def _read(stream, size, *, allow_eof=False):
    if not 0 <= size <= _MAX_MESSAGE:
        raise StreamError('invalid Mesa message length')
    data = stream.read(size)
    if len(data) != size and not (allow_eof and not data):
        raise StreamError('truncated Mesa message')
    return data


def _request(stream):
    header = _read(stream, _HEADER.size, allow_eof=True)
    if not header:
        return None
    length, command = _HEADER.unpack(header)
    if command in _LENGTHS and length != _LENGTHS[command]:
        raise StreamError('unexpected Mesa command length')
    if command == PUT:
        # The length field rounds to dwords, but the socket sends exact bytes.
        desc = _read(stream, _TRANSFER.size)
        size = _TRANSFER.unpack(desc)[-1]
        if length != 11 + (size + 3) // 4:
            raise StreamError('invalid texture upload length')
        payload = desc + _read(stream, size)
    elif command == RENDERER:
        if not 1 <= length <= 64:
            raise StreamError('invalid renderer name length')
        payload = _read(stream, length)
        if payload.rstrip(b'\0') != b'gnome-shell':
            raise StreamError('expected the GNOME Shell renderer')
    elif command == SUBMIT or command in _LENGTHS:
        payload = _read(stream, length * 4)
    else:
        raise StreamError('unsupported Mesa command')
    if command == VERSION:
        # Negotiate vtest 0 on both sides. Its data plane sends complete texture
        # and buffer uploads instead of file descriptors pointing at host RAM.
        payload = _WORD.pack(0)
    return header, command, payload


def copy_commands(source, gpu, *, recording=None, respond=None, scanout=None, max_bytes=_MAX_ARCHIVE):
    """Pump buffered requests to a dedicated GPU process.

    respond is the live GNOME connection during capture. scanout receives a
    resource descriptor and that local GPU's pixels, for presentation or timing.
    """
    total = 0
    display_targets = {}
    with gpu.makefile('rb') as result:
        while (packet := _request(source)) is not None:
            header, command, payload = packet
            total += len(header) + len(payload)
            if max_bytes is not None and total > max_bytes:
                raise StreamError('session exceeds the capture limit')
            if command == GET and _TRANSFER.unpack(payload)[-1] > _MAX_MESSAGE:
                raise StreamError('readback exceeds the supported size')
            if command == CREATE:
                handle, target, fmt, binds, width, height, depth, array, level, samples = _RESOURCE.unpack(payload)
                # Exact 2D, level-zero BGRX display-target descriptor emitted by
                # the supported Mutter backend. Ignore other offscreen textures.
                display_targets.pop(handle, None)
                if (target, fmt, binds, depth, array, level, samples) == (2, 2, 138, 1, 1, 0, 0):
                    display_targets[handle] = (width, height)
            elif command == UNREF:
                display_targets.pop(_WORD.unpack(payload)[0], None)
            if recording is not None:
                recording.write(header)
                recording.write(payload)
                if command in (GET, SUBMIT):
                    recording.flush()
            gpu.sendall(header + payload)
            if command in (CAPS, CAPS2):
                prefix = _read(result, 8)
                reply = prefix + _read(result, _HEADER.unpack(prefix)[0] - 1)
            elif command == GET:
                desc = _TRANSFER.unpack(payload)
                reply = _read(result, desc[-1])
                if scanout is not None and display_targets.get(desc[0]) == desc[7:9]:
                    scanout(desc, reply)
            elif command in (WAIT, VERSION):
                reply = _read(result, 12)
            elif command == PING:
                reply = _read(result, 8)
            else:
                continue
            if respond is not None:
                respond(reply)


def _connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(os.fspath(path))
        return sock
    except BaseException:
        sock.close()
        raise
