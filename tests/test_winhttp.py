"""The Windows provider transport pins the origin and never delegates redirects."""
import ctypes as ct
import urllib.error
import urllib.request
import unittest
from unittest.mock import Mock, patch

from thin_client import winhttp


class WindowsInstaBoxHTTP(unittest.TestCase):
    def test_rejects_other_origins_headers_and_methods_before_loading_windows(self):
        origin = 'https://api.instabox.sh'
        cases = [
            urllib.request.Request('http://api.instabox.sh/v1/machines'),
            urllib.request.Request('https://api.instabox.sh.other.invalid/v1/machines'),
            urllib.request.Request('https://user@api.instabox.sh/v1/machines'),
            urllib.request.Request(origin + ':8443/v1/machines'),
            urllib.request.Request(origin + '/v1/machines#x'),
            urllib.request.Request(origin + '/other'),
            urllib.request.Request(origin + '/v1/machines', headers={'Host': 'other.invalid'}),
            urllib.request.Request(origin + '/v1/machines', headers={'X-API-Key': 'fake\r\nX: other'}),
            urllib.request.Request(origin + '/v1/machines', method='TRACE'),
        ]
        with patch.object(winhttp, '_api', side_effect=AssertionError):
            for req in cases:
                with self.subTest(url=req.full_url), self.assertRaises(ValueError):
                    winhttp.open_request(req, 1)

    def test_redirect_is_an_http_error_and_all_owned_handles_close(self):
        api = Mock()
        api.WinHttpOpen.return_value = 10
        api.WinHttpConnect.return_value = 20
        api.WinHttpOpenRequest.return_value = 30
        def status(handle, query, name, value, size, index):
            ct.cast(value, ct.POINTER(winhttp.DWORD))[0] = 302
            return 1
        def eof(handle, buffer, capacity, count):
            ct.cast(count, ct.POINTER(winhttp.DWORD))[0] = 0
            return 1
        def check_options(handle, option, flags, length):
            self.assertEqual((handle, option, length), (30, 63, 4))
            self.assertTrue(ct.cast(flags, ct.POINTER(winhttp.DWORD))[0] & 2)
            return 1
        api.WinHttpQueryHeaders.side_effect = status
        api.WinHttpReadData.side_effect = eof
        api.WinHttpSetOption.side_effect = check_options
        request = urllib.request.Request('https://api.instabox.sh/v1/machines?limit=1', headers={'X-API-Key':'local-test'})
        with patch.object(winhttp, '_api', return_value=api), self.assertRaises(urllib.error.HTTPError) as caught:
            winhttp.open_request(request, 3)
        self.assertEqual(caught.exception.code, 302)
        api.WinHttpConnect.assert_called_once_with(10, 'api.instabox.sh', 443, 0)
        self.assertEqual(api.WinHttpOpenRequest.call_args.args[2], '/v1/machines?limit=1')
        self.assertEqual(api.WinHttpOpenRequest.call_args.args[-1], 0x00800000)
        self.assertEqual(api.WinHttpSendRequest.call_count, 1)
        self.assertEqual([c.args[0] for c in api.WinHttpCloseHandle.call_args_list], [30, 20, 10])
