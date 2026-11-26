"""Identify the application-aware stream inside an authenticated GNOME connection."""
import struct

from experiments.application_surface import Binding, SurfaceError
from experiments.mutter_app.controls import ORIGINAL_SHA256

SIZE = (1280, 900)
MAGIC = b'CWORIG1\n'
HELLO = struct.Struct('!8s32s32s')


def source_hello(config):
    nonce = bytes.fromhex(config['session'])
    if len(nonce) != 32 or config['exe'] != ORIGINAL_SHA256:
        raise SurfaceError('unknown original application source')
    return HELLO.pack(MAGIC, nonce, bytes.fromhex(ORIGINAL_SHA256))


def read_hello(stream):
    body = bytearray()
    while len(body) < HELLO.size:
        part = stream.read(HELLO.size - len(body))
        if not part or len(body) + len(part) > HELLO.size:
            raise SurfaceError('incomplete original application handshake')
        body.extend(part)
    magic, nonce, executable = HELLO.unpack(body)
    if magic != MAGIC or executable.hex() != ORIGINAL_SHA256:
        raise SurfaceError('this source has not negotiated original application drawing')
    return nonce, executable


def approved_binding(data):
    return Binding(bytes.fromhex(data['session']), bytes.fromhex(data['exe']), data['pid'],
                   bytes.fromhex(data['instance']), data['surface'], data['handle'], data['creation'])
