import hashlib
import json
from pathlib import Path
import unittest
import zipfile

from tools.build_original_desktop import NODE_EXE, ROOT, START


class OriginalDesktopPackage(unittest.TestCase):
    def test_published_client_contains_this_revisions_original_renderer(self):
        receipt_path = ROOT / 'docs/measurements/cloud-render.json'
        receipt = json.loads(receipt_path.read_text())['originalDesktopPackage']
        release = receipt_path.parent / receipt['download']
        with release.open('rb') as source, zipfile.ZipFile(release) as archive:
            self.assertEqual(hashlib.file_digest(source, 'sha256').hexdigest(), receipt['sha256'])
            self.assertEqual(release.stat().st_size, receipt['bytes'])
            names = archive.namelist()
            self.assertEqual(len(names), receipt['entries'])
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(archive.read('Start.cmd'), START.replace('\n', '\r\n').encode('ascii'))
            self.assertEqual(archive.read('README.md'), (ROOT / 'docs/interactive-original-desktop.md').read_bytes())
            with archive.open('thin_client/vendor/node/windows-x64/node.exe') as node:
                self.assertEqual(hashlib.file_digest(node, 'sha256').hexdigest(), NODE_EXE)
            # Transport and decoder dependencies inherited from an older base package
            # must also match the callers. Check all shipped first-party modules.
            for name in names:
                if Path(name).suffix in ('.py', '.cjs'):
                    with self.subTest(module=name):
                        self.assertEqual(archive.read(name), (ROOT / name).read_bytes())
