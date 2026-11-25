import base64
import hashlib
from io import BytesIO
import json
import unittest
from unittest.mock import Mock

import desktop


class Request:
    def __init__(self, path, method="GET", body=b""):
        self.path, self.command = path, method
        self.headers = {"Content-Length": str(len(body))}
        self.rfile, self.wfile = BytesIO(body), BytesIO()
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self.instance = Mock(machine_id="m_demo", key="example")

    def test_vendor_resource_routing(self):
        for path in ("/desktop/", "/desktop/novnc/core/rfb.js", "/desktop/novnc/vendor/pako/lib/zlib/inflate.js"):
            with self.subTest(path=path):
                r = Request(path)
                desktop.handle_desktop(r, self.instance)
                self.assertEqual(r.status, 200)
                self.assertGreater(len(r.wfile.getvalue()), 200)
        r = Request("/desktop/novnc/../desktop.py")
        desktop.handle_desktop(r, self.instance)
        self.assertEqual(r.status, 404)
        self.instance.api.assert_not_called()

    def test_start_uses_provider_control_only(self):
        self.instance.api.return_value = {"installed": True, "running": True, "secret": "never echoed"}
        r = Request("/desktop/start", "POST")
        desktop.handle_desktop(r, self.instance)
        self.assertEqual(r.status, 200)
        self.assertNotIn(b"secret", r.wfile.getvalue())
        self.instance.api.assert_called_once_with("POST", "/v1/machines/m_demo/desktop",
                                                 {"action": "start", "width": 1280, "height": 800}, timeout=45)

    def test_upgrade_preserves_first_vnc_bytes(self):
        api = Mock()
        key = base64.b64encode(bytes(16)).decode()
        accept = base64.b64encode(hashlib.sha1((key + desktop.WS_GUID).encode()).digest()).decode()
        api.sock.recv.return_value = (f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode() + b"RFB"
        response, prefix = desktop.request_upgrade(api, self.instance, key)
        self.assertEqual((response, prefix), (accept, b"RFB"))
        headers = api.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-API-Key"], "example")
        self.assertNotIn("Origin", headers)

    def test_disallow_untrusted_origin_and_invalid_keys(self):
        r = Request("/desktop/vnc")
        r.headers.update(Origin="http://elsewhere.test", Host="localhost:8765")
        desktop.handle_desktop(r, self.instance)
        self.assertEqual(r.status, 403)
        r.headers["Origin"] = "http://localhost:8765"
        desktop.handle_desktop(r, self.instance)
        self.assertEqual(r.status, 400)

    def test_guard_input_and_arguments(self):
        with self.assertRaises(desktop.DesktopError):
            desktop.input_command({"key": "press", "value": "alt+c --bad"})
        with self.assertRaises(desktop.DesktopError):
            desktop.input_command({"key": "click", "x": True, "y": 2, "button": 1})
        text = "$(literal)\n你好"
        cmd = desktop.input_command({"key": "type", "value": text})
        self.assertEqual(cmd["stdin"], text)
        self.assertNotIn(text, cmd["argv"])
        r = Request("/desktop/input", "POST", json.dumps({"key": "press", "value": "Escape"}).encode())
        self.instance.api.return_value = {"controller": "agent", "yours": False}
        desktop.handle_desktop(r, self.instance)
        self.assertEqual(r.status, 409)
        self.assertEqual(self.instance.api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
