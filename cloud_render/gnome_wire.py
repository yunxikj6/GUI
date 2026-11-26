"""Private stream of original Mesa requests for one GNOME source and one renderer."""
import asyncio
import hashlib
import struct
import zlib


MAGIC = b'GVGL'
AUTH = b'GNOME-AUTH/1 '
AUTH_SIZE = b'GNOME-AUTH/2 '
QUERY = b'GNOME-CAPS/1 '
FEATURES = struct.Struct('!4sI')
FEATURE_MAGIC = b'GCFG'
WALLPAPER = 1
SEARCH_INPUT = 2
SELECT_ALL = 4
FOCUS_STATE = struct.Struct('!IB')
HEADER = struct.Struct('!4sBBHI')
BLOCK, INPUT, ACK, READY, END, UNAVAILABLE, FOCUS = range(1, 8)
UNAVAILABLE_APPS = ('chatgpt', 'claude', 'wayland')
BLOCK_HEAD = struct.Struct('!II32s32s')
INPUT_ACK = struct.Struct('!IB')
DIMENSIONS = struct.Struct('!HH')
# Authenticated v2 setup. Flags advertise optional commands the viewer can decode.
# A server may always send the original commands and ignore unknown offers.
SESSION = struct.Struct('!HHI')
FINAL = struct.Struct('!IQ32s')
SEQUENCE = struct.Struct('!I')
MAX_RAW = 1024 * 1024
MAX_PACKET = BLOCK_HEAD.size + MAX_RAW + 4096
ZERO = bytes(32)


class DesktopError(ValueError):
    pass


def packet(kind, payload):
    return HEADER.pack(MAGIC, 1, kind, 0, len(payload)) + payload


def payload_size(header, allowed):
    magic, version, kind, flags, size = HEADER.unpack(header)
    if magic != MAGIC or version != 1 or flags or kind not in allowed:
        raise DesktopError('无效的桌面消息方向或版本。')
    if kind == BLOCK:
        valid = BLOCK_HEAD.size < size <= MAX_PACKET
    elif kind == INPUT:
        valid = 5 <= size <= 4 + 7 + 4096
    else:
        valid = size == {ACK: INPUT_ACK.size, READY: DIMENSIONS.size, END: FINAL.size, UNAVAILABLE: 1, FOCUS: FOCUS_STATE.size}[kind]
    if not valid:
        raise DesktopError('无效的桌面消息长度。')
    return kind, size


def read(stream, allowed):
    head = stream.read(HEADER.size)
    if not head:
        return None
    if len(head) != HEADER.size:
        raise DesktopError('桌面消息头不完整。')
    kind, size = payload_size(head, allowed)
    body = stream.read(size)
    if len(body) != size:
        raise DesktopError('桌面消息不完整。')
    return kind, body


async def read_async(reader, allowed):
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise DesktopError('桌面消息头不完整。') from exc
    kind, size = payload_size(header, allowed)
    return kind, await reader.readexactly(size)


def _next(previous, index, length, digest):
    return hashlib.sha256(previous + struct.pack('!II', index, length) + digest).digest()


def pack_block(raw, index, previous):
    if not 0 < len(raw) <= MAX_RAW or not 1 <= index <= 0xffffffff:
        raise DesktopError('桌面绘制块超出上限。')
    digest = hashlib.sha256(raw).digest()
    body = BLOCK_HEAD.pack(index, len(raw), previous, digest) + zlib.compress(raw, 3)
    return packet(BLOCK, body), _next(previous, index, len(raw), digest)


def unpack_block(body, index, previous):
    if not BLOCK_HEAD.size < len(body) <= MAX_PACKET:
        raise DesktopError('桌面绘制块长度无效。')
    number, length, source_base, digest = BLOCK_HEAD.unpack_from(body)
    if number != index or source_base != previous or not 0 < length <= MAX_RAW:
        raise DesktopError('桌面绘制指令的顺序不一致。')
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(memoryview(body)[BLOCK_HEAD.size:], length + 1)
    except zlib.error as exc:
        raise DesktopError('桌面绘制块解压失败。') from exc
    if (len(raw) != length or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail or
            hashlib.sha256(raw).digest() != digest):
        raise DesktopError('桌面绘制指令校验失败。')
    return raw, _next(previous, index, length, digest)
