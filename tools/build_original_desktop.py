"""Build the independent x64 original-application client from the verified desktop release."""
import argparse
import hashlib
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = '24.22.3'
NODE_ARCHIVE = 'e612af99f626ab415ea225af6bce5c971feb448cb5cae250bb1c3fa50c5f7d9f'
NODE_EXE = '86fc4785327db5f5c7558dc6156c543b433c313e813a222e088086222ab4afa6'
# The original desktop feature requires no local copy of Ubuntu or the cloud application.
MODULES = (
    'thin_client/scene_cache.py',
    'cloud_render/gnome_commands.py',
    'cloud_render/gnome_input.py',
    'cloud_render/gnome_wire.py',
    'cloud_render/instabox_relay.py',
    'thin_client/gnome_window.py',
    'thin_client/gnome_wintext.py',
    'thin_client/gnome_transport.py',
    'thin_client/gnome_desktop.py',
    'experiments/application_surface.py',
    'experiments/live_application_surface.py',
    'experiments/original_desktop.py',
    'experiments/original_desktop_protocol.py',
    'experiments/persistent_application_painter.py',
    'experiments/rasterize_application_frame.cjs',
    'experiments/mutter_app/controls.py',
)
START = r"""@echo off
setlocal
if not defined PROCESSOR_ARCHITECTURE exit /b 2
set "python=%~dp0windows\runtime\python.exe"
if not "%~1"=="" goto arguments
"%python%" -m experiments.original_desktop --instabox
goto finished
:arguments
"%python%" -m experiments.original_desktop %*
:finished
set "code=%ERRORLEVEL%"
if not "%code%"=="0" pause
exit /b %code%
"""


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _entry(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)


def build(base, base_sha, node_zip, output):
    if sha(base) != base_sha or sha(node_zip) != NODE_ARCHIVE:
        raise ValueError('desktop release or official Node archive digest mismatch')
    if output.resolve() == base.resolve():
        raise ValueError('the independent feature may not replace its input archive')
    with zipfile.ZipFile(base) as previous, zipfile.ZipFile(node_zip) as node:
        folder = 'node-v'+NODE_VERSION+'-win-x64/'
        binary = node.read(folder+'node.exe')
        if hashlib.sha256(binary).hexdigest() != NODE_EXE:
            raise ValueError('unexpected Windows Node executable')
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            candidate = Path(temporary)/'client.zip'
            with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name in previous.namelist():
                    if (name.startswith('windows/runtime/') or name.startswith('thin_client/')
                            or name.startswith('cloud_render/') or name == 'instabox.py'):
                        if name not in MODULES:
                            _entry(archive, name, previous.read(name))
                for name in MODULES:
                    _entry(archive, name, (ROOT/name).read_bytes())
                prefix = 'thin_client/vendor/node/windows-x64/'
                _entry(archive, prefix+'node.exe', binary)
                _entry(archive, prefix+'LICENSE', node.read(folder+'LICENSE'))
                _entry(archive, 'Start.cmd', START.replace('\n','\r\n').encode('ascii'))
                _entry(archive, 'README.md', (ROOT/'docs/interactive-original-desktop.md').read_bytes())
                for source in sorted((ROOT/'docs/measurements/original-window').iterdir()):
                    _entry(archive, 'measurements/original-window/'+source.name, source.read_bytes())
            with zipfile.ZipFile(candidate) as checked:
                if checked.testzip():
                    raise ValueError('damaged original application release')
            candidate.replace(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('desktop_zip', type=Path)
    parser.add_argument('desktop_sha256')
    parser.add_argument('win_node_zip', type=Path, help='official node-v24.22.3-win-x64.zip')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.desktop_zip, args.desktop_sha256, args.win_node_zip, args.output)
