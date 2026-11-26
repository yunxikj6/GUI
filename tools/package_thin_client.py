"""Build the offline Windows client from an explicit list of distribution files."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import zipfile

if __package__:
    from .windows_runtime import runtime_payloads
else:
    from windows_runtime import runtime_payloads

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ('windows/ThinStart.cmd', 'Start.cmd'),
    ('docs/thin-client.md', 'README.md'),
    ('thin_client_entry.py', 'thin_client_entry.py'),
    ('instabox.py', 'instabox.py'),
    ('cloud_render/wire.py', 'cloud_render/wire.py'),
    ('cloud_render/gnome_wire.py', 'cloud_render/gnome_wire.py'),
    ('cloud_render/gnome_input.py', 'cloud_render/gnome_input.py'),
    ('cloud_render/frame_delta.py', 'cloud_render/frame_delta.py'),
    ('cloud_render/instabox_relay.py', 'cloud_render/instabox_relay.py'),
)
CLIENT_FILES = ('winhttp.py', 'gnome_desktop.py', 'gnome_window.py', 'gnome_transport.py', 'virgl_windows.py', 'virgl_runtime.json',
                'viewer.py', 'app_window.py', 'instabox_transport.py', 'index.html', 'renderer.js',
                'viewer.js', 'input.js', 'viewer.css', 'vendor/canvaskit.js', 'vendor/canvaskit.wasm.gz',
                'vendor/CanvasKit.json', 'vendor/CanvasKit-LICENSE.txt', 'vendor/DejaVu-COPYRIGHT.txt',
                'vendor/Liberation-COPYRIGHT.txt', 'vendor/NotoSansCJK-OFL.txt')
WINDOWS_FILES = ('CloudLinuxWorkspace-x64.exe', 'CloudLinuxWorkspace-arm64.exe', 'Go-LICENSE.txt')


def add(archive, name, data):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(output, *, runtime_cache=None, fetch_runtimes=False):
    files = dict((name, ROOT / source) for source, name in FILES)
    for directory, entries in [('thin_client', CLIENT_FILES), ('windows', WINDOWS_FILES)]:
        for item in entries:
            name = f'{directory}/{item}'
            files[name] = ROOT / name
    runtime = runtime_payloads(ROOT / 'releases/thin-windows/cloud-workspace.zip', runtime_cache, fetch_runtimes)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=output.parent, prefix='.thin-client-', suffix='.zip')
    os.close(fd)
    try:
        checksums = []
        with zipfile.ZipFile(temp, 'w') as archive:
            for name in sorted(files.keys() | runtime.keys()):
                data = files[name].read_bytes() if name in files else runtime[name]
                if name.endswith('.cmd'):
                    data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                add(archive, name, data)
                checksums.append(f'{hashlib.sha256(data).hexdigest()}  {name}\n')
            add(archive, 'SHA256SUMS', ''.join(checksums).encode('utf-8'))
        os.replace(temp, output)
    finally:
        Path(temp).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    material = parser.add_mutually_exclusive_group()
    material.add_argument('--runtime-cache', type=Path, help='directory containing the pinned CPython ZIP and MSYS2 archives')
    material.add_argument('--fetch-runtimes', action='store_true', help='download and verify the pinned official runtime archives')
    args = parser.parse_args()
    build(args.output, runtime_cache=args.runtime_cache, fetch_runtimes=args.fetch_runtimes)
