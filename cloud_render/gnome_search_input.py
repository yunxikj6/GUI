"""Pair the original GNOME Shell input method with this session's source process."""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import time


UUID = 'desktop-input@cloud-workspace.local'


class SearchBridge:
    def __init__(self, directory, stack):
        destination = directory / 'home' / '.local/share/gnome-shell/extensions' / UUID
        shutil.copytree(Path(__file__).with_name('gnome_search'), destination)
        self.stack = stack
        self.server = stack.enter_context(socket.socket(socket.AF_UNIX))
        self.server.bind(str(directory / 'text-input'))
        self.server.listen(1)
        self.server.setblocking(False)
        self.peer = None
        self.pending = bytearray()
        self.revision = 0
        self.active = False
        self.decision = None
        self.waiting = False

    def connect(self, pid):
        try:
            peer, _ = self.server.accept()
        except BlockingIOError:
            return False
        owner, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if owner != pid or uid != os.geteuid():
            peer.close()
            return False
        self.peer = self.stack.enter_context(peer)
        self.peer.settimeout(2)
        return True

    def receive(self, notify):
        part = self.peer.recv(512)
        if not part or len(self.pending) + len(part) > 1024:
            raise ValueError('原厂输入法通道已关闭或无效。')
        self.pending.extend(part)
        while b'\n' in self.pending:
            raw, _, rest = self.pending.partition(b'\n')
            self.pending = bytearray(rest)
            fields = bytes(raw).split(b' ')
            if (len(fields) == 3 and fields[0] == b'F' and fields[1].isdigit() and len(fields[1]) <= 10
                    and int(fields[1]) == self.revision + 1 and fields[2] in (b'0', b'1')):
                self.revision += 1
                self.active = fields[2] == b'1'
                notify('focus', revision=self.revision, active=self.active)
            elif fields in ([b'A', b'0'], [b'A', b'1']) and self.waiting and self.decision is None:
                self.decision = fields[1] == b'1'
            else:
                raise ValueError('原厂输入法回执无效。')

    def commit(self, revision, text, poll):
        """Send one edit. The private extension interprets null as selection of the focused buffer."""
        if not self.active or self.revision != revision:
            return False
        self.decision = None
        self.waiting = True
        self.peer.sendall(json.dumps([revision, text], ensure_ascii=False).encode('utf-8') + b'\n')
        deadline = time.monotonic() + 2
        while self.decision is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError('等待原厂输入法确认超时。')
            poll(left)
        self.waiting = False
        return self.decision
