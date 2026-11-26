"""Committed Unicode input for a source-confirmed GNOME text focus."""
import ctypes
from functools import lru_cache

from cloud_render.gnome_input import MAX_TEXT, encode_text


@lru_cache(maxsize=1)
def _imm32():
    dll = ctypes.WinDLL('imm32.dll', use_last_error=True, winmode=0x800)
    pointer = ctypes.c_void_p
    dll.ImmGetContext.argtypes, dll.ImmGetContext.restype = [pointer], pointer
    dll.ImmReleaseContext.argtypes, dll.ImmReleaseContext.restype = [pointer, pointer], ctypes.c_int
    dll.ImmGetCompositionStringW.argtypes = [pointer, ctypes.c_uint32, pointer, ctypes.c_uint32]
    dll.ImmGetCompositionStringW.restype = ctypes.c_int32
    return dll


def ime_result(hwnd):
    """Read only the committed result from the window's own input context."""
    imm = _imm32()
    context = imm.ImmGetContext(hwnd)
    if not context:
        return ''
    try:
        gcs_resultstr = 0x800
        size = imm.ImmGetCompositionStringW(context, gcs_resultstr, None, 0)
        if size <= 0 or size > 2 * MAX_TEXT or size % 2:
            return ''
        buf = ctypes.create_string_buffer(size)
        if imm.ImmGetCompositionStringW(context, gcs_resultstr, buf, size) != size:
            return ''
        try:
            return buf.raw.decode('utf-16-le', 'strict')
        except UnicodeDecodeError:
            return ''
    finally:
        imm.ImmReleaseContext(hwnd, context)


class TextInput:
    def __init__(self, focus, submit):
        self.focus = focus
        self.submit = submit
        self.reset()

    def reset(self):
        self.composing = False
        self.revision = None
        self.high = None

    def start(self):
        self.reset()
        self.composing = True
        self.revision = self.focus()

    def end(self):
        # Some system input methods deliver GCS_RESULTSTR immediately after END.
        # Keep the original focus for that result until the next input or blur.
        self.composing = False
        self.high = None

    def next_key(self):
        if not self.composing:
            self.revision = None

    def _send(self, revision, text):
        if not text or revision is None or self.focus() != revision:
            return
        try:
            encode_text(1, text)
        except ValueError:
            return
        self.submit(('text', revision, text))

    def commit(self, text):
        revision, self.revision = self.revision, None
        self._send(revision, text)

    def character(self, unit):
        if self.composing:
            return
        self.next_key()
        revision = self.focus()
        high, self.high = self.high, None
        if not 0 <= unit <= 0xffff or revision is None:
            return
        if 0xd800 <= unit <= 0xdbff:
            self.high = revision, unit
        elif 0xdc00 <= unit <= 0xdfff:
            if high and high[0] == revision:
                self._send(revision, chr(0x10000 + ((high[1] - 0xd800) << 10) + unit - 0xdc00))
        else:
            self._send(revision, chr(unit))
