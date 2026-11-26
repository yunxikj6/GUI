"""Verified runtime inputs for the redistributable Windows client.

The normal offline rebuild takes pinned runtime members from the committed
release ZIP. --runtime-cache rebuilds them independently from upstream archives.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile

if __package__:
    from .prepare_virgl_windows import MANIFEST, installed_path, prepare_runtime
else:
    from prepare_virgl_windows import MANIFEST, installed_path, prepare_runtime


PYTHON_LOCK = Path(__file__).with_name('windows_python.json')
PYTHON_DIR = 'windows/runtime/'
VIRGL_DIR = 'thin_client/vendor/virgl/windows-x64/'
# Embeddable CPython resolves each line relative to python.exe. Only the
# signed-off stdlib, Windows extensions and the extracted client are on sys.path.
CLIENT_PTH = b'python313.zip\r\n.\r\n..\\..\r\n'
PTH_NAME = 'python313._pth'


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def runtime_payloads(existing, cache=None, fetch=False):
    python = json.loads(PYTHON_LOCK.read_text(encoding='utf-8'))
    graphics = json.loads(MANIFEST.read_text(encoding='utf-8'))
    wanted = {PYTHON_DIR + file: pin for file, pin in python['files'].items()}
    wanted[PYTHON_DIR + PTH_NAME] = checksum(CLIENT_PTH)
    for pkg in graphics['packages']:
        for file, pin in pkg['files'].items():
            wanted[VIRGL_DIR + installed_path(file).as_posix()] = pin
    wanted[VIRGL_DIR + MANIFEST.name] = checksum(MANIFEST.read_bytes())

    if cache is None and not fetch:
        if not existing.is_file():
            raise ValueError('No released runtime available. Build with --fetch-runtimes or --runtime-cache DIR.')
        with zipfile.ZipFile(existing) as packed:
            try:
                payloads = {name: packed.read(name) for name in wanted}
            except KeyError as error:
                raise ValueError('Release lacks the pinned runtime. Build with --fetch-runtimes or --runtime-cache DIR.') from error
    else:
        with tempfile.TemporaryDirectory() as work:
            stage = Path(work)
            if cache is None:
                with urllib.request.urlopen(python['archiveUrl'], timeout=45) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                archive = stage / 'python.zip'
                archive.write_bytes(raw)
            else:
                archive = cache / python['archiveUrl'].rsplit('/', 1)[1]
            if checksum(archive.read_bytes()) != python['archiveSha256']:
                raise ValueError('CPython archive checksum mismatch')
            with zipfile.ZipFile(archive) as source:
                if sorted(source.namelist()) != sorted(python['files']):
                    raise ValueError('Unexpected CPython archive contents')
                payloads = {}
                for name, pin in python['files'].items():
                    data = source.read(name)
                    if checksum(data) != pin:
                        raise ValueError('CPython member checksum mismatch: ' + name)
                    payloads[PYTHON_DIR + name] = CLIENT_PTH if name == PTH_NAME else data
            runtime = stage / 'graphics'
            prepare_runtime(runtime, cache)
            for name in wanted:
                if name.startswith(VIRGL_DIR):
                    payloads[name] = (runtime / name.removeprefix(VIRGL_DIR)).read_bytes()
    for name, pin in wanted.items():
        if checksum(payloads[name]) != pin:
            raise ValueError('Windows runtime member does not match the pinned distribution: ' + name)
    return payloads
