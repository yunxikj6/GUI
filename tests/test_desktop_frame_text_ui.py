"""Cover text in the frame viewer before any simulated keystroke leaves the browser."""
import re
import unittest

from test_browser import BROWSER, ONE_PIXEL, expect, sync_playwright
from test_desktop_clipboard_ui import ViewerHandler
from test_desktop_entry import serving


@unittest.skipUnless(sync_playwright and BROWSER, "requires Chromium and Playwright")
class FrameText(unittest.TestCase):
    def test_whole_draft_paste_delivery_uncertainty_and_handback(self):
        with serving(None, ViewerHandler) as server, sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER)
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"],
                                          viewport={"width": 320, "height": 620})
            page = context.new_page()
            actions, inputs, deferred = [], [], []

            def action(route):
                name = route.request.url.rsplit("/", 1)[1]
                actions.append(name)
                owned = name == "takeover"
                route.fulfill(json={"kind": "x11", "running": True, "agentPausable": True,
                              "controller": "human" if owned else "agent", "yours": owned})

            def deliver(route):
                payload = route.request.post_data_json
                inputs.append(payload)
                if payload["value"] == "ASCII once!":
                    route.fulfill(status=502, json={"error": "连接中断"})
                elif payload["value"] == "revised draft":
                    deferred.append(route)
                elif payload["value"] == "no acknowledgement":
                    route.fulfill(json={})
                else:
                    route.fulfill(json={"ok": True})

            page.route(re.compile(r"/desktop/(start|takeover|handback)$"), action)
            page.route("**/desktop/input", deliver)
            page.route("**/desktop/frame", lambda r: r.fulfill(content_type="image/png", body=ONE_PIXEL))
            page.goto(f"http://127.0.0.1:{server.server_port}/desktop/")
            expect(page.locator("#status")).to_have_text("正在观看")
            page.get_by_role("button", name="逐帧查看").click()
            page.get_by_role("button", name="接管输入").click()
            field = page.get_by_role("textbox", name="逐帧输入文字")
            note = page.locator("#typingStatus")
            send = page.get_by_role("button", name="发送文字")
            for draft in ("prefix 月🙂 suffix", "first\nsecond", "A" * 2048 + "月"):
                field.fill("")
                page.evaluate("s => navigator.clipboard.writeText(s)", draft)
                field.focus()
                page.keyboard.press("Control+v")
                expect(field).to_have_value(draft)
                field.press("Enter")
                expect(note).to_contain_text("整段文字未发送")
                expect(note).to_contain_text("剪贴板")
                self.assertNotIn("输入法", note.inner_text())
                expect(field).to_have_value(draft)
            self.assertEqual(inputs, [])

            field.fill("")
            field.focus()
            field.evaluate("""el => {
                window.compositionLog = [];
                for (const name of ['compositionstart', 'compositionend'])
                    el.addEventListener(name, e => compositionLog.push([e.type, e.isTrusted]));
            }""")
            cdp = context.new_cdp_session(page)
            cdp.send("Input.imeSetComposition", {"text": "nihao", "selectionStart": 5, "selectionEnd": 5})
            expect(field).to_have_value("nihao")
            expect(send).to_be_disabled()
            pos = send.bounding_box()
            page.mouse.click(pos["x"] + pos["width"] / 2, pos["y"] + pos["height"] / 2)
            expect(field).to_be_focused()
            self.assertEqual(page.evaluate("compositionLog"), [["compositionstart", True]])
            self.assertEqual(inputs, [])
            cdp.send("Input.insertText", {"text": "finished"})
            expect(field).to_have_value("finished")
            expect(send).to_be_enabled()
            self.assertEqual(page.evaluate("compositionLog.map(([name]) => name)"), ["compositionstart", "compositionend"])
            field.press("Enter")
            expect(note).to_contain_text("已发出按键")
            expect(field).to_have_value("")
            expect(send).to_be_disabled()
            self.assertEqual(inputs, [{"key": "type", "value": "finished"}])

            # A native double click is one submission even with an immediate response.
            field.fill("double click")
            send.dblclick()
            expect(field).to_have_value("")
            self.assertEqual([p["value"] for p in inputs], ["finished", "double click"])
            inputs.clear()

            field.fill("revised draft")
            send.click()
            expect(send).to_be_disabled()
            field.fill("other")
            field.fill("revised draft")
            deferred.pop().fulfill(json={"ok": True})
            expect(send).to_be_enabled()
            expect(field).to_have_value("revised draft")
            self.assertEqual(inputs, [{"key": "type", "value": "revised draft"}])
            inputs.clear()

            field.fill("no acknowledgement")
            send.dblclick()
            expect(note).to_contain_text("无法确认")
            expect(field).to_have_value("no acknowledgement")
            self.assertEqual(inputs, [{"key": "type", "value": "no acknowledgement"}])
            inputs.clear()
            field.fill("ASCII once!")
            field.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
            self.assertEqual(inputs, [])
            field.press("Enter")
            expect(note).to_contain_text("无法确认文字是否已送达")
            expect(field).to_have_value("ASCII once!")
            with page.expect_response("**/desktop/frame"):
                pass  # A later screenshot keeps its own status and never retries the text.
            expect(note).to_contain_text("无法确认文字是否已送达")
            self.assertEqual(inputs, [{"key": "type", "value": "ASCII once!"}])
            page.get_by_role("button", name="交还输入").click()
            page.get_by_role("button", name="接管输入").click()
            expect(field).to_have_value("")
            expect(note).to_have_text("")
            field.fill("new draft")
            page.get_by_role("button", name="重新连接").click()
            expect(page.locator("#status")).to_have_text("正在观看")
            expect(page.locator("#typing")).to_have_value("")
            self.assertEqual(actions, ["start", "takeover", "handback", "takeover", "start"])
            browser.close()
