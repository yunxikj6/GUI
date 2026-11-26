import contextlib
import os
import shutil
import socket
import threading
import unittest

from cloud_render.wire import CONTROL, SHIFT, INPUT_TYPES, Click, Frame, InputAck, KeyPress, Layer, TextCommit, read_message, write_message
from thin_client.viewer import DisplaySession, ViewerServer
from test_chromium_capture import SOLID_SKP

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

BROWSER = os.environ.get('CLOUD_WORKSPACE_TEST_CHROMIUM') or shutil.which('chromium') or shutil.which('google-chrome')


@contextlib.contextmanager
def browser_viewer(*, hold=False, reject_first=False):
    local, peer = socket.socketpair()
    incoming = peer.makefile('rwb')
    def current(seq):
        return Frame(seq, 320, 240, (Layer(0, 0, 320, 240, SOLID_SKP),))
    write_message(incoming, current(1))
    session = DisplaySession(local)
    release = threading.Event()
    if not hold:
        release.set()
    events = []
    sequence = 1
    lock = threading.Lock()
    def publish(seq):
        nonlocal sequence
        with lock:
            write_message(incoming, current(seq))
            sequence = seq
    def source():
        nonlocal sequence
        try:
            while (e := read_message(incoming, expected=INPUT_TYPES)) is not None:
                events.append(e)
                release.wait(6)
                with lock:
                    write_message(incoming, InputAck(e.sequence, not reject_first or len(events) != 1))
                    sequence += 1
                    write_message(incoming, current(sequence))
        except OSError:
            pass
    worker = threading.Thread(target=source, daemon=True)
    worker.start()
    try:
        with ViewerServer(session) as server:
            http = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
            http.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=BROWSER, headless=True, chromium_sandbox=True)
                    context = browser.new_context(viewport={'width': 320, 'height': 240})
                    context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=server.origin)
                    page = context.new_page()
                    page.goto(server.url, wait_until='domcontentloaded')
                    expect(page.locator('#keyboard')).to_be_focused()
                    yield page, context, events, release, publish
                    browser.close()
            finally:
                release.set()
                server.shutdown()
                http.join(2)
    finally:
        release.set()
        session.close()
        worker.join(3)
        incoming.close()
        peer.close()


@unittest.skipUnless(sync_playwright and BROWSER, 'requires Chromium and Playwright')
class BrowserKeyboard(unittest.TestCase):
    def test_native_ime_cancel_and_commit_once_and_real_clipboard_paste(self):
        with browser_viewer() as (page, ctx, events, _, _publish):
            field = page.locator('#keyboard')
            cdp = ctx.new_cdp_session(page)
            cdp.send('Input.imeSetComposition', {'text': 'quxiao', 'selectionStart': 6, 'selectionEnd': 6})
            expect(field).to_have_value('quxiao')
            cdp.send('Input.imeSetComposition', {'text': '', 'selectionStart': 0, 'selectionEnd': 0})
            expect(field).to_have_value('')
            self.assertEqual(events, [])
            cdp.send('Input.imeSetComposition', {'text': 'nihao', 'selectionStart': 5, 'selectionEnd': 5})
            expect(field).to_have_value('nihao')
            self.assertEqual(events, [])
            with page.expect_response(lambda r: r.url.endswith('/input') and r.status == 204):
                cdp.send('Input.insertText', {'text': '你好🐢'})
            expect(field).to_have_value('')
            page.evaluate("navigator.clipboard.writeText('粘贴✓\\n表\\t格')")
            with page.expect_response(lambda r: r.url.endswith('/input') and r.status == 204):
                page.keyboard.press('Control+v')
            self.assertEqual(events, [TextCommit(1, '你好🐢'), TextCommit(2, '粘贴✓\n表\t格')])
            expect(field).to_have_value('')

    def test_fast_text_and_shortcuts_wait_for_ack_and_following_scene(self):
        with browser_viewer(hold=True) as (page, _, events, release, _publish):
            page.keyboard.type('ab')
            page.keyboard.press('Control+a')
            page.keyboard.press('Shift+ArrowLeft')
            page.keyboard.press('Backspace')
            page.keyboard.press('Enter')
            page.wait_for_timeout(80)
            self.assertEqual(events, [TextCommit(1, 'a')])
            release.set()
            expect(page.locator('canvas')).not_to_have_attribute('aria-busy', 'true')
            self.assertEqual(events, [TextCommit(1, 'a'), TextCommit(2, 'b'), KeyPress(3, 'a', CONTROL),
                                      KeyPress(4, 'ArrowLeft', SHIFT), KeyPress(5, 'Backspace'), KeyPress(6, 'Enter')])
            handled = page.locator('#keyboard').evaluate('''field => {
              const event = new KeyboardEvent('keydown', {key:'F4', altKey:true, bubbles:true, cancelable:true});
              field.dispatchEvent(event); return event.defaultPrevented;
            }''')
            self.assertFalse(handled)

    def test_stale_cloud_focus_does_not_retarget_text_or_the_queued_enter(self):
        with browser_viewer(hold=True, reject_first=True) as (page, _, events, release, _publish):
            page.keyboard.type('xyz')
            page.keyboard.press('Enter')
            release.set()
            expect(page.get_by_role('status')).to_contain_text('未执行')
            self.assertEqual(events, [TextCommit(1, 'x')])
            page.keyboard.press('Tab')
            expect(page.locator('canvas')).not_to_have_attribute('aria-busy', 'true')
            self.assertEqual(events, [TextCommit(1, 'x'), KeyPress(2, 'Tab')])

    def test_composition_survives_ambient_animation_and_commits_to_original_sequence(self):
        with browser_viewer() as (page, ctx, events, _, publish):
            field = page.locator('#keyboard')
            cdp = ctx.new_cdp_session(page)
            cdp.send('Input.imeSetComposition', {'text': 'ni', 'selectionStart': 2, 'selectionEnd': 2})
            expect(field).to_have_value('ni')
            with page.expect_request(lambda r: '/frame?after=2' in r.url):
                publish(2)
            with page.expect_request(lambda r: '/frame?after=3' in r.url):
                publish(3)
            with page.expect_response(lambda r: r.url.endswith('/input') and r.status == 204) as committed:
                cdp.send('Input.insertText', {'text': '你'})
            self.assertEqual(committed.value.headers['x-next-scene'], '4')
            expect(field).to_have_value('')
            self.assertEqual(events, [TextCommit(1, '你')])
            with page.expect_response(lambda r: r.url.endswith('/input') and r.status == 204):
                page.keyboard.insert_text('好')
            self.assertEqual(events, [TextCommit(1, '你'), TextCommit(4, '好')])

    def test_cloud_rejects_composition_if_the_original_focus_was_lost(self):
        with browser_viewer(reject_first=True) as (page, ctx, events, _, publish):
            cdp = ctx.new_cdp_session(page)
            cdp.send('Input.imeSetComposition', {'text': 'ni', 'selectionStart': 2, 'selectionEnd': 2})
            with page.expect_request(lambda r: '/frame?after=2' in r.url):
                publish(2)
            with page.expect_response(lambda r: r.url.endswith('/input') and r.status == 409):
                cdp.send('Input.insertText', {'text': '你'})
            expect(page.get_by_role('status')).to_contain_text('未执行')
            self.assertEqual(events, [TextCommit(1, '你')])

    def test_old_composition_cannot_commit_into_a_new_pointer_target(self):
        with browser_viewer() as (page, ctx, events, _, _publish):
            field = page.locator('#keyboard')
            cdp = ctx.new_cdp_session(page)
            cdp.send('Input.imeSetComposition', {'text': 'ni', 'selectionStart': 2, 'selectionEnd': 2})
            expect(field).to_have_value('ni')
            with page.expect_response(lambda r: r.url.endswith('/click') and r.status == 204):
                page.mouse.click(80, 100)
            cdp.send('Input.insertText', {'text': '你'})
            expect(page.get_by_role('status')).to_contain_text('未发送')
            expect(field).to_have_value('')
            self.assertEqual(events, [Click(1, 80, 100)])


if __name__ == '__main__':
    unittest.main()
