"""Record or replay the original GNOME Shell's vtest commands on private sockets."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import socket
import stat
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
    options = {}
    if getattr(args, 'live', False):
        if not stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode):
            raise StreamError('live recording requires a pipe to the bounded command transport')
        options['max_bytes'] = None
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
        listener.bind(os.fspath(args.listen))
        try:
            listener.listen(1)
            with listener.accept()[0] as app, _connect(args.gpu) as gpu, app.makefile('rb') as incoming:
                copy_commands(incoming, gpu, recording=sys.stdout.buffer, respond=app.sendall, **options)
        finally:
            args.listen.unlink(missing_ok=True)


def replay(args):
    def present(desc, pixels):
        _, level, stride, _, x, y, _, width, height, depth, size = desc
        if (level, x, y, width, height, depth, size) != (0, 0, 0, args.width, args.height, 1,
                                                         args.width * args.height * 4):
            return
        if stride not in (0, width * 4):
            raise StreamError('unsupported scanout stride')
        # The tested GNOME/Mesa software display target is little-endian BGRX8.
        sys.stdout.buffer.write(pixels)
        sys.stdout.buffer.flush()

    if sys.stdout.isatty() or not 1 <= args.width <= 8192 or not 1 <= args.height <= 8192:
        raise StreamError('provide valid dimensions and pipe raw BGRX output to a local presenter')
    with _connect(args.gpu) as gpu:
        copy_commands(sys.stdin.buffer, gpu, scanout=present)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    roles = parser.add_subparsers(required=True)
    capture = roles.add_parser('record', help='receive GNOME commands and write a command stream to stdout')
    capture.add_argument('--listen', type=Path, required=True)
    capture.add_argument('--gpu', type=Path, required=True)
    capture.add_argument('--live', action='store_true', help='continue streaming to a pipe until disconnect')
    capture.set_defaults(action=record)
    client = roles.add_parser('replay', help='read commands on stdin and output locally rendered BGRX surfaces')
    client.add_argument('--gpu', type=Path, required=True)
    client.add_argument('--width', type=int, required=True)
    client.add_argument('--height', type=int, required=True)
    client.set_defaults(action=replay)
    args = parser.parse_args()
    try:
        args.action(args)
    except (OSError, StreamError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
