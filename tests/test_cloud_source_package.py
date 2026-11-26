import hashlib
import json
from pathlib import Path, PurePosixPath
import unittest
import zipfile

from tools.package_cloud_source import SOURCE


class CloudSourcePackage(unittest.TestCase):
    def test_published_server_contains_the_current_drawing_and_transport_sources(self):
        root = Path(__file__).resolve().parents[1]
        record = root / 'docs/measurements/cloud-render.json'
        receipt = json.loads(record.read_text())['cloudSourcePackage']
        release = record.parent / receipt['download']
        with release.open('rb') as source, zipfile.ZipFile(release) as archive:
            self.assertEqual(hashlib.file_digest(source, 'sha256').hexdigest(), receipt['sha256'])
            self.assertEqual(release.stat().st_size, receipt['bytes'])
            self.assertEqual(archive.comment.decode('ascii'), receipt['sourceCommit'])
            names = set()
            for info in archive.infolist():
                if info.is_dir():
                    continue
                path = PurePosixPath(info.filename).relative_to('cloud-workspace-server')
                self.assertNotIn('..', path.parts)
                self.assertNotIn(path, names)
                names.add(path)
                with self.subTest(member=path):
                    self.assertEqual(archive.read(info), (root / path).read_bytes())
            self.assertEqual(len(names), receipt['filesMatchingReviewedGitBlobs'])
            for entry in SOURCE:
                path = PurePosixPath(entry)
                self.assertTrue(any(item == path or path in item.parents for item in names), entry)
            self.assertIn(PurePosixPath('experiments/mutter_app/capture.py'), names)
            self.assertIn(PurePosixPath('cloud_render/gnome_wallpaper.py'), names)
