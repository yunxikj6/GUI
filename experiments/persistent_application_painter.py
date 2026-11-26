"""One bounded, local CanvasKit worker per trusted application render session."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from cloud_render.wire import encode_message
from experiments.application_surface import SurfaceError


class OriginalPicturePainter:
    """Start via a context manager and call on a single renderer thread."""
    def __init__(self, *, node='node', vendor=None, timeout=30):
        if not 0 < timeout <= 120:
            raise ValueError('invalid drawing deadline')
        self.node = node
        self.vendor = vendor or Path(__file__).resolve().parents[1]/'thin_client/vendor'
        self.timeout = timeout
        self.process = self.directory = None

    def __enter__(self):
        keep = ('SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','PATH','SYSTEMDRIVE','USERPROFILE','LOCALAPPDATA')
        env = {k:v for k,v in os.environ.items() if k.upper() in keep}
        script = Path(__file__).with_name('rasterize_application_frame.cjs')
        self.directory = tempfile.TemporaryDirectory(prefix='original-app-render-')
        env.update(TMP=self.directory.name, TEMP=self.directory.name, TMPDIR=self.directory.name)
        try:
            self.process = subprocess.Popen([str(self.node), str(script), str(self.vendor), '--pipe'],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except BaseException:
            self.directory.cleanup()
            raise
        return self

    def __call__(self, frame):
        process = self.process
        if process is None or process.poll() is not None:
            raise SurfaceError('local application graphics process unavailable')
        expected = frame.width * frame.height * 4
        if not 0 < expected <= 32 << 20:
            raise SurfaceError('invalid application surface size')
        timer = threading.Timer(self.timeout,process.kill)
        timer.daemon = True
        timer.start()
        try:
            process.stdin.write(encode_message(frame))
            process.stdin.flush()
            pixels = process.stdout.read(expected)
            if len(pixels) != expected:
                raise SurfaceError('local renderer did not complete an original picture')
            return pixels
        except (OSError, ValueError) as exc:
            process.kill()
            raise SurfaceError('local renderer failed to complete an original picture') from exc
        finally:
            timer.cancel()
            timer.join()

    def __exit__(self, *_):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            process.stdout.close()
            self.directory.cleanup()
