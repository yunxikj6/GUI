import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud_render.provision import chatgpt


class CloudVendorDownload(unittest.TestCase):
    def test_only_a_verified_archive_is_published(self):
        data = b'cloud-side vendor archive'
        record = dict(chatgpt.PACKAGE, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as tmp, patch.object(chatgpt, 'PACKAGE', record), \
                patch('urllib.request.build_opener') as opener:
            destination = Path(tmp)
            request = opener.return_value.open
            for changed in (data[:-1], data + b'x', b'?' * len(data)):
                request.return_value = io.BytesIO(changed)
                with self.assertRaises(RuntimeError):
                    chatgpt.fetch_vendor(destination)
                self.assertEqual(list(destination.iterdir()), [])
            request.return_value = io.BytesIO(data)
            archive = chatgpt.fetch_vendor(destination)
            request.assert_called_with(chatgpt.PACKAGE['url'], timeout=60)
            self.assertEqual(archive.read_bytes(), data)
            self.assertEqual(list(destination.iterdir()), [archive])
            request.return_value = io.BytesIO(data)
            with self.assertRaises(FileExistsError):
                chatgpt.fetch_vendor(destination)
