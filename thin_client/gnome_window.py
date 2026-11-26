"""Win32 presentation for a BGRX8 scanout produced by the local renderer."""
import ctypes as c
import os
import threading
from ctypes import wintypes as w

from .gnome_wintext import TextInput, ime_result

user = c.WinDLL('user32', use_last_error=True)
gdi = c.WinDLL('gdi32', use_last_error=True)
kernel = c.WinDLL('kernel32', use_last_error=True)
LRESULT = c.c_ssize_t
PROC = c.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WndClass(c.Structure):
    _fields_ = [('style', w.UINT), ('proc', PROC), ('class_extra', c.c_int), ('window_extra', c.c_int),
                ('instance', w.HINSTANCE), ('icon', w.HANDLE), ('cursor', w.HANDLE),
                ('brush', w.HBRUSH), ('menu', w.LPCWSTR), ('name', w.LPCWSTR)]


class MonitorInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('monitor', w.RECT), ('work', w.RECT), ('flags', w.DWORD)]


class PaintStruct(c.Structure):
    _fields_ = [('dc', w.HDC), ('erase', w.BOOL), ('rect', w.RECT), ('restore', w.BOOL),
                ('inc_update', w.BOOL), ('reserved', c.c_ubyte * 32)]


class BitmapInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG), ('planes', w.WORD),
                ('bits', w.WORD), ('compression', w.DWORD), ('image_size', w.DWORD),
                ('x_ppm', w.LONG), ('y_ppm', w.LONG), ('used', w.DWORD), ('important', w.DWORD)]


def function(dll, name, result, *args):
    f = getattr(dll, name)
    f.argtypes, f.restype = args, result
    return f


module = function(kernel, 'GetModuleHandleW', w.HMODULE, w.LPCWSTR)
register = function(user, 'RegisterClassW', w.ATOM, c.POINTER(WndClass))
unregister = function(user, 'UnregisterClassW', w.BOOL, w.LPCWSTR, w.HINSTANCE)
create = function(user, 'CreateWindowExW', w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                  c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
default = function(user, 'DefWindowProcW', LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
show = function(user, 'ShowWindow', w.BOOL, w.HWND, c.c_int)
destroy = function(user, 'DestroyWindow', w.BOOL, w.HWND)
get_pointer = function(user, 'GetCursorPos', w.BOOL, c.POINTER(w.POINT))
monitor_from_point = function(user, 'MonitorFromPoint', w.HANDLE, w.POINT, w.DWORD)
monitor_info = function(user, 'GetMonitorInfoW', w.BOOL, w.HANDLE, c.POINTER(MonitorInfo))
adjust = function(user, 'AdjustWindowRect', w.BOOL, c.POINTER(w.RECT), w.DWORD, w.BOOL)
begin = function(user, 'BeginPaint', w.HDC, w.HWND, c.POINTER(PaintStruct))
end = function(user, 'EndPaint', w.BOOL, w.HWND, c.POINTER(PaintStruct))
get_rect = function(user, 'GetClientRect', w.BOOL, w.HWND, c.POINTER(w.RECT))
blit = function(gdi, 'StretchDIBits', c.c_int, w.HDC, *([c.c_int]*8), c.c_void_p,
                c.POINTER(BitmapInfo), w.UINT, w.DWORD)
fill = function(user, 'FillRect', c.c_int, w.HDC, c.POINTER(w.RECT), w.HBRUSH)
stock = function(gdi, 'GetStockObject', w.HGDIOBJ, c.c_int)
invalidate = function(user, 'InvalidateRect', w.BOOL, w.HWND, c.POINTER(w.RECT), w.BOOL)
post = function(user, 'PostMessageW', w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
get_message = function(user, 'GetMessageW', c.c_int, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
peek = function(user, 'PeekMessageW', w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
translate = function(user, 'TranslateMessage', w.BOOL, c.POINTER(w.MSG))
dispatch = function(user, 'DispatchMessageW', LRESULT, c.POINTER(w.MSG))
cursor = function(user, 'LoadCursorW', w.HANDLE, w.HINSTANCE, c.c_void_p)

set_title = function(user, 'SetWindowTextW', w.BOOL, w.HWND, w.LPCWSTR)
get_key_state = function(user, 'GetKeyState', c.c_short, c.c_int)
get_focus = function(user, 'GetFocus', w.HWND)
move_window = function(user, 'SetWindowPos', w.BOOL, w.HWND, w.HWND, *([c.c_int] * 4), w.UINT)
WM_PAINT, WM_CLOSE, WM_DESTROY, WM_SIZE = 15, 16, 2, 5
WM_MOUSEMOVE, WM_LBUTTONUP, WM_RBUTTONUP = 0x200, 0x202, 0x205
WM_KEYDOWN, WM_KEYUP, WM_DPICHANGED = 0x100, 0x101, 0x02e0
WM_KILLFOCUS, WM_CHAR = 0x0008, 0x0102
WM_IME_STARTCOMPOSITION, WM_IME_ENDCOMPOSITION, WM_IME_COMPOSITION = 0x010d, 0x010e, 0x010f
WM_IME_CHAR, GCS_RESULTSTR = 0x0286, 0x0800
VK_CONTROL, VK_SHIFT, VK_MENU = 0x11, 0x10, 0x12
NAVIGATION = {
    0x08: 'Backspace', 0x09: 'Tab', 0x0d: 'Enter', 0x1b: 'Escape',
    0x20: 'Space', 0x21: 'PageUp', 0x22: 'PageDown', 0x23: 'End', 0x24: 'Home',
    0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down', 0x2e: 'Delete',
}


def enable_native_dpi():
    try:
        set_dpi = function(user, 'SetProcessDpiAwarenessContext', w.BOOL, w.HANDLE)
        set_dpi(c.c_void_p(-4))
    except AttributeError:
        pass


def viewport(client_width, client_height, image_width, image_height):
    if client_width <= 0 or client_height <= 0:
        return 0, 0, 0, 0
    scale = min(client_width / image_width, client_height / image_height)
    width = max(1, round(image_width * scale))
    height = max(1, round(image_height * scale))
    return (client_width - width) // 2, (client_height - height) // 2, width, height
PRESENT = 0x8001


def source_coordinate(position, displayed, source):
    # Map the first and last visible pixel to the actual desktop edges, also
    # when a high-resolution source is scaled down to a laptop screen.
    distance = max(1, displayed - 1)
    return (position * (source - 1) + distance // 2) // distance


def initial_bounds(width, height, style):
    # Keep the complete title bar and client area reachable on the monitor
    # where the user is working. Source coordinates always retain their size.
    rect = w.RECT(0, 0, width, height)
    if not adjust(c.byref(rect), style, False):
        raise c.WinError(c.get_last_error())
    frame_w, frame_h = rect.right - rect.left - width, rect.bottom - rect.top - height
    point = w.POINT()
    get_pointer(c.byref(point))
    monitor = monitor_from_point(point, 2)  # MONITOR_DEFAULTTONEAREST
    info = MonitorInfo(size=c.sizeof(MonitorInfo))
    if not monitor or not monitor_info(monitor, c.byref(info)):
        return -2147483648, 0, width + frame_w, height + frame_h  # CW_USEDEFAULT
    area = info.work
    room_w, room_h = area.right - area.left, area.bottom - area.top
    scale = min(1, max(1, room_w - frame_w) / width, max(1, room_h - frame_h) / height)
    win_w = max(1, int(width * scale)) + frame_w
    win_h = max(1, int(height * scale)) + frame_h
    return area.left + (room_w - win_w) // 2, area.top + (room_h - win_h) // 2, win_w, win_h


class NativeWindow:
    """Paint the latest local GPU scanout and return input in cloud coordinates."""

    def __init__(self, title, width, height, on_input=None):
        if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 8388608:
            raise ValueError('invalid scanout dimensions')
        self.width, self.height, self.on_input = width, height, on_input
        self.closed = False
        self.text_focus = lambda: None
        self._text = TextInput(lambda: self.text_focus(), on_input)
        self._space_as_key = False
        self._pixels = None
        self._lock = threading.Lock()
        self._posted = False
        self.paint_count = 0
        self.error = None
        self._owner = threading.get_ident()
        self._info = BitmapInfo(c.sizeof(BitmapInfo), width, -height, 1, 32, 0, width * height * 4)
        self._proc = PROC(self._window_proc)
        self._instance = module(None)
        self._name = f'CloudWorkspaceScanout{os.getpid()}_{id(self)}'
        black = stock(4)
        wc = WndClass(3, self._proc, 0, 0, self._instance, None, cursor(None, 32512), black, None, self._name)
        if not register(c.byref(wc)):
            raise c.WinError(c.get_last_error())
        style = 0x00CF0000
        try:
            bounds = initial_bounds(width, height, style)
            self.hwnd = create(0, self._name, title, style, *bounds, None, None, self._instance, None)
            if not self.hwnd:
                raise c.WinError(c.get_last_error())
        except BaseException:
            unregister(self._name, self._instance)
            raise
        show(self.hwnd, 5)

    def _window_proc(self, hwnd, msg, wp, lp):
        if msg in (WM_KILLFOCUS, WM_DESTROY):
            self._text.reset()
            self._space_as_key = False
        if msg == WM_CLOSE:
            destroy(hwnd)
            return 0
        if msg == WM_DESTROY:
            self.closed = True
            return 0
        if msg in (PRESENT, WM_SIZE):
            if msg == PRESENT:
                with self._lock:
                    self._posted = False
            invalidate(hwnd, None, False)
            return 0
        if msg == WM_PAINT:
            ps = PaintStruct()
            dc = begin(hwnd, c.byref(ps))
            try:
                rect = w.RECT()
                get_rect(hwnd, c.byref(rect))
                fill(dc, c.byref(rect), stock(4))
                with self._lock:
                    frame = self._pixels
                if frame and rect.right and rect.bottom:
                    ox, oy, width, height = viewport(rect.right, rect.bottom, self.width, self.height)
                    if blit(dc, ox, oy, width, height, 0, 0, self.width, self.height,
                            c.c_char_p(frame), c.byref(self._info), 0, 0x00CC0020) in (0, -1):
                        raise c.WinError(c.get_last_error())
                    self.paint_count += 1
            except Exception as exc:
                self.error = exc
            finally:
                end(hwnd, c.byref(ps))
            return 0
        if msg == WM_DPICHANGED:
            rect = c.cast(lp, c.POINTER(w.RECT)).contents
            move_window(hwnd, None, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, 0x14)
            return 0
        if self.on_input and self._pixels is not None:
            if msg == WM_IME_STARTCOMPOSITION:
                if get_focus() == hwnd:
                    self._text.start()
                else:
                    self._text.reset()
                return default(hwnd, msg, wp, lp)
            if msg == WM_IME_ENDCOMPOSITION:
                self._text.end()
                return default(hwnd, msg, wp, lp)
            if msg == WM_IME_COMPOSITION and lp & GCS_RESULTSTR:
                if self._text.revision is not None and get_focus() == hwnd:
                    self._text.commit(ime_result(hwnd))
                # The default window may display simultaneous preedit updates.
                # Only this handler consumes the result and sends committed text.
                return default(hwnd, msg, wp, lp & 0x1ff) if lp & 0x1ff else 0
            if msg == WM_IME_CHAR:
                # DefWindowProc would translate this legacy copy into a second WM_CHAR.
                return 0
            if msg == WM_CHAR:
                if wp == 0x20 and self._space_as_key:
                    self._space_as_key = False
                elif get_focus() == hwnd:
                    self._text.character(wp)
                return 0
            if msg in (WM_MOUSEMOVE, WM_LBUTTONUP, WM_RBUTTONUP):
                x, y = c.c_short(lp & 0xffff).value, c.c_short((lp >> 16) & 0xffff).value
                rect = w.RECT()
                if get_rect(hwnd, c.byref(rect)):
                    ox, oy, width, height = viewport(rect.right, rect.bottom, self.width, self.height)
                    if ox <= x < ox + width and oy <= y < oy + height:
                        px = source_coordinate(x - ox, width, self.width)
                        py = source_coordinate(y - oy, height, self.height)
                        if msg == WM_MOUSEMOVE:
                            self.on_input(('move', px, py))
                        else:
                            button = 'left' if msg == WM_LBUTTONUP else 'right'
                            self.on_input(('click', px, py, button))
                return 0
            if msg == WM_KEYUP and wp in (0x5b, 0x5c):
                self.on_input(('super',))
                return 0
            if msg == WM_KEYDOWN:
                self._text.next_key()
            if msg == WM_KEYDOWN and self._text.composing:
                return default(hwnd, msg, wp, lp)
            if msg == WM_KEYDOWN and wp == 0x20:
                self._space_as_key = self.text_focus() is None
                if not self._space_as_key:
                    return default(hwnd, msg, wp, lp)
            if msg == WM_KEYDOWN and not (lp & (1 << 30)):
                ctrl = get_key_state(VK_CONTROL) < 0
                shift = get_key_state(VK_SHIFT) < 0
                if wp == ord('A') and ctrl and not shift and get_key_state(VK_MENU) >= 0:
                    revision = self.text_focus()
                    if revision is not None:
                        self.on_input(('select-all', revision))
                        return 0
                if wp == 0x1b and ctrl and not shift:
                    self.on_input(('super',))
                    return 0
                if wp in NAVIGATION and not ctrl and get_key_state(VK_MENU) >= 0:
                    if not shift or wp == 0x09:
                        self.on_input(('key', 'Shift+Tab' if shift else NAVIGATION[wp]))
                        return 0
        return default(hwnd, msg, wp, lp)

    def set_title(self, title):
        if not self.closed:
            set_title(self.hwnd, title)

    def present(self, pixels):
        if len(pixels) != self.width * self.height * 4:
            raise ValueError('incomplete local scanout')
        with self._lock:
            if self.closed:
                return False
            self._pixels = bytes(pixels)
            if not self._posted:
                self._posted = True
                if not post(self.hwnd, PRESENT, 0, 0):
                    self._posted = False
                    return False
        return True

    def pump(self, wait=False):
        if threading.get_ident() != self._owner:
            raise RuntimeError('pump from the owning UI thread')
        msg = w.MSG()
        if not self.closed and wait:
            result = get_message(c.byref(msg), None, 0, 0)
            if result < 0:
                raise c.WinError(c.get_last_error())
            if result == 0:
                self.close()
            else:
                translate(c.byref(msg))
                dispatch(c.byref(msg))
        while not self.closed and peek(c.byref(msg), None, 0, 0, 1):
            translate(c.byref(msg))
            dispatch(c.byref(msg))
        if self.error:
            raise self.error
        return not self.closed

    def close(self):
        if threading.get_ident() != self._owner:
            return post(self.hwnd, WM_CLOSE, 0, 0) if not self.closed else True
        if not self.closed:
            destroy(self.hwnd)
        if self._name is not None:
            unregister(self._name, self._instance)
            self._name = None


def open_window(title, width, height, on_input=None):
    enable_native_dpi()
    return NativeWindow(title, width, height, on_input)
