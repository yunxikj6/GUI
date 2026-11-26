"""Serve original drawings from a private Chromium offline error document."""
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile

from .browser_surface import OFFLINE_TARGET, capture_offline_viewport
from .chromium_capture import CaptureError
from .service import Session, TOKEN_PATTERN, same_scene
from .wire import Click, InputAck, KeyPress, WireError, async_read_message, encode_message


_SCROLL_KEYS = {'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'}
_WATCH_ACTION = """(doc, action) => {
    if (doc !== document || !doc.hasFocus()) return null;
    let node = doc, expected;
    if (action.key) {
        expected = ['keydown', 'keyup'];
    } else {
        node = doc.getElementById('details-button');
        const hit = doc.elementFromPoint(action.x, action.y);
        if (!node || node.tagName !== 'BUTTON' || node.disabled || !node.contains(hit)) return null;
        expected = ['pointerdown', 'pointerup', 'click'];
    }
    let count = 0;
    const note = e => {
        if (!e.isTrusted || e.view !== doc.defaultView || e.type !== expected[count]) return;
        if (action.key ? e.key !== action.key : e.button !== 0 || !node.contains(e.target)) return;
        count++;
    };
    for (const type of expected) node.addEventListener(type, note, true);
    return () => {
        for (const type of expected) node.removeEventListener(type, note, true);
        return count === expected.length && doc === document && doc.hasFocus();
    };
}"""


class _NativeNavigation:
    """Only the one fresh offline browser created by run() gets this input owner."""
    def __init__(self, page):
        self.page = page
        self.active = True
        page.on('framenavigated', self.invalidate)
        page.on('popup', self.invalidate)
        page.on('close', self.invalidate)

    def invalidate(self, *_):
        self.active = False

    async def apply(self, window, event):
        if isinstance(event, KeyPress):
            if event.modifiers or event.key not in _SCROLL_KEYS:
                return False
            action = {'key': event.key}
        elif isinstance(event, Click):
            action = {'x': event.x, 'y': event.y}
        else:
            return False
        if not self.active:
            return False
        page = self.page
        session = await page.context.new_cdp_session(page)
        try:
            async def same_document():
                tree = (await session.send('Page.getFrameTree'))['frameTree']
                frame = tree['frame']
                return (self.active and not tree.get('childFrames') and page.context.pages == [page]
                        and frame['id'] == window.target_id and frame['loaderId'] == window.loader_id
                        and frame['url'] == 'chrome-error://chromewebdata/'
                        and frame.get('unreachableUrl') == OFFLINE_TARGET)

            if not await same_document():
                return False
            document = await page.evaluate_handle('document')
            try:
                witness = await document.evaluate_handle(_WATCH_ACTION, action)
                try:
                    if not await witness.evaluate('f => typeof f === "function"') or not await same_document():
                        return False
                    # Delivery uses Chromium's input path; the original DOM listeners
                    # above witness the target and event sequence without changing behavior.
                    if isinstance(event, Click):
                        await page.mouse.click(event.x, event.y)
                    else:
                        await page.keyboard.press(event.key)
                finally:
                    try:
                        observed = await witness.evaluate('f => f ? f() : false')
                    finally:
                        await witness.dispose()
                return observed and await same_document()
            finally:
                await document.dispose()
        finally:
            await session.detach()


class OfflineViewport(Session):
    def __init__(self, page, token, *, browser_pid, executable):
        super().__init__(page, capture_offline_viewport, token, poll_seconds=.5)
        self.browser_pid = browser_pid
        self.executable = executable
        self.window = None
        self.navigation = None
        self.viewer_closed = asyncio.Event()
        self.failure_kind = None

    async def wait_for_viewer(self):
        await self.viewer_closed.wait()
        if self.failure_kind is not None:
            raise CaptureError('The paired browser viewport failed: ' + self.failure_kind)

    async def next_frame(self):
        if self.sequence == 0xffffffff:
            raise WireError('browser viewport sequence exhausted')
        window, frame = await self.capture(self.page, self.sequence + 1, browser_pid=self.browser_pid,
                                           executable=self.executable)
        if self.window is not None and self.window != window:
            raise CaptureError('browser document changed during the session')
        self.window = window
        return frame

    async def _publish(self, writer, frame):
        writer.write(encode_message(frame))
        await asyncio.wait_for(writer.drain(), 10)
        self.sequence = frame.sequence

    async def serve_viewer(self, reader, writer):
        incoming = None
        try:
            current = await self.next_frame()
            await self._publish(writer, current)
            incoming = asyncio.create_task(async_read_message(reader, expected=(KeyPress, Click)))
            while not self.page.is_closed():
                ready, _ = await asyncio.wait({incoming}, timeout=self.poll_seconds)
                event = incoming.result() if ready else None
                if ready and event is None:
                    return
                candidate = await self.next_frame()
                if ready:
                    applied = (event.sequence == current.sequence and self.navigation is not None
                               and same_scene(candidate, current)
                               and await self.navigation.apply(self.window, event))
                    writer.write(encode_message(InputAck(event.sequence, applied)))
                    # Every consumed action gets a new drawing sequence, even at a scroll limit.
                    candidate = await self.next_frame() if applied else candidate
                    await self._publish(writer, candidate)
                    current = candidate
                    incoming = asyncio.create_task(async_read_message(reader, expected=(KeyPress, Click)))
                elif not same_scene(candidate, current):
                    await self._publish(writer, candidate)
                    current = candidate
        except Exception as exc:
            # Session.accept closes an individual failed connection. The one-shot
            # CLI also needs to distinguish that outcome from the viewer's EOF.
            if isinstance(exc, CaptureError) or not self.page.is_closed():
                self.failure_kind = type(exc).__name__
            raise
        finally:
            self.viewer_closed.set()
            if incoming is not None:
                incoming.cancel()
                with suppress(asyncio.CancelledError, EOFError, OSError, WireError):
                    await incoming


def serve_offline_viewport(page, token, *, browser_pid, executable, port=17328):
    """Subscribe to an owner's existing offline view. This entry is always read-only."""
    service = OfflineViewport(page, token, browser_pid=browser_pid, executable=executable)
    return _listen(service, port)


@asynccontextmanager
async def _listen(service, port):
    # Bind the actual document before publishing the port or accepting a pairing.
    await service.next_frame()
    server = await asyncio.start_server(service.accept, '127.0.0.1', port, limit=1024, backlog=8)
    async with server:
        try:
            yield server
        finally:
            await service.close()


async def _window_id(browser, context, page):
    target = await context.new_cdp_session(page)
    try:
        info = (await target.send('Target.getTargetInfo'))['targetInfo']
        result = await browser.send('Browser.getWindowForTarget', {'targetId': info['targetId']})
        return result['windowId']
    finally:
        await target.detach()


async def _gio(env, *args):
    proc = await asyncio.create_subprocess_exec('/usr/bin/gio', *args, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        if proc.returncode:
            raise CaptureError('system browser activation failed')
        return out
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def run(executable, port, pairing, size):
    from playwright.async_api import async_playwright
    from .system_browser import DESKTOP_ID, SystemBrowserRegistration

    platform = 'wayland' if os.environ.get('WAYLAND_DISPLAY') else 'x11'
    with tempfile.TemporaryDirectory(prefix='cloud-browser-') as private, socket.socket() as offline:
        # Reserve a closed local port; Chromium itself paints the resulting network error.
        # Holding the bound socket prevents another process from starting a proxy there.
        offline.bind(('127.0.0.1', 0))
        proxy = {'server': f'http://127.0.0.1:{offline.getsockname()[1]}'}
        root = Path(private)
        registration = SystemBrowserRegistration.create(root / 'session', executable=executable, platform=platform)
        # Neither the startup browser nor GIO may consult the host user's defaults/profile.
        keys = ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'XDG_CURRENT_DESKTOP',
                'XDG_SESSION_TYPE', 'DBUS_SESSION_BUS_ADDRESS', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE',
                'LIBGL_ALWAYS_SOFTWARE')
        display_env = {key: os.environ[key] for key in keys if key in os.environ}
        display_env.update(HOME=private, PATH='/usr/local/bin:/usr/bin:/bin', XDG_CONFIG_HOME=str(root / '.config'),
                           XDG_DATA_HOME=str(root / '.local/share'), XDG_CACHE_HOME=str(root / '.cache'))
        env = registration.environment(display_env)
        if 'DISPLAY' in env and 'XAUTHORITY' not in env and (Path.home()/'.Xauthority').is_file():
            env['XAUTHORITY'] = str(Path.home()/'.Xauthority')
        mime = await _gio(dict(env, LC_ALL='C', LANGUAGE='C'), 'mime', 'x-scheme-handler/https')
        if not mime.splitlines() or not mime.splitlines()[0].endswith(b': ' + DESKTOP_ID.encode()):
            raise CaptureError('the session browser is not the registered HTTPS handler')
        async with async_playwright() as owner:
            context = await owner.chromium.launch_persistent_context(
                str(registration.profile), executable_path=str(executable), headless=False, chromium_sandbox=True,
                no_viewport=True, proxy=proxy, env=env,
                args=[*registration.arguments[1:], f'--window-size={size[0]},{size[1]}'])
            try:
                (blank,) = context.pages
                if blank.url != 'about:blank':
                    raise CaptureError('unrecognized private startup page')
                browser = await context.browser.new_browser_cdp_session()
                try:
                    first = await _window_id(browser, context, blank)
                    async with context.expect_page(timeout=12000) as second:
                        await _gio(env, 'open', OFFLINE_TARGET)
                    page = await second.value
                    target = await context.new_cdp_session(page)
                    try:
                        async with asyncio.timeout(10):
                            while True:
                                document = (await target.send('Page.getFrameTree'))['frameTree']['frame']
                                if (document['url'] == 'chrome-error://chromewebdata/' and
                                        document.get('unreachableUrl') == OFFLINE_TARGET):
                                    break
                                await asyncio.sleep(.05)
                    finally:
                        await target.detach()
                    if await _window_id(browser, context, page) == first or set(context.pages) != {blank, page}:
                        raise CaptureError('the system handler did not open a distinct browser window')
                    await blank.close()
                    processes = (await browser.send('SystemInfo.getProcessInfo'))['processInfo']
                finally:
                    await browser.detach()
                (pid,) = [entry['id'] for entry in processes if entry['type'] == 'browser']
                service = OfflineViewport(page, pairing, browser_pid=pid, executable=executable)
                service.navigation = _NativeNavigation(page)
                async with _listen(service, port) as server:
                    print(f'云端 Chromium 视口端口 {server.sockets[0].getsockname()[1]}。', flush=True)
                    page.on('close', lambda *_: service.viewer_closed.set())
                    if not page.is_closed():
                        await service.wait_for_viewer()
            finally:
                await context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='从云端原版 Chromium 的离线错误页读取实时绘图。')
    parser.add_argument('--port', type=int, default=17328)
    parser.add_argument('--executable', type=Path, default=Path('/usr/lib/chromium/chromium'))
    parser.add_argument('--window-size', nargs=2, type=int, default=(1000, 900), metavar=('WIDTH', 'HEIGHT'))
    args = parser.parse_args(argv)
    if not all(240 <= side <= 4096 for side in args.window_size):
        parser.error('窗口宽高必须介于 240 与 4096 像素。')
    if not 0 <= args.port <= 65535 or not args.executable.is_absolute():
        parser.error('需要有效的私有端口和浏览器绝对路径。')
    pairing = os.environ.pop('CLOUDWORKSPACE_RENDER_TOKEN', None)
    if pairing is None:
        if not sys.stderr.isatty():
            parser.error('后台运行需要 CLOUDWORKSPACE_RENDER_TOKEN。')
        pairing = secrets.token_urlsafe(32)
        print('本次私有配对码 ' + pairing, file=sys.stderr)
    if not TOKEN_PATTERN.fullmatch(pairing):
        parser.error('需要有效的私有配对码。')
    try:
        asyncio.run(run(args.executable, args.port, pairing, args.window_size))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f'云端 Chromium 视口已关闭（{type(exc).__name__}）。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
