"""Read the rendered corners and the wire coordinates of the same 1280×800 desktop."""
import base64
import re
import struct
import time
import unittest
import zlib
from contextlib import ExitStack
from unittest.mock import patch

from test_browser import BROWSER, expect, sync_playwright
from test_desktop_entry import serving
import test_desktop_clipboard_ui as rfb_fixture


WIDTH, HEIGHT, SIDE = 1280, 800, 24
# Four opaque, distinct blocks inset from the corners have known remote coordinates.
CORNERS = [(8, 8, (211, 37, 52)), (WIDTH-8-SIDE, 8, (38, 197, 72)),
           (8, HEIGHT-8-SIDE, (49, 87, 210)), (WIDTH-8-SIDE, HEIGHT-8-SIDE, (218, 154, 33))]


def frame_png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    rows = []
    for y in range(HEIGHT):
        row = bytearray(WIDTH * 3)
        for x, top, color in CORNERS:
            if top <= y < top + SIDE:
                row[x*3:(x+SIDE)*3] = bytes(color) * SIDE
        rows.append(b'\0' + row)
    header = struct.pack('>IIBBBBB', WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')


class ScreenPeer(rfb_fixture.ClipboardPeer):
    def __init__(self, send, extended):
        super().__init__(send, extended)
        self.painted = False
        self.events = []

    def message(self, data):
        if self.step == 2:
            self.step = 3
            self.send(struct.pack('>HHBBBBHHHBBB3xI', WIDTH, HEIGHT, 32, 24, 0, 1, 255, 255, 255, 0, 8, 16, 4) + b'Test')
            return
        if self.step == 3 and data[0] in (4, 5, 251):  # KeyEvent, PointerEvent, SetDesktopSize
            self.events.append(data)
        if self.step == 3 and data[0] == 3 and not self.painted:
            self.painted = True
            # Advertise resize support, so an unintended resizeSession can be observed on the wire.
            info = struct.pack('>B3xIHHHHI', 1, 1, 0, 0, WIDTH, HEIGHT, 0)
            rectangles = [struct.pack('>HHHHi', 0, 0, WIDTH, HEIGHT, -308) + info]
            for x, y, rgb in CORNERS:
                rectangles.append(struct.pack('>HHHHi', x, y, SIDE, SIDE, 0) + (bytes(rgb) + b'\0') * SIDE ** 2)
            self.send(struct.pack('>BBH', 0, 0, len(rectangles)) + b''.join(rectangles))
        super().message(data)


@unittest.skipUnless(sync_playwright and BROWSER, 'browser acceptance requires Playwright and Chromium')
class DesktopScaleFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver = sync_playwright().start()
        cls.addClassCleanup(driver.stop)
        cls.browser = driver.chromium.launch(executable_path=BROWSER)
        cls.addClassCleanup(cls.browser.close)
        cls.frame = frame_png()

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(rfb_fixture, 'ClipboardPeer', ScreenPeer))
        self.server = stack.enter_context(serving(None, rfb_fixture.ViewerHandler))
        self.context = self.browser.new_context(viewport={'width': 320, 'height': 570}, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(8000)
        self.actions, self.frame_inputs = [], []
        self.controlled = False

        def desktop_action(route):
            action = route.request.url.rsplit('/', 1)[1]
            self.actions.append(action)
            if action in ('takeover', 'handback'):
                self.controlled = action == 'takeover'
            route.fulfill(json={'kind': 'x11', 'running': True, 'agentPausable': True, 'yours': self.controlled,
                                'controller': 'human' if self.controlled else 'agent'})

        def frame_input(route):
            self.frame_inputs.append(route.request.post_data_json)
            route.fulfill(json={})

        self.page.route(re.compile(r'/desktop/(start|takeover|handback)$'), desktop_action)
        self.page.route('**/desktop/input', frame_input)
        self.page.route('**/desktop/frame', lambda r: r.fulfill(content_type='image/png', body=self.frame))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/desktop/')
        expect(self.page.locator('#status')).to_have_text('正在观看')

    def markers(self):
        # Sample the composited screenshot, including clipping and viewport offsets. No app internals.
        screenshot = base64.b64encode(self.page.screenshot()).decode('ascii')
        return self.page.evaluate('''async ([png, colors]) => {
            const buf = Uint8Array.from(atob(png), c => c.charCodeAt(0));
            const img = await createImageBitmap(new Blob([buf], {type:'image/png'}));
            const ctx = new OffscreenCanvas(img.width, img.height).getContext('2d');
            ctx.drawImage(img, 0, 0);
            const px = ctx.getImageData(0, 0, img.width, img.height).data;
            const lookup = new Map(colors.map(([r,g,b], i) => [(r<<16)+(g<<8)+b, i]));
            const bounds = colors.map(() => null);
            for (let p=0; p<px.length; p+=4) {
                const i = lookup.get((px[p]<<16)+(px[p+1]<<8)+px[p+2]);
                if (i === undefined) continue;
                const x = (p/4) % img.width, y = Math.floor(p/4/img.width);
                const b = bounds[i] ||= [x, y, x, y];
                b[0] = Math.min(b[0],x); b[1] = Math.min(b[1],y);
                b[2] = Math.max(b[2],x); b[3] = Math.max(b[3],y);
            }
            img.close(); return bounds;
        }''', [screenshot, [color for _, _, color in CORNERS]])

    def wait_markers(self, indices, original):
        end = time.monotonic() + 3
        while True:
            bounds = self.markers()
            visible = [i for i,b in enumerate(bounds) if b]
            if visible == indices and all((bounds[i][2] - bounds[i][0] + 1 == SIDE) == original for i in indices):
                return bounds
            if time.monotonic() >= end:
                self.fail(f'Expected corners {indices} at original={original}, got {bounds}')

    def select(self, value):
        scale = self.page.get_by_role('combobox', name='桌面显示比例')
        expect(scale).to_be_visible()
        scale.select_option(value)
        expect(scale).to_have_value(value)

    def swipe(self, dx, dy):
        # Trusted touch input on the actual viewer, including when the page owns the remote lease.
        box = self.page.get_by_role('main').bounding_box()
        cx, cy = box['x'] + box['width']/2, box['y'] + box['height']/2
        ax, ay = .35*box['width']*dx, .35*box['height']*dy
        touches = self.context.new_cdp_session(self.page)
        try:
            for step in range(6):
                frac = 1 - 2*step/5
                touches.send('Input.dispatchTouchEvent', {'type': 'touchStart' if step == 0 else 'touchMove',
                             'touchPoints': [{'x': cx+frac*ax, 'y': cy+frac*ay, 'id': 1}]})
            touches.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        finally:
            touches.detach()

    def corner(self, index, dx, dy):
        for _ in range(12):
            b = self.markers()[index]
            if b and b[2]-b[0]+1 == SIDE and b[3]-b[1]+1 == SIDE:
                # Momentum can continue briefly after the entire marker becomes visible.
                self.page.get_by_role('main').evaluate('''async s => {
                    let x = s.scrollLeft, y = s.scrollTop, steady = 0;
                    for (let frames=0; frames<150 && steady<5; frames++) {
                        await new Promise(requestAnimationFrame);
                        steady = x===s.scrollLeft && y===s.scrollTop ? steady+1 : 0;
                        x = s.scrollLeft; y = s.scrollTop;
                    }
                }''')
                b = self.markers()[index]
                if b and b[2]-b[0]+1 == SIDE and b[3]-b[1]+1 == SIDE:
                    return b
            self.swipe(dx, dy)
        self.fail(f'Corner {index} could not be reached by touch: {self.markers()}')


class DesktopScale(DesktopScaleFixture):
    def test_live_window_corners_and_controlled_pointer_after_touch_pan(self):
        self.wait_markers([0, 1, 2, 3], original=False)
        scale = self.page.get_by_role('combobox', name='桌面显示比例')
        expect(scale).to_have_value('fit')
        self.select('original')
        self.corner(0, -1, -1)
        self.swipe(-1, 0)  # Panning against the left boundary stays on the desktop.
        self.wait_markers([0], original=True)
        expect(self.page).to_have_url(re.compile(r'/desktop/$'))
        peer, = self.server.peers
        for i, dx, dy in [(1, 1, 0), (3, 0, 1), (2, -1, 0), (0, 0, -1)]:
            self.corner(i, dx, dy)
        self.assertTrue(peer.wait_for_update_request())
        self.assertEqual((self.actions, peer.events, self.frame_inputs), (['start'], [], []))
        self.page.get_by_role('button', name='接管输入').click()
        pan = self.page.get_by_role('button', name='本地平移')
        pan.click()
        expect(pan).to_have_attribute('aria-pressed', 'true')
        screen = self.page.get_by_role('main')
        expect(screen).to_be_focused()
        self.page.keyboard.press('Tab')
        self.assertFalse(screen.evaluate('s => s.contains(document.activeElement) && s !== document.activeElement'))
        screen.focus()
        self.page.keyboard.press('ArrowDown')
        self.wait_markers([], original=True)
        self.corner(0, 0, -1)
        self.corner(1, 1, 0)
        self.corner(3, 0, 1)
        self.page.get_by_role('main').hover()
        self.page.mouse.wheel(0, -160)
        self.wait_markers([], original=True)
        self.corner(3, 0, 1)
        self.assertTrue(peer.wait_for_update_request())
        self.assertEqual((self.server.peers, self.actions, peer.events), ([peer], ['start', 'takeover'], []))
        pan.click()
        expect(pan).to_have_attribute('aria-pressed', 'false')
        bottom_right = self.wait_markers([3], original=True)[3]
        x, y = bottom_right[:2]
        self.page.mouse.click(x+10, y+10)
        self.page.mouse.wheel(0, -130)
        self.assertTrue(peer.wait_for_update_request())
        clicks = [struct.unpack('>BBHH', pkt)[1:] for pkt in peer.events if pkt[0] == 5]
        rx, ry = CORNERS[3][:2]
        self.assertIn((1, rx+10, ry+10), clicks)
        self.assertTrue(any(mask & 8 for mask, _, _ in clicks), clicks)
        self.wait_markers([3], original=True)
        self.select('fit')
        left, top, right, bottom = self.wait_markers([0, 1, 2, 3], original=False)[1]
        self.page.mouse.click((left+right)//2, (top+bottom)//2)
        self.assertTrue(peer.wait_for_update_request())
        _, _, px, py = next(struct.unpack('>BBHH', pkt) for pkt in reversed(peer.events) if pkt[:2] == b'\x05\x01')
        rx, ry = CORNERS[1][:2]
        self.assertTrue(rx <= px < rx+SIDE and ry <= py < ry+SIDE, (px, py))
        self.assertFalse(any(pkt[0] == 251 for pkt in peer.events))
        self.assertEqual(self.server.peers, [peer])

    def test_frame_scaling_local_pan_clicks_and_return_to_live(self):
        self.page.get_by_role('button', name='逐帧查看').click()
        expect(self.page.locator('#status')).to_have_text('逐帧观看')
        self.wait_markers([0, 1, 2, 3], original=False)
        self.select('original')
        self.corner(0, -1, -1)
        self.wait_markers([0], original=True)
        self.page.get_by_role('button', name='接管输入').click()
        pan = self.page.get_by_role('button', name='本地平移')
        pan.click()
        self.page.keyboard.press('Tab')
        self.assertNotEqual(self.page.evaluate('document.activeElement.tagName'), 'IMG')
        self.corner(2, 0, 1)
        self.corner(3, 1, 0)
        self.assertEqual((self.actions, self.frame_inputs), (['start', 'takeover'], []))
        pan.click()
        x, y = self.wait_markers([3], original=True)[3][:2]
        rx, ry = CORNERS[3][:2]
        for button in (1, 4):
            with self.page.expect_request('**/desktop/input') as next_input:
                if button == 1:
                    self.page.mouse.click(x+9, y+9)
                else:
                    self.page.mouse.wheel(0, -130)
            self.assertEqual(next_input.value.post_data_json,
                             {'key':'click', 'button':button, 'x':rx+9, 'y':ry+9})
        self.wait_markers([3], original=True)
        self.page.get_by_role('button', name='切回实时').click()
        expect(self.page.locator('#status')).to_have_text('已接管输入')
        expect(self.page.get_by_role('combobox', name='桌面显示比例')).to_have_value('original')
        self.assertEqual(len(self.server.peers), 2)
        self.page.get_by_role('button', name='重新连接').click()
        expect(self.page.locator('#status')).to_have_text('已接管输入')
        expect(self.page.get_by_role('combobox', name='桌面显示比例')).to_have_value('original')
        self.select('fit')
        self.wait_markers([0, 1, 2, 3], original=False)
        self.assertEqual(self.actions, ['start', 'takeover', 'start', 'start'])
        self.assertEqual(len(self.server.peers), 3)
