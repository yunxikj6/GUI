"""Small, fixed input vocabulary for an 800×520 original GNOME surface."""
import struct

WIDTH, HEIGHT = 800, 520
_COORDS = struct.Struct('!HH')
KEYS = ('Tab', 'Enter', 'Escape', 'Left', 'Right', 'Up', 'Down', 'Backspace',
        'Delete', 'Home', 'End', 'PageUp', 'PageDown', 'Shift+Tab', 'Space')
# xdotool accepts X11 keysyms; do not parse shortcuts supplied by a remote peer.
_KEYSYMS = dict(zip(KEYS, ('Tab', 'Return', 'Escape', 'Left', 'Right', 'Up', 'Down',
                           'BackSpace', 'Delete', 'Home', 'End', 'Prior', 'Next',
                           'shift+Tab', 'space')))
_SIZES = {b'S': 1, b'K': 2, b'M': 5, b'L': 5, b'R': 5}


def input_size(op):
    try:
        return _SIZES[op]
    except (KeyError, TypeError) as exc:
        raise ValueError('不支持的 GNOME 输入。') from exc


def encode_pointer(op, x, y):
    if (op not in (b'M', b'L', b'R') or type(x) is not int or type(y) is not int
            or not (0 <= x < WIDTH and 0 <= y < HEIGHT)):
        raise ValueError('GNOME 指针坐标或按钮无效。')
    return op + _COORDS.pack(x, y)


def encode_key(key):
    if key not in KEYS:
        raise ValueError('不支持的 GNOME 导航键。')
    return b'K' + bytes([KEYS.index(key) + 1])


def decode_input(command):
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
    encode_pointer(op, x, y)
    return op, (x, y)
