"""Keep a local desktop viewport through real WebSocket loss and display-mode changes."""
import socket
from unittest.mock import patch

from test_browser import expect
import test_desktop_scale_ui as screen_fixture
from test_desktop_clipboard_ui import ViewerHandler


class DesktopViewRecovery(screen_fixture.DesktopScaleFixture):
    def setUp(self):
        self.sockets = []
        normal = ViewerHandler.desktop

        def capture(handler):
            if handler.path == '/desktop/vnc':
                self.sockets.append(handler.connection)
            return normal(handler)

        intercept = patch.object(ViewerHandler, 'desktop', capture)
        intercept.start()
        self.addCleanup(intercept.stop)
        super().setUp()
        self.screen = self.page.get_by_role('main')
        self.select('original')

    def position(self):
        return self.screen.evaluate('s => [s.scrollLeft, s.scrollTop]')

    def expect_restored(self, pos, width, height, kind):
        # Wait for the new renderer to have its actual remote dimensions before comparing offsets.
        self.page.wait_for_function('''([kind,w,h]) => {
            const el = document.querySelector('main ' + kind);
            return el && el.getBoundingClientRect().width === w && el.getBoundingClientRect().height === h;
        }''', arg=[kind, width, height])
        self.page.wait_for_function('''([x,y]) => {
            const s = document.querySelector('main');
            return s.scrollLeft === Math.min(x, s.scrollWidth - s.clientWidth) &&
                   s.scrollTop === Math.min(y, s.scrollHeight - s.clientHeight);
        }''', arg=pos, timeout=3000)

    def test_viewer_reconnect_roundtrip_and_user_selecting_fit(self):
        self.swipe(1, 0)
        self.swipe(0, 1)
        before = self.position()
        self.assertTrue(0 < before[0] < 900 and 0 < before[1] < 380, before)
        for action, kind, status in [('重新连接', 'canvas', '正在观看'), ('逐帧查看', 'img', '逐帧观看'),
                                      ('切回实时', 'canvas', '正在观看')]:
            self.page.get_by_role('button', name=action).click()
            expect(self.page.locator('#status')).to_have_text(status)
            self.expect_restored(before, 1280, 800, kind)

        # A choice made while a screenshot is in flight wins over the previous original-size view.
        waiting = []
        self.page.route('**/desktop/frame', lambda route: waiting.append(route))
        with self.page.expect_request('**/desktop/frame'):
            self.page.get_by_role('button', name='逐帧查看').click()
        self.select('fit')
        waiting[0].fulfill(content_type='image/png', body=self.frame)
        self.wait_markers([0, 1, 2, 3], original=False)
        self.assertEqual(self.position(), [0, 0])
        self.assertEqual(self.actions, ['start', 'start', 'start'])
        self.assertEqual(self.frame_inputs, [])
        self.assertTrue(all(not peer.events for peer in self.server.peers))

    def test_controlled_drop_clamps_to_new_frame_then_recovers_live(self):
        self.page.get_by_role('button', name='接管输入').click()
        self.page.get_by_role('button', name='本地平移').click()
        self.corner(1, 1, 0)
        self.corner(3, 0, 1)
        before = self.position()
        corners = [(min(x, 1024 - 8 - screen_fixture.SIDE), min(y, 640 - 8 - screen_fixture.SIDE), rgb)
                   for x, y, rgb in screen_fixture.CORNERS]
        with patch.multiple(screen_fixture, WIDTH=1024, HEIGHT=640, CORNERS=corners):
            self.frame = screen_fixture.frame_png()
        self.sockets[-1].shutdown(socket.SHUT_RDWR)
        expect(self.page.locator('#status')).to_have_text('逐帧控制')
        self.expect_restored(before, 1024, 640, 'img')
        self.wait_markers([3], original=True)
        smaller = self.position()
        self.assertTrue(smaller[0] < before[0] and smaller[1] < before[1], (before, smaller))
        self.page.get_by_role('button', name='切回实时').click()
        expect(self.page.locator('#status')).to_have_text('已接管输入')
        self.expect_restored(smaller, 1280, 800, 'canvas')
        expect(self.page.get_by_role('button', name='本地平移')).to_have_attribute('aria-pressed', 'true')
        # A different controller acquired the lease before the next reconnect.
        self.controlled = False
        self.page.get_by_role('button', name='重新连接').click()
        expect(self.page.locator('#status')).to_have_text('正在观看')
        expect(self.page.get_by_role('button', name='接管输入')).to_be_visible()
        self.expect_restored(smaller, 1280, 800, 'canvas')
        self.screen.click()
        self.page.keyboard.press('x')
        self.assertTrue(self.server.peers[-1].wait_for_update_request())
        self.assertEqual((self.actions, self.frame_inputs), (['start', 'takeover', 'start', 'start'], []))
        self.assertTrue(all(not peer.events for peer in self.server.peers))
