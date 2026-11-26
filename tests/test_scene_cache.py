import io
import unittest
import threading
import time

from thin_client.gnome_desktop import HEIGHT, WIDTH, present_session
from thin_client.scene_cache import SceneCache, SceneMismatch


class SceneCacheTests(unittest.TestCase):
    def test_replay_stops_at_last_presented_command_across_blocks(self):
        body = bytes(range(251)) * 1250
        cache = SceneCache(io.BytesIO(body + b'partial next command'))
        self.assertEqual(cache.read(len(body)), body)
        cache.presented(2, 1, b'last RGB')
        cache.read(99)
        frames = []

        def replay(commands, emit):
            self.assertEqual(b''.join(iter(lambda: commands.read(3907), b'')), body)
            emit(2, 1, b'previous')
            emit(2, 1, b'last RGB')

        self.assertTrue(cache.redraw(replay, lambda *args: frames.append(args)))
        self.assertEqual(frames, [(2, 1, b'last RGB')])
        self.assertIsNone(cache.source)
        self.assertFalse(cache.active)

    def test_expired_or_divergent_scene_is_never_published(self):
        for limit in (dict(max_original=10), dict(max_encoded=10)):
            with self.subTest(limit=limit):
                cache = SceneCache(io.BytesIO(b'a'*12), **limit)
                cache.read(8)
                cache.presented(1, 1, b'state A')
                cache.read(4)
                cache.presented(1, 1, b'state B')
                self.assertFalse(cache.redraw(self.fail, self.fail))
        cache = SceneCache(io.BytesIO(b'p'))
        cache.read(1)
        cache.presented(1, 1, b'old')
        with self.assertRaises(SceneMismatch):
            cache.redraw(lambda _s, emit: emit(1, 1, b'other'), self.fail)

    def test_broken_peer_is_detached_before_last_scene_is_repainted(self):
        detached = threading.Event()
        rebuilt = threading.Event()
        published = []
        titles = []
        class Peer:
            def close(self):
                detached.set()
            def pop_unavailable(self):
                return None
            def click(self, *_):
                raise AssertionError('detached input')
        class Window:
            def __init__(self, title, width, height, send):
                self.send = send
                self.until = time.monotonic() + 2
            def present(self, pixels):
                published.append(pixels)
                return True
            def set_title(self, title):
                titles.append(title)
            def pump(self):
                if rebuilt.is_set() and any('只读' in value for value in titles):
                    self.send(('click', 0, 0, 'left'))
                    return False
                return time.monotonic() < self.until
            def close(self):
                pass

        def initial(peer, present):
            present(WIDTH, HEIGHT, b'first')
            raise ConnectionResetError('detached')

        def restore(present, cancelled):
            self.assertFalse(cancelled())
            self.assertTrue(detached.wait(1))
            present(WIDTH, HEIGHT, b'rebuilt')
            rebuilt.set()

        present_session(Peer, initial, Window, retain_on_disconnect=True, restore=restore)
        self.assertTrue(rebuilt.is_set())
        self.assertEqual(published, [b'first', b'rebuilt'])
        self.assertTrue(any('只读' in value for value in titles))

    def test_window_close_cancels_reexecution_at_the_next_command_read(self):
        cache = SceneCache(io.BytesIO(b'abc'))
        cache.read(3)
        cache.presented(1, 1, b'pixels')
        with self.assertRaises(InterruptedError):
            cache.redraw(lambda r, _: r.read(2), self.fail, cancelled=lambda: True)
