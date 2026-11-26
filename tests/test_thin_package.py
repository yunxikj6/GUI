import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.package_thin_client import ROOT, build


class ThinDownloadTest(unittest.TestCase):
    def test_published_windows_zip_matches_source_and_starts_without_the_full_repository(self):
        release = ROOT / 'releases/thin-windows/cloud-workspace.zip'
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rebuilt = base / 'repeat.zip'
            build(rebuilt)
            self.assertEqual(hashlib.sha256(rebuilt.read_bytes()).digest(), hashlib.sha256(release.read_bytes()).digest())
            dest = base / '独立 客户端'
            with zipfile.ZipFile(release) as archive:
                self.assertIsNone(archive.testzip())
                self.assertNotIn('linux/setup-ubuntu.sh', archive.namelist())
                self.assertNotIn('Setup.cmd', archive.namelist())
                self.assertNotIn('launcher.py', archive.namelist())
                self.assertNotIn('cloud_render/chatgpt_process.py', archive.namelist())
                archive.extractall(dest)
            for line in (dest / 'SHA256SUMS').read_text().splitlines():
                digest, name = line.split('  ', 1)
                self.assertEqual(hashlib.sha256((dest / name).read_bytes()).hexdigest(), digest, name)
            command = ('import thin_client_entry; from thin_client import viewer, instabox_transport; '
                       'assert instabox_transport.HELPER.is_file()')
            subprocess.run([sys.executable, '-E', '-c', command], cwd=dest, check=True)
