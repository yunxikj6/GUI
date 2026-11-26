"""InstaBox HTTPS using the Windows system proxy, TLS stack and trust store."""
from contextlib import ExitStack
import ctypes as ct
from functools import lru_cache
from io import BytesIO
from time import monotonic
import urllib.error
import urllib.parse


HOST = 'api.instabox.sh'
DWORD = ct.c_uint32
HANDLE = ct.c_void_p
WIDE = ct.c_wchar_p
MAX_BLOCK = 65536


@lru_cache(maxsize=1)
def _api():
    # LOAD_LIBRARY_SEARCH_SYSTEM32: never load a DLL from the current directory.
    dll = ct.WinDLL('winhttp.dll', use_last_error=True, winmode=0x800)
    def declare(name, result, *args):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = result, args
    declare('WinHttpOpen', HANDLE, WIDE, DWORD, WIDE, WIDE, DWORD)
    declare('WinHttpConnect', HANDLE, HANDLE, WIDE, ct.c_uint16, DWORD)
    declare('WinHttpOpenRequest', HANDLE, HANDLE, WIDE, WIDE, WIDE, WIDE, ct.POINTER(WIDE), DWORD)
    declare('WinHttpSetTimeouts', ct.c_int, HANDLE, ct.c_int, ct.c_int, ct.c_int, ct.c_int)
    declare('WinHttpSetOption', ct.c_int, HANDLE, DWORD, HANDLE, DWORD)
    declare('WinHttpSendRequest', ct.c_int, HANDLE, WIDE, DWORD, HANDLE, DWORD, DWORD, ct.c_size_t)
    declare('WinHttpReceiveResponse', ct.c_int, HANDLE, HANDLE)
    declare('WinHttpQueryHeaders', ct.c_int, HANDLE, DWORD, WIDE, HANDLE, ct.POINTER(DWORD), ct.POINTER(DWORD))
    declare('WinHttpReadData', ct.c_int, HANDLE, HANDLE, DWORD, ct.POINTER(DWORD))
    declare('WinHttpCloseHandle', ct.c_int, HANDLE)
    return dll


def _checked(result):
    if not result:
        # Win32 error numbers identify TLS, connection and timeout failures.
        # The exception never contains raw URLs, headers or response data.
        raise OSError(ct.get_last_error(), 'Windows HTTPS transport failed')
    return result


def _request_target(request):
    url = urllib.parse.urlsplit(request.full_url)
    if (url.scheme != 'https' or url.netloc != HOST or not url.path.startswith('/v1/') or url.fragment
            or any(c in request.full_url for c in '\r\n\0\\') or request.get_method() not in ('GET', 'POST')):
        raise ValueError('expected the private InstaBox HTTPS API origin')
    headers = []
    for name, value in request.header_items():
        if name.lower() not in ('accept', 'content-type', 'x-api-key', 'idempotency-key'):
            raise ValueError('unexpected InstaBox request header')
        if not isinstance(value, str) or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid InstaBox request header value')
        headers.append(f'{name}: {value}\r\n')
    return url.path + ('?' + url.query if url.query else ''), ''.join(headers)


class Response:
    def __init__(self, api, handle, owners, deadline):
        self._api = api
        self._handle = handle
        self._owners = owners
        self._deadline = deadline
        self._eof = False
        self._timeout_ms = None

    def read(self, size):
        if not isinstance(size, int) or not 0 <= size <= 8 * 1024 * 1024:
            raise ValueError('read requires a bounded InstaBox response size')
        if self._owners is None:
            raise ValueError('response closed')
        result = bytearray()
        while len(result) < size and not self._eof:
            remaining_ms = int((self._deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError('Windows HTTPS response timed out')
            # Never restart the full response timeout for each chunk.
            if self._timeout_ms is None or self._timeout_ms - remaining_ms >= 100:
                _checked(self._api.WinHttpSetTimeouts(self._handle, remaining_ms, remaining_ms, remaining_ms, remaining_ms))
                self._timeout_ms = remaining_ms
            capacity = min(size - len(result), MAX_BLOCK)
            chunk = ct.create_string_buffer(capacity)
            count = DWORD()
            _checked(self._api.WinHttpReadData(self._handle, chunk, capacity, ct.byref(count)))
            if count.value > capacity:
                raise OSError('invalid Windows HTTPS read size')
            if count.value:
                result.extend(chunk.raw[:count.value])
            else:
                self._eof = True
        return bytes(result)

    def close(self):
        if self._owners is not None:
            self._owners.close()
            self._owners = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_request(request, timeout):
    target, headers = _request_target(request)
    if not isinstance(timeout, (float, int)) or not 0 < timeout <= 2147483:
        raise ValueError('invalid InstaBox timeout')
    api = _api()
    deadline = monotonic() + timeout
    with ExitStack() as owners:
        def own(handle):
            handle = _checked(handle)
            owners.callback(api.WinHttpCloseHandle, handle)
            return handle
        session = own(api.WinHttpOpen('CloudLinuxWorkspace', 0, None, None, 0))
        millis = max(1, int(timeout * 1000))
        _checked(api.WinHttpSetTimeouts(session, millis, millis, millis, millis))
        connection = own(api.WinHttpConnect(session, HOST, 443, 0))
        handle = own(api.WinHttpOpenRequest(connection, request.get_method(), target, None, None, None, 0x00800000))
        # WINHTTP_OPTION_DISABLE_FEATURE: cookies, redirects and HTTP authentication.
        # The API uses only the explicit origin-bound key. TLS uses unmodified system policy.
        disabled = DWORD(1 | 2 | 4)
        _checked(api.WinHttpSetOption(handle, 63, ct.byref(disabled), ct.sizeof(disabled)))
        data = request.data or b''
        raw = ct.create_string_buffer(data) if data else None
        _checked(api.WinHttpSendRequest(handle, headers, len(headers), raw, len(data), len(data), 0))
        _checked(api.WinHttpReceiveResponse(handle, None))
        status = DWORD()
        size = DWORD(ct.sizeof(status))
        # WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER.
        _checked(api.WinHttpQueryHeaders(handle, 19 | 0x20000000, None, ct.byref(status), ct.byref(size), None))
        response = Response(api, handle, owners.pop_all(), deadline)
        if not 200 <= status.value < 300:
            with response:
                error = response.read(32768)
            raise urllib.error.HTTPError(request.full_url, status.value, 'InstaBox HTTP response', {}, BytesIO(error))
        return response
