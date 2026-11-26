"""Lossless InstaBox frame transport. The dictionary is the previous frame of this session."""
import hashlib
import re
import struct
import zlib


MAX_FRAME = 32 * 1024 * 1024 + 12
_HEADER = struct.Struct('!4sIII32s32s')
_LITERAL = struct.Struct('!BI')
_COPY = struct.Struct('!BII')
_EMPTY_BASE = bytes(32)
_MIN_CHUNK = 2048
_MAX_CHUNK = 16384
# Content-defined boundaries survive insertions before repeated original UI assets.
_ANCHOR = re.compile(b'[\x01\x16\x41\x89][\x00\x02\x3b\x91]')


class DeltaError(ValueError):
    """Never deliver the partial result of a damaged or unresolvable transfer."""


def _chunks(raw):
    start = 0
    while start < len(raw):
        end = min(len(raw), start + _MAX_CHUNK)
        anchor = _ANCHOR.search(raw, start + _MIN_CHUNK, end)
        if anchor:
            end = anchor.end()
        yield start, end
        start = end


def _digest(data):
    return hashlib.sha256(data).digest()


def encode_frame(current, previous=b''):
    """Return a self-identifying packet or the smaller original CLRP frame."""
    if not 0 < len(current) <= MAX_FRAME or len(previous) > MAX_FRAME:
        raise DeltaError('invalid frame size')
    old = memoryview(previous)
    index = {}
    for start, end in _chunks(previous):
        index.setdefault(_digest(old[start:end]), (1, start, end - start))
    data = memoryview(current)
    patch = bytearray()
    literal = 0
    for start, end in _chunks(current):
        chunk = data[start:end]
        if len(chunk) < 32:
            continue
        fingerprint = _digest(chunk)
        match = index.get(fingerprint)
        if not match:
            # Identical fonts can occur more than once even in the first SKP.
            index[fingerprint] = (2, start, len(chunk))
            continue
        op, offset, size = match
        source = old if op == 1 else data
        if len(chunk) != size or chunk != source[offset:offset + size]:
            continue
        if start > literal:
            patch.extend(_LITERAL.pack(0, start - literal))
            patch.extend(data[literal:start])
        patch.extend(_COPY.pack(op, offset, size))
        literal = end
    if literal < len(data):
        patch.extend(_LITERAL.pack(0, len(data) - literal))
        patch.extend(data[literal:])
    compact = zlib.compress(patch, 3)
    if len(compact) + _HEADER.size >= len(current):
        return current
    base_hash = _digest(previous) if previous else _EMPTY_BASE
    header = _HEADER.pack(b'CDZ1', len(data), len(patch), len(compact), base_hash, _digest(current))
    return header + compact


def decode_frame(packet, previous=b''):
    if not _HEADER.size < len(packet) <= MAX_FRAME or len(previous) > MAX_FRAME:
        raise DeltaError('invalid packet size')
    magic, target, patch_size, compressed_size, base_hash, full_hash = _HEADER.unpack_from(packet)
    if (magic != b'CDZ1' or not 0 < target <= MAX_FRAME or
            not 0 < patch_size <= target + _LITERAL.size or compressed_size != len(packet) - _HEADER.size):
        raise DeltaError('invalid packet header')
    if base_hash != (_digest(previous) if previous else _EMPTY_BASE):
        raise DeltaError('missing previous frame')
    inflater = zlib.decompressobj()
    try:
        patch = inflater.decompress(memoryview(packet)[_HEADER.size:], patch_size + 1)
    except zlib.error as exc:
        raise DeltaError('invalid compressed stream') from exc
    if len(patch) != patch_size or not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
        raise DeltaError('invalid expanded size')
    patch = memoryview(patch)
    base = memoryview(previous)
    pos = instructions = 0
    max_instructions = 2 * ((target + _MIN_CHUNK - 1) // _MIN_CHUNK) + 1
    result = bytearray()
    while pos < len(patch):
        instructions += 1
        if len(patch) - pos < _LITERAL.size or instructions > max_instructions:
            raise DeltaError('truncated or excessive transfer instructions')
        op, number = _LITERAL.unpack_from(patch, pos)
        pos += _LITERAL.size
        if op == 0:
            if not 0 < number <= min(target - len(result), len(patch) - pos):
                raise DeltaError('invalid literal bounds')
            result.extend(patch[pos:pos + number])
            pos += number
        elif op in (1, 2):
            if len(patch) - pos < 4:
                raise DeltaError('truncated reference')
            size, = struct.unpack_from('!I', patch, pos)
            pos += 4
            source = base if op == 1 else result
            if not 0 < size <= min(len(source) - number, target - len(result), _MAX_CHUNK):
                raise DeltaError('invalid reference bounds')
            # For self-references this slice is a bounded, nonoverlapping copy.
            result.extend(source[number:number + size])
        else:
            raise DeltaError('unknown transfer instruction')
    if len(result) != target or _digest(result) != full_hash:
        raise DeltaError('frame integrity check failed')
    return bytes(result)
