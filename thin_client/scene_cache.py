"""Retain original graphics commands through the last locally rendered frame in memory."""
from collections import deque
import hashlib
import io
import zlib


class SceneMismatch(ValueError):
    pass


class _Blocks:
    """Inflate one internally generated command block at a time on the renderer thread."""

    def __init__(self, blocks, tail, cancelled):
        self.cancelled = cancelled
        self.blocks = deque(blocks)
        self.tail = tail
        self.current = io.BytesIO()

    def read(self, size):
        if self.cancelled():
            raise InterruptedError('窗口已关闭。')
        if size <= 0:
            return b''
        part = self.current.read(size)
        if part:
            return part
        if self.blocks:
            self.current = io.BytesIO(zlib.decompress(self.blocks.popleft()))
        elif self.tail:
            self.current = io.BytesIO(self.tail)
            self.tail = None
        return self.current.read(size)


class SceneCache:
    """Tee a verified stream on its one GPU thread. Commit only after native presentation.

    A byte or memory limit retires the archive for this connection. A newer
    source state must never be replaced with an older one to fit the budget.
    No file, screen image, connection credential or input event is retained here.
    """

    BLOCK = 128 << 10

    def __init__(self, source, *, max_encoded=24 << 20, max_original=256 << 20):
        self.source = source
        self.max_encoded = max_encoded
        self.max_original = max_original
        self.encoded = self.original = 0
        self.blocks = []
        self.pending = bytearray()
        self.snapshot = None
        self.active = True

    def _retire(self):
        self.active = False
        self.blocks.clear()
        self.pending.clear()
        self.snapshot = None

    def read(self, size):
        data = self.source.read(size)
        if not self.active or not data:
            return data
        self.original += len(data)
        if self.original > self.max_original:
            self._retire()
            return data
        view = memoryview(data)
        while view:
            count = min(len(view), self.BLOCK - len(self.pending))
            self.pending.extend(view[:count])
            view = view[count:]
            if len(self.pending) == self.BLOCK:
                block = zlib.compress(self.pending, 1)
                self.blocks.append(block)
                self.encoded += len(block)
                self.pending.clear()
            if self.encoded + len(self.pending) > self.max_encoded:
                self._retire()
                break
        return data

    def presented(self, width, height, pixels):
        if self.active:
            self.snapshot = (len(self.blocks), bytes(self.pending), width, height,
                             hashlib.sha256(pixels).digest())

    def redraw(self, replay, present, cancelled=lambda: False):
        """After disconnect, rebuild the last complete frame in a fresh local GPU context."""
        self.source = None
        if self.snapshot is None:
            self._retire()
            return False
        count, tail, width, height, digest = self.snapshot
        commands = _Blocks(self.blocks[:count], tail, cancelled)
        self._retire()
        last = None

        def keep(w, h, pixels):
            nonlocal last
            last = (w, h, pixels)

        replay(commands, keep)
        if (last is None or last[:2] != (width, height)
                or hashlib.sha256(last[2]).digest() != digest):
            raise SceneMismatch('本机重新执行绘图的结果与断开前的最后画面不一致。')
        present(*last)
        return True
