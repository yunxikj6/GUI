"""CLRP v1 for a single rendered surface over an authenticated blocking byte stream.

All envelope and geometry fields use network byte order. SKPs are opaque Skia
byte sequences. Layer rectangles and primary clicks use logical viewport pixels.
Layers are painted back to front. A sequence belongs to one connection and must
advance whenever a new scene is published. End the connection before wraparound.
"""
import asyncio
import io
import math
import struct
from dataclasses import dataclass
from typing import BinaryIO


MAGIC = b"CLRP"
VERSION = 1
FRAME = 1
CLICK = 2
CLICK_ACK = 3
MAX_PAYLOAD = 32 * 1024 * 1024
MAX_PICTURE = 16 * 1024 * 1024
MAX_LAYERS = 128
MAX_VIEWPORT = 8192
MAX_VIEWPORT_AREA = 33554432
SKP_MAGIC = b"skiapict"

_HEADER = struct.Struct("!4sBBHI")  # Magic, version, message kind, reserved, payload bytes.
_SCENE = struct.Struct("!IHHH")    # Sequence, viewport width/height, number of layers.
_LAYER = struct.Struct("!ffffI")   # Display rectangle, length of the following raw SKP.
_CLICK = struct.Struct("!Iff")    # The scene shown at click time, logical x/y.
_CLICK_ACK = struct.Struct("!IB")  # The scene shown at click time, applied (0 or 1).


class WireError(ValueError):
    """Invalid message. A malformed byte stream cannot be resynchronized."""


def _sequence(value):
    if type(value) is not int or not 1 <= value <= 0xffffffff:
        raise WireError("invalid scene sequence")


def _viewport(width, height, count):
    if (type(width) is not int or type(height) is not int or
            not 1 <= width <= MAX_VIEWPORT or not 1 <= height <= MAX_VIEWPORT or
            width * height > MAX_VIEWPORT_AREA or not 1 <= count <= MAX_LAYERS):
        raise WireError("invalid viewport or layer count")


def _number(value, low, high):
    # Test the range before converting unbounded Python integers to a float.
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _geometry(x, y, width, height):
    if (not _number(x, -65536, 65536) or not _number(y, -65536, 65536) or
            not _number(width, 2 ** -149, 32768) or not _number(height, 2 ** -149, 32768)):
        raise WireError("invalid layer rectangle")


@dataclass(frozen=True, slots=True)
class Layer:
    x: float
    y: float
    width: float
    height: float
    skp: bytes

    def __post_init__(self):
        _geometry(self.x, self.y, self.width, self.height)
        if (type(self.skp) is not bytes or len(self.skp) > MAX_PICTURE or
                not self.skp.startswith(SKP_MAGIC)):
            raise WireError("invalid SKP payload")


@dataclass(frozen=True, slots=True)
class Frame:
    sequence: int
    width: int
    height: int
    layers: tuple[Layer, ...]

    def __post_init__(self):
        _sequence(self.sequence)
        if type(self.layers) not in (tuple, list):
            raise WireError("expected a list of layers")
        _viewport(self.width, self.height, len(self.layers))
        object.__setattr__(self, "layers", tuple(self.layers))
        if any(type(layer) is not Layer for layer in self.layers) or self.payload_size > MAX_PAYLOAD:
            raise WireError("invalid frame payload")

    @property
    def payload_size(self):
        return _SCENE.size + sum(_LAYER.size + len(layer.skp) for layer in self.layers)


@dataclass(frozen=True, slots=True)
class Click:
    sequence: int
    x: float
    y: float

    def __post_init__(self):
        _sequence(self.sequence)
        if not _number(self.x, 0, MAX_VIEWPORT) or not _number(self.y, 0, MAX_VIEWPORT):
            raise WireError("invalid click coordinates")


@dataclass(frozen=True, slots=True)
class ClickAck:
    sequence: int
    applied: bool

    def __post_init__(self):
        _sequence(self.sequence)
        if type(self.applied) is not bool:
            raise WireError("invalid click acknowledgement status")


Message = Frame | Click | ClickAck
_TYPES = {FRAME: Frame, CLICK: Click, CLICK_ACK: ClickAck}


def validate_click(click: Click, current_frame: Frame) -> None:
    """Call against the current scene immediately before dispatch on its event loop."""
    if type(click) is not Click or type(current_frame) is not Frame:
        raise WireError("expected a click and its current scene")
    if click.sequence != current_frame.sequence:
        raise WireError("stale scene sequence")
    if click.x >= current_frame.width or click.y >= current_frame.height:
        raise WireError("click is outside the viewport")


def _read(stream, size, *, allow_eof=False):
    data = stream.read(size)
    if len(data) == size or (allow_eof and not data):
        return data
    result = bytearray(data)
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise WireError("truncated message")
        result.extend(part)
    return bytes(result)


def _header(header, expected):
    if expected is not None:
        if not isinstance(expected, tuple):
            expected = (expected,)
        if not expected or any(t not in _TYPES.values() for t in expected):
            raise ValueError("expected must contain known message types")
    magic, version, kind, flags, length = _HEADER.unpack(header)
    if magic != MAGIC or version != VERSION or flags != 0 or length > MAX_PAYLOAD:
        raise WireError("invalid message header")
    if kind not in _TYPES or (expected is not None and _TYPES[kind] not in expected):
        raise WireError("unexpected message type")
    if ((kind == FRAME and length < _SCENE.size) or (kind == CLICK and length != _CLICK.size)
            or (kind == CLICK_ACK and length != _CLICK_ACK.size)):
        raise WireError("unexpected message type or length")
    return kind, length


def _decode(stream, kind, length):
    if kind == CLICK:
        return Click(*_CLICK.unpack(_read(stream, length)))
    if kind == CLICK_ACK:
        sequence, status = _CLICK_ACK.unpack(_read(stream, length))
        if status not in (0, 1):
            raise WireError("invalid click acknowledgement status")
        return ClickAck(sequence, bool(status))
    sequence, width, height, count = _SCENE.unpack(_read(stream, _SCENE.size))
    _sequence(sequence)
    _viewport(width, height, count)
    remaining = length - _SCENE.size
    layers = []
    for _ in range(count):
        if remaining < _LAYER.size:
            raise WireError("missing layer descriptor")
        x, y, w, h, size = _LAYER.unpack(_read(stream, _LAYER.size))
        _geometry(x, y, w, h)
        remaining -= _LAYER.size
        if not len(SKP_MAGIC) <= size <= min(remaining, MAX_PICTURE):
            raise WireError("invalid picture length")
        layers.append(Layer(x, y, w, h, _read(stream, size)))
        remaining -= size
    if remaining:
        raise WireError("unexpected trailing frame bytes")
    return Frame(sequence, width, height, tuple(layers))


def read_message(stream: BinaryIO, expected=None) -> Message | None:
    """Read exactly one message. Return None only on EOF between messages.

    A message class or tuple of classes restricts direction before reading any
    picture bytes. The viewer receives (Frame, ClickAck) and sends Click.
    """
    header = _read(stream, _HEADER.size, allow_eof=True)
    if not header:
        return None
    return _decode(stream, *_header(header, expected))


async def async_read_message(reader: asyncio.StreamReader, expected=None) -> Message | None:
    """Async equivalent, checking the declared direction and size before the body."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as error:
        if not error.partial:
            return None
        raise WireError("truncated message header") from error
    kind, length = _header(header, expected)
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise WireError("truncated message") from error
    return _decode(io.BytesIO(payload), kind, length)


def _write(stream, data):
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:
            raise OSError("stream did not accept data")
        view = view[written:]


def write_message(stream: BinaryIO, message: Message) -> None:
    """Write and flush a complete immutable message. Serialize calls per stream."""
    if type(message) is Click:
        _write(stream, _HEADER.pack(MAGIC, VERSION, CLICK, 0, _CLICK.size))
        _write(stream, _CLICK.pack(message.sequence, message.x, message.y))
    elif type(message) is ClickAck:
        _write(stream, _HEADER.pack(MAGIC, VERSION, CLICK_ACK, 0, _CLICK_ACK.size))
        _write(stream, _CLICK_ACK.pack(message.sequence, message.applied))
    elif type(message) is Frame:
        _write(stream, _HEADER.pack(MAGIC, VERSION, FRAME, 0, message.payload_size))
        _write(stream, _SCENE.pack(message.sequence, message.width, message.height, len(message.layers)))
        for layer in message.layers:
            _write(stream, _LAYER.pack(layer.x, layer.y, layer.width, layer.height, len(layer.skp)))
            _write(stream, layer.skp)
    else:
        raise WireError("unsupported message type")
    stream.flush()


def encode_message(message: Message) -> bytes:
    """Encode an entire envelope for an async writer or an HTTP response."""
    stream = io.BytesIO()
    write_message(stream, message)
    return stream.getvalue()
