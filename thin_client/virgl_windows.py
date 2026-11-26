"""Replay Mesa vtest 0 commands with the pinned native Windows VirGL renderer.

The cloud application owns the desktop and generates its drawing commands. Call
replay on a dedicated thread in a graphics-only process. Callbacks receive
complete, top-down, little-endian BGRX surfaces rendered on this machine.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import ctypes as ct
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import sysconfig
import threading
from typing import BinaryIO

CAPS, CREATE, UNREF, GET, PUT, SUBMIT, WAIT, RENDERER, CAPS2, PING, VERSION = range(1, 12)
HEADER = struct.Struct('<II')
CREATE_ARGS = struct.Struct('<10I')
TRANSFER = struct.Struct('<11I')
WORD = struct.Struct('<I')
FIXED_WORDS = {CAPS: 0, CREATE: 10, UNREF: 1, GET: 11, WAIT: 2, CAPS2: 0, PING: 0, VERSION: 1}
MAX_MESSAGE = 32 << 20
MAX_BUFFER = 64 << 20
MAX_DIMENSION = 8192
MAX_RESOURCE = 256 << 20
MAX_RESIDENT = 512 << 20
MAX_HANDLES = 8192
_RENDERER_LOCK = threading.Lock()
_MANIFEST = Path(__file__).with_name('virgl_runtime.json')


class VirglError(ValueError):
    """The stream, runtime or graphics driver could not complete replay."""


def _read(stream, size, *, allow_eof=False):
    chunk = stream.read(size)
    if not isinstance(chunk, bytes) or len(chunk) > size:
        raise VirglError('invalid stream read')
    if len(chunk) == size or (allow_eof and not chunk):
        return chunk
    # File-like transports may yield short, ordered chunks.
    parts = bytearray(chunk)
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(parts):
            raise VirglError('truncated Mesa command stream')
        parts.extend(chunk)
    return bytes(parts)


def _requests(stream, *, wallpaper=None):
    from cloud_render import gnome_wallpaper
    references_left = gnome_wallpaper.MAX_REFERENCES if wallpaper is not None else 0
    while header := _read(stream, HEADER.size, allow_eof=True):
        words, opcode = HEADER.unpack(header)
        if opcode == gnome_wallpaper.REFERENCE:
            if not references_left or words != (TRANSFER.size + 32) // 4:
                raise VirglError('unavailable desktop resource reference')
            body = _read(stream, words * 4)
            if references_left == gnome_wallpaper.MAX_REFERENCES:
                if (type(wallpaper) is not bytes or len(wallpaper) != gnome_wallpaper.PIXELS * 4
                        or hashlib.sha256(wallpaper).digest() != gnome_wallpaper.RGBX_SHA256):
                    raise VirglError('local desktop resource failed verification')
            try:
                _, texture = gnome_wallpaper.expand_reference(body, wallpaper)
            except ValueError as exc:
                raise VirglError('invalid desktop resource reference') from exc
            references_left -= 1
            yield PUT, body[:TRANSFER.size] + texture
            continue
        if opcode == RENDERER:
            if not 1 <= words <= 64:
                raise VirglError('invalid renderer name length')
            body = _read(stream, words)
        elif opcode == PUT:
            if not 11 <= words <= 11 + MAX_MESSAGE // 4:
                raise VirglError('invalid upload header')
            desc = _read(stream, TRANSFER.size)
            size = WORD.unpack_from(desc, TRANSFER.size - 4)[0]
            if size > MAX_MESSAGE or words != 11 + (size + 3) // 4:
                raise VirglError('invalid texture upload length')
            body = desc + _read(stream, size)
        elif (opcode == SUBMIT and words <= MAX_MESSAGE // 4) or FIXED_WORDS.get(opcode) == words:
            body = _read(stream, words * 4)
        else:
            raise VirglError('unsupported Mesa command or invalid length')
        yield opcode, body


@dataclass(frozen=True)
class _Resource:
    args: tuple[int, ...]
    budget: int

    @classmethod
    def checked(cls, body):
        args = CREATE_ARGS.unpack(body)
        handle, target, fmt, _, width, height, depth, layers, last_level, samples = args
        if not handle or target > 11 or not all((width, height, depth)):
            raise VirglError('invalid graphics resource descriptor')
        if target == 0:  # PIPE_BUFFER dimensions and offsets are bytes.
            if width > MAX_BUFFER or layers not in (0, 1) or (height, depth, last_level, samples) != (1, 1, 0, 0):
                raise VirglError('invalid graphics buffer')
            budget = width
        else:
            if not layers or max(width, height, depth, layers) > MAX_DIMENSION or last_level > 13 or samples > 16:
                raise VirglError('graphics texture exceeds supported dimensions')
            # VirGL 1.3 virgl_hw.h: B8G8R8X8_UNORM, S8_UINT_Z24_UNORM,
            # R8G8B8A8_UNORM and R8G8B8X8_UNORM are uncompressed 32-bit texels.
            # For single-level 2D allocations allow a second full copy, row
            # pitch alignment and one page minimum. Keep the conservative
            # estimate for unrecognized, layered, multisampled or mipmapped data.
            if target == 2 and fmt in (2, 20, 67, 134) and (depth, layers, last_level, samples) == (1, 1, 0, 0):
                aligned_row = (width * 4 + 255) // 256 * 256
                budget = max(4096, 2 * aligned_row * height)
            else:
                budget = width * height * depth * layers * 16 * max(1, samples) * (2 if last_level else 1)
        if budget > MAX_RESOURCE:
            raise VirglError('graphics resource exceeds memory budget')
        return cls(args, budget)

    def check_transfer(self, desc):
        _, level, stride, layer_stride, x, y, z, width, height, depth, size = desc
        _, _, _, _, resource_w, resource_h, resource_d, layers, last_level, _ = self.args
        if (level > last_level or size > MAX_MESSAGE or stride > MAX_MESSAGE or layer_stride > MAX_MESSAGE
                or not all((width, height, depth))
                or x + width > max(1, resource_w >> level)
                or y + height > max(1, resource_h >> level)
                or z + depth > max(1, resource_d >> level) * max(1, layers)):
            raise VirglError('invalid graphics transfer bounds')

    def is_scanout(self, desc):
        _, level, stride, layer_stride, x, y, z, width, height, depth, size = desc
        _, target, fmt, binds, w, h, resource_depth, layers, last, samples = self.args
        return ((target, fmt, binds, resource_depth, layers, last, samples) == (2, 2, 138, 1, 1, 0, 0)
                and (level, x, y, z, width, height, depth, size) == (0, 0, 0, 0, w, h, 1, w * h * 4)
                and stride in (0, w * 4) and layer_stride in (0, size))


def _play(stream, gpu, present, *, wallpaper=None):
    handles = {}
    resident = 0
    started = False
    for opcode, body in _requests(stream, wallpaper=wallpaper):
        if opcode == RENDERER:
            if started or body.rstrip(b'\0') != b'gnome-shell':
                raise VirglError('expected a single GNOME Shell context')
            gpu.start_context()
            started = True
        elif not started:
            raise VirglError('expected the GNOME Shell context first')
        elif opcode == VERSION:
            if WORD.unpack(body)[0] != 0:
                raise VirglError('this renderer requires the inline-resource vtest dialect')
        elif opcode == CREATE:
            resource = _Resource.checked(body)
            handle = resource.args[0]
            if handle in handles or len(handles) >= MAX_HANDLES or resident + resource.budget > MAX_RESIDENT:
                raise VirglError('graphics resource handle or memory budget exceeded')
            gpu.create(resource.args)
            handles[handle] = resource
            resident += resource.budget
        elif opcode == UNREF:
            handle = WORD.unpack(body)[0]
            resource = handles.pop(handle, None)
            if resource is None:
                raise VirglError('unknown graphics resource')
            gpu.unref(handle)
            resident -= resource.budget
        elif opcode in (GET, PUT):
            desc = TRANSFER.unpack_from(body)
            resource = handles.get(desc[0])
            if resource is None:
                raise VirglError('unknown transfer resource')
            resource.check_transfer(desc)
            scanout = opcode == GET and resource.is_scanout(desc)
            result = gpu.transfer(desc, body[TRANSFER.size:] if opcode == PUT else None, scanout)
            if scanout:
                present(desc[7], desc[8], result)
        elif opcode == SUBMIT:
            gpu.submit(body)
        # Capability queries and vtest WAIT responses belonged to the source
        # GPU during recording. Rendering and transfer readbacks run locally.
    if not started:
        raise VirglError('empty Mesa command stream')


# Windows LLP64 and virglrenderer 1.3 public C ABI (virglrenderer.h).
_PTR = ct.c_void_p
_INT = ct.c_int32
_UINT = ct.c_uint32


class _ContextParams(ct.Structure):
    _fields_ = [('version', _INT), ('shared', ct.c_bool), ('major', _INT), ('minor', _INT), ('compat', _INT)]


class _Iovec(ct.Structure):
    _fields_ = [('base', _PTR), ('length', ct.c_size_t)]


_Fence = ct.CFUNCTYPE(None, _PTR, _UINT)
_Create = ct.CFUNCTYPE(_PTR, _PTR, _INT, ct.POINTER(_ContextParams))
_Destroy = ct.CFUNCTYPE(None, _PTR, _PTR)
_Current = ct.CFUNCTYPE(_INT, _PTR, _INT, _PTR)


class _Callbacks(ct.Structure):
    _fields_ = [('version', _INT), ('write_fence', _Fence), ('create_gl_context', _Create),
                ('destroy_gl_context', _Destroy), ('make_current', _Current), ('get_drm_fd', _PTR),
                ('write_context_fence', _PTR), ('get_server_fd', _PTR), ('get_egl_display', _PTR)]


def _proc(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.argtypes, function.restype = arguments, result
    return function


def _check(result, operation):
    if result != 0:
        raise VirglError(f'{operation} failed ({result})')


def _runtime(directory):
    directory = Path(directory).resolve(strict=True)
    metadata = json.loads(_MANIFEST.read_text(encoding='utf-8'))
    expected = {Path(member).name: digest for item in metadata['packages']
                for member, digest in item['files'].items() if member.endswith('.dll')}
    actual = {file.name for file in directory.iterdir() if file.suffix.lower() == '.dll'}
    if actual != set(expected):
        raise VirglError('missing or unexpected graphics DLLs')
    for name, digest in expected.items():
        path = directory / name
        if not path.is_file() or path.stat().st_size > 4 << 20 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise VirglError('graphics DLL checksum mismatch')
    return directory / 'libvirglrenderer-1.dll'


class _Wgl:
    """Own the hidden drawable and context callbacks for one render thread."""

    def __init__(self, cleanup):
        system32 = 0x800  # LOAD_LIBRARY_SEARCH_SYSTEM32
        user = ct.WinDLL('user32', winmode=system32)
        gdi = ct.WinDLL('gdi32', winmode=system32)
        gl = ct.WinDLL('opengl32', winmode=system32)
        create_window = _proc(user, 'CreateWindowExW', _PTR, _UINT, ct.c_wchar_p, ct.c_wchar_p, _UINT,
                              _INT, _INT, _INT, _INT, _PTR, _PTR, _PTR, _PTR)
        window = create_window(0, 'STATIC', 'VirGL drawable', 0x80000000, 0, 0, 32, 32, None, None, None, None)
        if not window:
            raise VirglError('could not create the Win32 graphics drawable')
        cleanup.callback(_proc(user, 'DestroyWindow', _INT, _PTR), window)
        self.dc = _proc(user, 'GetDC', _PTR, _PTR)(window)
        if not self.dc:
            raise VirglError('could not obtain a Win32 graphics DC')
        cleanup.callback(_proc(user, 'ReleaseDC', _INT, _PTR, _PTR), window, self.dc)
        # PIXELFORMATDESCRIPTOR: window/OpenGL, RGBA32, depth24, stencil8.
        pixel_format = struct.pack('<HHI20BIII', 40, 1, 0x24, 0, 32, 0, 0, 0, 0, 0, 0,
                                   8, 0, 0, 0, 0, 0, 0, 24, 8, 0, 0, 0, 0, 0, 0)
        choose = _proc(gdi, 'ChoosePixelFormat', _INT, _PTR, _PTR)
        configure = _proc(gdi, 'SetPixelFormat', _INT, _PTR, _INT, _PTR)
        selected = choose(self.dc, pixel_format)
        if not selected or not configure(self.dc, selected, pixel_format):
            raise VirglError('the Windows graphics driver has no usable OpenGL pixel format')
        self.legacy = _proc(gl, 'wglCreateContext', _PTR, _PTR)
        self.share = _proc(gl, 'wglShareLists', _INT, _PTR, _PTR)
        self.bind = _proc(gl, 'wglMakeCurrent', _INT, _PTR, _PTR)
        self.delete = _proc(gl, 'wglDeleteContext', _INT, _PTR)
        self.current = _proc(gl, 'wglGetCurrentContext', _PTR)
        previous_dc = _proc(gl, 'wglGetCurrentDC', _PTR)()
        cleanup.callback(self.bind, previous_dc, self.current())
        self.owned = set()
        cleanup.callback(self.close)
        bootstrap = self.legacy(self.dc)
        if not bootstrap:
            raise VirglError('could not bootstrap Windows OpenGL')
        self.owned.add(bootstrap)
        if not self.bind(self.dc, bootstrap):
            raise VirglError('could not activate Windows OpenGL')
        address = _proc(gl, 'wglGetProcAddress', _PTR, ct.c_char_p)(b'wglCreateContextAttribsARB')
        invalid = (None, 0, 1, 2, 3, _PTR(-1).value)
        self.modern = None if address in invalid else ct.WINFUNCTYPE(_PTR, _PTR, _PTR, ct.POINTER(_INT))(address)
        # Keep the callback objects alive until after virgl_renderer_cleanup.
        self.callbacks = _Callbacks(1, _Fence(lambda _cookie, _seq: None), _Create(self.create),
                                    _Destroy(self.destroy), _Current(self.make_current))

    def create(self, _cookie, _scanout, pointer):
        params = pointer.contents
        share = self.current() if params.shared else None
        if self.modern:
            attrs = (_INT * 7)(0x2091, params.major, 0x2092, params.minor, 0x9126, 2 if params.compat else 1, 0)
            handle = self.modern(self.dc, share, attrs)
        else:
            handle = self.legacy(self.dc)
            if handle and share and not self.share(share, handle):
                self.delete(handle)
                return None
        if handle:
            self.owned.add(handle)
        return handle

    def destroy(self, _cookie, handle):
        if handle in self.owned:
            if self.current() == handle:
                self.bind(None, None)
            self.delete(handle)
            self.owned.discard(handle)

    def make_current(self, _cookie, _scanout, handle):
        if handle and handle not in self.owned:
            return -1
        return 0 if self.bind(self.dc if handle else None, handle) else -1

    def close(self):
        for handle in tuple(self.owned):
            self.destroy(None, handle)


class _NativeGpu:
    def __init__(self, dll_path, cleanup):
        # Load all non-system dependencies from the exact verified directory.
        # System32 supplies WGL, kernel32, and the Windows Universal CRT.
        system32_and_dll_directory = 0x800 | 0x100
        dll = ct.CDLL(str(dll_path), winmode=system32_and_dll_directory)
        self.wgl = _Wgl(cleanup)
        self.cleanup = cleanup
        init = _proc(dll, 'virgl_renderer_init', _INT, _PTR, _INT, ct.POINTER(_Callbacks))
        finalize = _proc(dll, 'virgl_renderer_cleanup', None, _PTR)
        self.make_context = _proc(dll, 'virgl_renderer_context_create', _INT, _UINT, _UINT, ct.c_char_p)
        self.destroy_context = _proc(dll, 'virgl_renderer_context_destroy', None, _UINT)
        self.make_resource = _proc(dll, 'virgl_renderer_resource_create', _INT, _PTR, _PTR, _UINT)
        self.attach = _proc(dll, 'virgl_renderer_ctx_attach_resource', None, _INT, _INT)
        self.detach = _proc(dll, 'virgl_renderer_ctx_detach_resource', None, _INT, _INT)
        self.release = _proc(dll, 'virgl_renderer_resource_unref', None, _UINT)
        self.execute = _proc(dll, 'virgl_renderer_submit_cmd', _INT, _PTR, _INT, _INT)
        self.read = _proc(dll, 'virgl_renderer_transfer_read_iov', _INT, _UINT, _UINT, _UINT, _UINT,
                          _UINT, _PTR, ct.c_uint64, ct.POINTER(_Iovec), _INT)
        self.write = _proc(dll, 'virgl_renderer_transfer_write_iov', _INT, _UINT, _UINT, _INT, _UINT,
                           _UINT, _PTR, ct.c_uint64, ct.POINTER(_Iovec), _UINT)
        _check(init(1, 0, ct.byref(self.wgl.callbacks)), 'VirGL initialization')
        cleanup.callback(finalize, 1)

    def start_context(self):
        _check(self.make_context(1, 11, b'gnome-shell'), 'VirGL context creation')
        self.cleanup.callback(self.destroy_context, 1)

    def create(self, args):
        data = (_UINT * 11)(*args, 0)
        _check(self.make_resource(data, None, 0), 'VirGL resource creation')
        self.attach(1, args[0])

    def unref(self, handle):
        self.detach(1, handle)
        self.release(handle)

    def submit(self, payload):
        # The library requires aligned, exclusively owned storage until return.
        data = ct.create_string_buffer(payload)
        _check(self.execute(data, 1, len(payload) // 4), 'VirGL submission')

    def transfer(self, desc, payload, scanout):
        handle, level, stride, layer_stride, x, y, z, width, height, depth, size = desc
        if not size:
            return None
        box = (_UINT * 6)(x, y, z, width, height, depth)
        data = ct.create_string_buffer(size) if payload is None else ct.create_string_buffer(payload, size)
        vector = _Iovec(ct.cast(data, _PTR), size)
        routine = self.read if payload is None else self.write
        _check(routine(handle, 1, level, stride, layer_stride, box, 0, ct.byref(vector), 1), 'VirGL transfer')
        return data.raw if scanout else None


def replay(stream: BinaryIO, on_scanout: Callable[[int, int, bytes], None], *, runtime_dir: Path | str | None = None,
           wallpaper: bytes | None = None) -> None:
    """Incrementally replay one GNOME Shell stream until EOF.

    The stream, WGL context and callbacks run on the invoking thread. A blocking
    callback applies backpressure. Close the transport to interrupt a blocked
    read. Exceptions propagate after cleanup. The native DLL permits a single
    active renderer in this process. A new stream needs its own replay call.
    """
    if os.name != 'nt' or sysconfig.get_platform() != 'win-amd64':
        raise VirglError('this graphics runtime requires x64 Windows Python')
    if not _RENDERER_LOCK.acquire(blocking=False):
        raise VirglError('another VirGL stream is already active in this process')
    with ExitStack() as cleanup:
        cleanup.callback(_RENDERER_LOCK.release)
        directory = runtime_dir if runtime_dir is not None else _MANIFEST.parent / 'vendor' / 'virgl' / 'windows-x64'
        renderer = _NativeGpu(_runtime(directory), cleanup)
        _play(stream, renderer, on_scanout, wallpaper=wallpaper)
