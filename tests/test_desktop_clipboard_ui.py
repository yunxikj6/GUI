"""Exercise the shipped noVNC client without a real display, cloud account or OS clipboard."""
import base64
import hashlib
import re
import struct
import threading
import zlib

import desktop
import launcher
from test_browser import BrowserFixture, ONE_PIXEL, expect


CAPS, REQUEST, NOTIFY, PROVIDE = (1 << n for n in (24, 25, 27, 28))


class ClipboardPeer:
    """Small loopback RFB 3.8 peer. Echo only text actually received on the wire."""

    def __init__(self, send, extended):
        self.send, self.extended = send, extended
        self.step, self.offered, self.received = 0, "", []
        self.echo, self.delivery = True, threading.Event()
        send(b"RFB 003.008\n")

    def extension(self, action, body=b""):
        payload = struct.pack(">I", action | 1) + body  # Format 1 is plain text.
        self.send(struct.pack(">B3xi", 3, -len(payload)) + payload)

    def offer(self, text):
        self.offered = text
        if self.extended:
            self.extension(NOTIFY)
        else:
            data = text.encode("latin-1")
            self.send(struct.pack(">B3xI", 3, len(data)) + data)

    def message(self, data):
        if self.step < 3:
            self.step += 1
            if self.step == 1:
                self.send(b"\x01\x01")  # One security type: None.
            elif self.step == 2:
                self.send(bytes(4))  # SecurityResult OK.
            else:
                init = struct.pack(">HHBBBBHHHBBB3xI", 160, 90, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0, 4)
                self.send(init + b"Test")
                if self.extended:
                    self.extension(CAPS | NOTIFY | PROVIDE | REQUEST, bytes(4))
            return
        if data[0] != 6:  # ClientCutText
            return
        size = struct.unpack_from(">i", data, 4)[0]
        if size >= 0:
            text = data[8:8 + size].decode("latin-1")
        else:
            action = struct.unpack_from(">I", data, 8)[0] & 0xff000000
            if action == NOTIFY:
                self.extension(REQUEST)
                return
            if action == REQUEST:
                text = self.offered.replace("\n", "\r\n").encode("utf-8") + b"\0"
                self.extension(PROVIDE, zlib.compress(struct.pack(">I", len(text)) + text))
                return
            if action != PROVIDE:
                return
            plain = zlib.decompressobj().decompress(data[12:])
            text = plain[4:].removesuffix(b"\0").decode("utf-8").replace("\r\n", "\n")
        self.received.append(text)
        self.delivery.set()
        if self.echo:
            self.offer(text)


class ViewerHandler(launcher.Handler):
    """The normal desktop asset handler with one native, binary-only test WebSocket."""

    def desktop(self):
        if self.path != "/desktop/vnc":
            return super().desktop()
        if not self.headers.get("Sec-WebSocket-Key") or self.headers.get("Origin") != "http://" + self.headers["Host"]:
            return self.send_error(400)
        key = self.headers["Sec-WebSocket-Key"] + desktop.WS_GUID
        self.protocol_version = "HTTP/1.1"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
        self.end_headers()
        self.close_connection = True
        lock = threading.Lock()

        def send(data):
            size = len(data)
            length = bytes([size]) if size < 126 else b"\x7e" + struct.pack(">H", size)
            with lock:
                self.connection.sendall(b"\x82" + length + data)

        peer = ClipboardPeer(send, self.server.extended)
        self.server.peers.append(peer)
        while len(header := self.rfile.read(2)) == 2:
            if header[0] & 15 == 8:  # Close acknowledgement.
                self.connection.sendall(b"\x88\x00")
                return
            if header[0] != 0x82 or not header[1] & 128:
                return
            size = header[1] & 127
            if size > 125:
                size = int.from_bytes(self.rfile.read(2 if size == 126 else 8), "big")
            if size > 65535:
                return
            mask = self.rfile.read(4)
            data = self.rfile.read(size)
            if len(mask) != 4 or len(data) != size or not data:
                return
            peer.message(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class DesktopClipboardAcceptance(BrowserFixture):
    def viewer(self, extended=True, width=1100):
        page = self.page
        server = launcher.LocalServer(("127.0.0.1", 0), ViewerHandler)
        self.addCleanup(server.server_close)
        server.workspace = None
        server.extended, server.peers = extended, []
        self.peers = server.peers
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        owned = False

        def control(route):
            nonlocal owned
            action = route.request.url.rsplit("/", 1)[1]
            if action in ("takeover", "handback"):
                owned = action == "takeover"
            route.fulfill(json={"kind": "x11", "running": True, "agentPausable": True,
                                "controller": "human" if owned else "agent", "yours": owned})

        page.route(re.compile(r"/desktop/(?:start|takeover|handback)$"), control)
        page.route("**/desktop/frame", lambda r: r.fulfill(content_type="image/png", body=ONE_PIXEL))
        page.add_init_script("""window.board = {reads: 0, writes: [], result: 'ok'};
            Object.defineProperty(navigator, 'clipboard', {value: {
                readText() { board.reads++; throw Error('unexpected system clipboard read'); },
                writeText(value) {
                    board.writes.push(value);
                    if (board.result === 'deny') return Promise.reject(new DOMException('denied','NotAllowedError'));
                    if (board.result === 'defer') return new Promise(resolve => { board.finish = resolve; });
                    return Promise.resolve();
                }
            }});""")
        page.set_viewport_size({"width": width, "height": 740})
        page.goto(f"http://127.0.0.1:{server.server_port}/desktop/")
        expect(page.locator("#status")).to_have_text("正在观看")
        self.toggle = page.locator("#clipboardToggle")
        self.outgoing = page.locator("#clipboardSendText")
        self.incoming = page.locator("#clipboardRemoteText")
        self.copy = page.locator("#clipboardCopy")
        self.note = page.locator("#clipboardStatus")

    def take_control(self):
        self.page.locator("#control").click()
        expect(self.toggle).to_be_visible()
        self.toggle.click()
        expect(self.outgoing).to_be_focused()

    def test_explicit_bidirectional_copy_and_permission_refusal_on_mobile(self):
        self.viewer(width=320)
        page = self.page
        expect(self.toggle).to_be_hidden()
        self.peers[-1].offer("观看中")
        self.take_control()
        expect(self.incoming).to_have_value("")
        expect(self.copy).to_be_disabled()
        local, remote = "  本地草稿\n🙂  ", "远端选段\n第二行"
        self.outgoing.fill(local)
        self.peers[-1].offer(remote)
        expect(self.incoming).to_have_value(remote)
        expect(self.outgoing).to_have_value(local)
        self.assertEqual(self.peers[-1].received, [])
        self.assertEqual(page.evaluate("[board.reads, board.writes]"), [0, []])
        page.locator("#clipboardSend").click()
        expect(self.incoming).to_have_value(local)  # Confirmed by the RFB peer echo.
        self.assertEqual(self.peers[-1].received, [local])
        self.assertEqual(page.evaluate("board.writes"), [])
        self.peers[-1].offer(remote)
        expect(self.incoming).to_have_value(remote)
        page.evaluate("board.result = 'deny'")
        self.copy.click()
        expect(self.note).to_contain_text("选中")
        expect(self.incoming).to_be_focused()
        self.assertEqual(self.incoming.evaluate("e=>e.value.slice(e.selectionStart,e.selectionEnd)"), remote)
        page.evaluate("board.result = 'ok'")
        self.copy.click()
        expect(self.note).to_contain_text("已复制")
        self.assertEqual(page.evaluate("[board.reads, board.writes]"), [0, [remote, remote]])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        for element in (self.toggle, self.outgoing, self.incoming, page.locator("#clipboardSend"), self.copy):
            self.assertTrue(element.evaluate("e=>{const b=e.getBoundingClientRect();return b.left>=0 && b.right<=innerWidth}"))

    def test_handback_frames_and_reconnect_clear_old_session_and_late_copy_feedback(self):
        self.viewer()
        page = self.page
        self.take_control()
        self.outgoing.fill("旧接管会话的文本")
        page.locator("#clipboardSend").click()
        expect(self.incoming).to_have_value("旧接管会话的文本")
        # Leave the noVNC client's last offered text intact: this peer does not echo it back.
        prior = self.peers[-1]
        prior.echo = False
        prior.delivery.clear()
        self.outgoing.fill("这份内容仅供旧会话")
        page.locator("#clipboardSend").click()
        self.assertTrue(prior.delivery.wait(3))
        self.assertEqual(prior.received[-1], "这份内容仅供旧会话")
        page.evaluate("board.result = 'defer'")
        self.copy.click()
        page.locator("#control").click()
        expect(page.locator("#status")).to_have_text("正在观看")
        expect(self.toggle).to_be_hidden()
        self.take_control()
        self.assertGreaterEqual(len(self.peers), 2)
        expect(self.outgoing).to_have_value("")
        expect(self.incoming).to_have_value("")
        self.peers[-1].extension(REQUEST)  # A new lease cannot supply the prior client's offer.
        self.peers[-1].offer("本次会话")
        expect(self.incoming).to_have_value("本次会话")
        page.evaluate("board.finish()")
        expect(self.note).to_contain_text("收到")
        self.assertEqual(self.peers[-1].received, [])

        for entry, back, target in [("#mode", "#mode", "逐帧控制"), ("#retry", None, "已接管输入")]:
            self.outgoing.fill("待清除")
            prior = self.peers[-1]
            page.locator(entry).click()
            expect(page.locator("#status")).to_have_text(target)
            expect(page.locator("#clipboardPanel")).to_be_hidden()
            expect(self.outgoing).to_have_value("")
            expect(self.incoming).to_have_value("")
            if back:
                expect(self.toggle).to_be_hidden()
                page.locator(back).click()
            expect(self.toggle).to_be_visible()
            self.assertIsNot(self.peers[-1], prior)
            self.toggle.click()
            expect(self.copy).to_be_disabled()
            self.peers[-1].offer("新会话数据")
            expect(self.incoming).to_have_value("新会话数据")
        self.assertEqual(page.evaluate("[board.reads, board.writes]"), [0, ["旧接管会话的文本"]])

    def test_classic_rfb_preserves_unicode_draft_and_sends_supported_latin1(self):
        self.viewer(extended=False)
        self.take_control()
        draft = "保留中文与 emoji 🙂"
        self.outgoing.fill(draft)
        self.page.locator("#clipboardSend").click()
        expect(self.note).to_contain_text("Latin-1")
        expect(self.outgoing).to_have_value(draft)
        self.assertEqual(self.peers[-1].received, [])
        self.outgoing.fill("café\nsecond")
        self.page.locator("#clipboardSend").click()
        expect(self.incoming).to_have_value("café\nsecond")
        self.assertEqual(self.peers[-1].received, ["café\nsecond"])
