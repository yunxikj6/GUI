"""Prepare the pinned x64 Win32 graphics DLLs and their licenses for distribution.

Build-time only. Requires Python 3.10+ and a tar executable with zstd support.
Pass --cache for repeatable offline builds with previously downloaded archives.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

MANIFEST = Path(__file__).resolve().parents[1] / 'thin_client' / 'virgl_runtime.json'
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024


def installed_path(source):
    return Path(*source.split('/')[2:])  # bin/F.dll or share/licenses/<package>/...


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_runtime(destination, cache=None):
    destination = Path(destination)
    specification = json.loads(MANIFEST.read_text(encoding='utf-8'))
    expected = {installed_path(source): checksum for pkg in specification['packages']
                for source, checksum in pkg['files'].items()}
    expected[Path(MANIFEST.name)] = digest(MANIFEST)
    if destination.exists():
        if all((destination / path).is_file() and digest(destination / path) == pin
               for path, pin in expected.items()):
            return
        raise ValueError('runtime directory already exists with different or missing files')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        work = Path(temp)
        stage = work / 'runtime'
        stage.mkdir()
        for package in specification['packages']:
            url = package['source']
            filename = url.rsplit('/', 1)[-1]
            if cache:
                archive = Path(cache) / filename
            else:
                archive = work / filename
                with urllib.request.urlopen(url, timeout=40) as source:
                    raw = source.read(MAX_ARCHIVE_BYTES + 1)
                if len(raw) > MAX_ARCHIVE_BYTES:
                    raise ValueError('archive exceeded size limit')
                archive.write_bytes(raw)
            if archive.stat().st_size > MAX_ARCHIVE_BYTES or digest(archive) != package['sha256']:
                raise ValueError('archive checksum mismatch for ' + filename)
            # Fixed members from a verified archive are streamed separately.
            # No archive paths, links or permissions are installed directly.
            for source, pin in package['files'].items():
                path = stage / installed_path(source)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('wb') as out:
                    subprocess.run(['tar', '-xOf', str(archive), source], stdout=out, check=True)
                if digest(path) != pin:
                    raise ValueError('member checksum mismatch for ' + source)
        shutil.copyfile(MANIFEST, stage / MANIFEST.name)
        os.rename(stage, destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--cache', type=Path, help='offline directory containing the pinned .pkg.tar.zst archives')
    options = parser.parse_args()
    try:
        prepare_runtime(options.destination, options.cache)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Could not prepare VirGL runtime: {error}\n')
