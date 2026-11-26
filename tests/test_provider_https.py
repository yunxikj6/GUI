"""All authenticated control and file requests use the same verified HTTPS policy."""
import ssl
import urllib.request
import unittest
from unittest.mock import patch

import instabox


class ProviderHTTPS(unittest.TestCase):
    def test_origin_header_and_method_rejected_before_connect(self):
        origin = 'https://api.instabox.sh'
        cases = [
            urllib.request.Request('http://api.instabox.sh/v1/machines'),
            urllib.request.Request('https://api.instabox.sh.other.invalid/v1/machines'),
            urllib.request.Request('https://user@api.instabox.sh/v1/machines'),
            urllib.request.Request(origin + ':8443/v1/machines'),
            urllib.request.Request(origin + '/v1/machines#x'),
            urllib.request.Request(origin + '/other'),
            urllib.request.Request(origin + '/v1/machines', headers={'Host': 'other.invalid'}),
            urllib.request.Request(origin + '/v1/machines', headers={'X-API-Key': 'fixture\r\nX: invalid'}),
            urllib.request.Request(origin + '/v1/machines', method='TRACE'),
        ]
        with patch.object(urllib.request, 'build_opener', side_effect=AssertionError):
            for req in cases:
                with self.subTest(url=req.full_url), self.assertRaises(ValueError):
                    instabox.open_provider_request(req, 1)

    def test_verified_tls_and_no_redirect_apply_to_the_request(self):
        req = urllib.request.Request('https://api.instabox.sh/v1/machines', headers={'X-API-Key': 'fixture'})
        with patch.object(urllib.request, 'build_opener') as factory:
            result = instabox.open_provider_request(req, 4)
        redirect, https = factory.call_args.args
        self.assertIsNone(redirect.redirect_request(req, None, 302, '', {}, 'https://other.invalid/'))
        self.assertIsInstance(https, urllib.request.HTTPSHandler)
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        factory.return_value.open.assert_called_once_with(req, timeout=4)
        self.assertIs(result, factory.return_value.open.return_value)
