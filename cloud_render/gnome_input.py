"""Small, fixed input vocabulary for a GNOME surface."""
import struct

DEFAULT_SIZE = (800, 520)
SUPPORTED_SIZES = (DEFAULT_SIZE, (1280, 900), (1920, 1080))
SIZE_NAMES = {f'{w}x{h}': (w, h) for w, h in SUPPORTED_SIZES}
WIDTH, HEIGHT = DEFAULT_SIZE


TEXT_HEAD = struct.Struct('!BIH')
MAX_TEXT = 4096


def command_size(data):
    if data[:1] != b'T':
        return input_size(bytes(data[:1]))
    if len(data) < TEXT_HEAD.size:
        return None
    _, revision, length = TEXT_HEAD.unpack_from(data)
    if not revision or not 1 <= length <= MAX_TEXT:
        raise ValueError('GNOME 文字提交头无效。')
    return TEXT_HEAD.size + length


def encode_text(revision, text):
    if (type(revision) is not int or not 0 < revision <= 0xffffffff or type(text) is not str
            or not 0 < len(text) <= MAX_TEXT
            or any(ord(c) < 32 or 127 <= ord(c) < 160 or c in '\u2028\u2029' for c in text)):
        raise ValueError('GNOME 搜索文本或焦点编号无效。')
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('GNOME 搜索文本编码无效。') from None
    if len(data) > MAX_TEXT:
        raise ValueError('GNOME 搜索文本过长。')
    return TEXT_HEAD.pack(ord('T'), revision, len(data)) + data


_COORDS = struct.Struct('!HH')
KEYS = ('Tab', 'Enter', 'Escape', 'Left', 'Right', 'Up', 'Down', 'Backspace',
        'Delete', 'Home', 'End', 'PageUp', 'PageDown', 'Shift+Tab', 'Space')
# xdotool accepts X11 keysyms; do not parse shortcuts supplied by a remote peer.
_KEYSYMS = dict(zip(KEYS, ('Tab', 'Return', 'Escape', 'Left', 'Right', 'Up', 'Down',
                           'BackSpace', 'Delete', 'Home', 'End', 'Prior', 'Next',
                           'shift+Tab', 'space')))
_SIZES = {b'S': 1, b'A': 5, b'B': 5, b'D': 5, b'K': 2, b'M': 5, b'L': 5, b'R': 5}


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


def _focused_command(op, revision):
    if type(revision) is not int or not 0 < revision <= 0xffffffff:
        raise ValueError('GNOME 编辑操作的焦点编号无效。')
    return op + struct.pack('!I', revision)


def encode_select_all(revision):
    return _focused_command(b'A', revision)


def encode_delete(revision, *, backward):
    if type(backward) is not bool:
        raise ValueError('GNOME 删除方向无效。')
    return _focused_command(b'B' if backward else b'D', revision)


def encode_key(key):
    if key not in KEYS:
        raise ValueError('不支持的 GNOME 导航键。')
    return b'K' + bytes([KEYS.index(key) + 1])


def decode_input(command, *, size=DEFAULT_SIZE):
    """Validate exactly one command, returning (opcode, positional arguments)."""
    if type(command) is not bytes or len(command) != command_size(command):
        raise ValueError('GNOME 输入长度无效。')
    op = command[:1]
    if op in (b'A', b'B', b'D'):
        revision, = struct.unpack_from('!I', command, 1)
        _focused_command(op, revision)
        return op, (revision,)
    if op == b'T':
        _, revision, _ = TEXT_HEAD.unpack_from(command)
        try:
            text = command[TEXT_HEAD.size:].decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('GNOME 搜索文本编码无效。') from None
        encode_text(revision, text)
        return op, (revision, text)
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
