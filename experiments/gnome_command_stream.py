"""Record or replay the original GNOME Shell's vtest commands on private sockets."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import socket
import struct
import sys

# Also support running the experiment by file path from outside the repository.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_render.gnome_commands import (
    CAPS, CAPS2, CREATE, GET, PING, PUT, RENDERER, SUBMIT, UNREF, VERSION, WAIT,
    StreamError, _connect, _request, _TRANSFER, copy_commands,
)


def record(args):
    # The socket is created inside an owner-private directory. The caller can
    # pipe the unidirectional command stream straight to the replay process.
    state = args.listen.parent.stat()
    if state.st_uid != os.geteuid() or state.st_mode & 0o077:
        raise StreamError('the listening socket needs an owner-private parent directory')
    if sys.stdout.isatty():
        raise StreamError('redirect the command stream to a private file or a pipe')
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
        listener.bind(os.fspath(args.listen))
        try:
            listener.listen(1)
            with listener.accept()[0] as app, _connect(args.gpu) as gpu, app.makefile('rb') as incoming:
                if args.application == 'nautilus':
                    pid, uid, _ = struct.unpack('3i', app.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid() or not os.path.samefile(f'/proc/{pid}/exe', '/usr/bin/nautilus'):
                        raise StreamError('expected the original Files process on its private rendering socket')
                copy_commands(incoming, gpu, recording=sys.stdout.buffer, respond=app.sendall,
                              renderer=args.application.encode('ascii'))
        finally:
            args.listen.unlink(missing_ok=True)


def replay(args):
    def present(desc, pixels):
        _, level, stride, _, x, y, _, width, height, depth, size = desc
        if (level, x, y, width, height, depth) != (0, 0, 0, args.width, args.height, 1):
            return
        pitch = stride or width * 4
        if pitch not in (width * 4, (width * 4 + 63) // 64 * 64) or size != pitch * height:
            raise StreamError('unsupported scanout stride')
        if pitch != width * 4:
            pixels = b''.join(pixels[i:i + width * 4] for i in range(0, size, pitch))
        # The tested GNOME/Mesa software display target is little-endian BGRX8.
        sys.stdout.buffer.write(pixels)
        sys.stdout.buffer.flush()

    if sys.stdout.isatty() or not 1 <= args.width <= 8192 or not 1 <= args.height <= 8192:
        raise StreamError('provide valid dimensions and pipe raw BGRX output to a local presenter')
    with _connect(args.gpu) as gpu:
        copy_commands(sys.stdin.buffer, gpu, scanout=present, renderer=args.application.encode('ascii'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    roles = parser.add_subparsers(required=True)
    capture = roles.add_parser('record', help='receive GNOME commands and write a command stream to stdout')
    capture.add_argument('--listen', type=Path, required=True)
    capture.add_argument('--gpu', type=Path, required=True)
    capture.add_argument('--application', choices=('gnome-shell', 'nautilus'), default='gnome-shell')
    capture.set_defaults(action=record)
    client = roles.add_parser('replay', help='read commands on stdin and output locally rendered BGRX surfaces')
    client.add_argument('--gpu', type=Path, required=True)
    client.add_argument('--width', type=int, required=True)
    client.add_argument('--height', type=int, required=True)
    client.add_argument('--application', choices=('gnome-shell', 'nautilus'), default='gnome-shell')
    client.set_defaults(action=replay)
    args = parser.parse_args()
    try:
        args.action(args)
    except (OSError, StreamError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
