import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_render.provision import chatgpt
from cloud_render.provision.__main__ import main


class OriginalGuestTests(unittest.TestCase):
    def test_changed_or_partial_vendor_archive_is_never_accepted(self):
        valid = b'private, signed package target'
        record = dict(size=len(valid), sha256=hashlib.sha256(valid).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for i, body in enumerate((valid[:-1], valid + b'x', b'x' * len(valid))):
                with self.assertRaises(ValueError):
                    chatgpt.checked_copy(io.BytesIO(body), base / str(i), record)
            chatgpt.checked_copy(io.BytesIO(valid), base / 'verified', record)
            self.assertEqual((base / 'verified').read_bytes(), valid)

    def test_invalid_deb_refused_before_any_cloud_provisioning(self):
        with tempfile.TemporaryDirectory() as tmp, patch('cloud_render.provision.__main__.prerequisites') as preflight:
            candidate = Path(tmp) / 'changed.deb'
            candidate.write_bytes(b'wrong file')
            with self.assertRaises(SystemExit):
                main(['--chatgpt-deb', str(candidate)])
            preflight.assert_not_called()
