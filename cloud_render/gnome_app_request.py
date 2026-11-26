"""Notify the isolated shell source when the user activates an application."""
from pathlib import Path
import socket
import sys


def activate(runtime, application):
    if application not in ('chatgpt', 'claude', 'wayland'):
        raise ValueError('未知桌面应用。')
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(str(runtime / 'activation'))
        client.send(application.encode('ascii'))


if __name__ == '__main__':
    activate(Path(__file__).resolve().parent, sys.argv[1])
