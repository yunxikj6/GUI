"""Small, fixed input vocabulary for a GNOME surface."""
import struct

DEFAULT_SIZE = (800, 520)
SUPPORTED_SIZES = (DEFAULT_SIZE, (1280, 900), (1920, 1080))
SIZE_NAMES = {f'{w}x{h}': (w, h) for w, h in SUPPORTED_SIZES}
WIDTH, HEIGHT = DEFAULT_SIZE


_COORDS = struct.Struct('!HH')
KEYS = ('Tab', 'Enter', 'Escape', 'Left', 'Right', 'Up', 'Down', 'Backspace',
        'Delete', 'Home', 'End', 'PageUp', 'PageDown', 'Shift+Tab', 'Space')
# xdotool accepts X11 keysyms; do not parse shortcuts supplied by a remote peer.
_KEYSYMS = dict(zip(KEYS, ('Tab', 'Return', 'Escape', 'Left', 'Right', 'Up', 'Down',
                           'BackSpace', 'Delete', 'Home', 'End', 'Prior', 'Next',
                           'shift+Tab', 'space')))
_SIZES = {b'S': 1, b'K': 2, b'M': 5, b'L': 5, b'R': 5}


def checked_size(size):
    if (type(size) is not tuple or len(size) != 2 or
            any(type(v) is not int for v in size) or size not in SUPPORTED_SIZES):
        raise ValueError('不支持的 GNOME 桌面尺寸。')
    return size


def input_size(op):
    try:
        return _SIZES[op]
    except (KeyError, TypeError) as exc:
        raise ValueError('不支持的 GNOME 输入。') from exc


def encode_pointer(op, x, y, *, size=DEFAULT_SIZE):
    width, height = checked_size(size)
    if (op not in (b'M', b'L', b'R') or type(x) is not int or type(y) is not int
            or not (0 <= x < width and 0 <= y < height)):
        raise ValueError('GNOME 指针坐标或按钮无效。')
    return op + _COORDS.pack(x, y)


def encode_key(key):
    if key not in KEYS:
        raise ValueError('不支持的 GNOME 导航键。')
    return b'K' + bytes([KEYS.index(key) + 1])


def decode_input(command, *, size=DEFAULT_SIZE):
    """Validate exactly one command, returning (opcode, positional arguments)."""
    if type(command) is not bytes or len(command) != input_size(command[:1]):
        raise ValueError('GNOME 输入长度无效。')
    op = command[:1]
    if op == b'S':
        return op, ()
    if op == b'K':
        key_id = command[1]
        if not 1 <= key_id <= len(KEYS):
            raise ValueError('不支持的 GNOME 导航键。')
        return op, (_KEYSYMS[KEYS[key_id - 1]],)
    x, y = _COORDS.unpack(command[1:])
    encode_pointer(op, x, y, size=size)
    return op, (x, y)
