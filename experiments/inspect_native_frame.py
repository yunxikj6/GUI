"""Display a single exported original CLRP drawing in a read-only Windows window."""
import argparse
from pathlib import Path
import sys
import time

from cloud_render.wire import Frame, WireError, read_message
from experiments.persistent_application_painter import OriginalPicturePainter


def load_frame(path):
    with path.open('rb') as stream:
        frame = read_message(stream, expected=Frame)
        if frame is None or stream.read(1):
            raise WireError('Expected exactly one original drawing.')
        return frame


def display(frame, node):
    from thin_client.gnome_window import open_window

    with OriginalPicturePainter(node=node) as painter:
        rgba = painter(frame)
    bgrx = bytearray(rgba)
    bgrx[0::4], bgrx[2::4] = rgba[2::4], rgba[0::4]
    window = open_window('Original drawing · Read-only', frame.width, frame.height, lambda event: None)
    try:
        if not window.present(bgrx):
            raise RuntimeError('The native window closed before the drawing was presented.')
        while window.pump():
            time.sleep(.012)
    finally:
        window.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('frame', type=Path, help='one exported CLRP Frame')
    parser.add_argument('--node', type=Path, help='Windows Node.js executable')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('This inspection window requires x64 Windows.')
    bundled = Path(__file__).resolve().parents[1] / 'thin_client/vendor/node/windows-x64/node.exe'
    display(load_frame(args.frame), args.node or (bundled if bundled.is_file() else 'node'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
