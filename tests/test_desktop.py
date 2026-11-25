import base64
import hashlib
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, call

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

    def post_action(self, action="start"):
        r = Request("/desktop/" + action, "POST")
        desktop.handle_desktop(r, self.instance)
        return r.status, json.loads(r.wfile.getvalue())

    def test_running_desktop_keeps_existing_session_and_control(self):
        for kind in ("wayland", None):
            with self.subTest(kind=kind):
                state = {"running": True, "controller": "human", "yours": False,
                         "width": 1920, "height": 1080, "secret": "never echoed"}
                if kind:
                    state["kind"] = kind
                self.instance.api.reset_mock()
                self.instance.api.return_value = state
                status, result = self.post_action()
                self.assertEqual(status, 200)
                self.assertEqual(result, {"running": True, "controller": "human", "yours": False,
                                          "installed": None, "agentPausable": None, "kind": kind})
                self.instance.api.assert_called_once_with("POST", "/v1/machines/m_demo/desktop",
                                                          {"action": "status"}, timeout=45)

    def test_stopped_desktop_starts_once_at_initial_size(self):
        stopped = {"installed": True, "running": False}
        started = {"installed": True, "running": True, "controller": "agent"}
        self.instance.api.side_effect = [stopped, started, started]
        for _ in range(2):
            status, result = self.post_action()
            self.assertEqual(status, 200)
            self.assertEqual(result["controller"], "agent")
        path = "/v1/machines/m_demo/desktop"
        self.assertEqual(self.instance.api.call_args_list, [
            call("POST", path, {"action": "status"}, timeout=45),
            call("POST", path, {"action": "start", "width": 1280, "height": 800}, timeout=45),
            call("POST", path, {"action": "status"}, timeout=45),
        ])

    def test_missing_install_pending_and_failed_are_distinct(self):
        for extra, expected in (
            ({}, "agent-browser"),
            ({"installing": True}, "正在安装"),
            ({"failed": True, "log": "private"}, "安装失败"),
        ):
            with self.subTest(extra=extra):
                self.instance.api.reset_mock()
                self.instance.api.return_value = {"installed": False, "running": False, **extra}
                status, result = self.post_action()
                self.assertEqual(status, 409)
                self.assertIn(expected, result["error"])
                self.assertNotIn("private", result["error"])
                self.assertEqual(self.instance.api.call_count, 1)

    def test_unready_start_and_unknown_status_do_not_trigger_other_actions(self):
        self.instance.api.side_effect = [{"installed": True, "running": False}] * 2
        status, result = self.post_action()
        self.assertEqual(status, 409)
        self.assertIn("尚未就绪", result["error"])
        self.assertEqual(self.instance.api.call_count, 2)
        for response in ({"installed": True}, OSError("private")):
            with self.subTest(response=response):
                self.instance.api.reset_mock()
                self.instance.api.side_effect = [response]
                status, result = self.post_action()
                self.assertEqual(status, 502)
                self.assertNotIn("private", result["error"])
                self.assertEqual(self.instance.api.call_count, 1)

    def test_explicit_control_actions_do_not_query_or_start(self):
        self.instance.api.return_value = {"running": True}
        for action in ("takeover", "handback"):
            with self.subTest(action=action):
                self.instance.api.reset_mock()
                self.assertEqual(self.post_action(action)[0], 200)
                self.instance.api.assert_called_once_with("POST", "/v1/machines/m_demo/desktop",
                                                          {"action": action}, timeout=45)

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

    def test_frame_text_validation_precedes_cloud_input(self):
        for text in ("ASCII 前缀", "A🙂B", "é", "a\nb", "a\r\nb", "\t", "\x00", "a\x7fb", "A\x01B"):
            with self.subTest(text=text):
                r = Request("/desktop/input", "POST", json.dumps({"key": "type", "value": text}).encode())
                desktop.handle_desktop(r, self.instance)
                self.assertEqual(r.status, 422)
                self.assertIn("整段文字未发送", json.loads(r.wfile.getvalue())["error"])
        self.instance.api.assert_not_called()
        for text in ("".join(map(chr, range(32, 127))), "a" * 2048):
            self.assertEqual(desktop.input_command({"key": "type", "value": text})["stdin"], text)
        for text in ("", "a" * 2049):
            with self.assertRaises(desktop.DesktopError) as e:
                desktop.input_command({"key": "type", "value": text})
            self.assertEqual(e.exception.status, 400)

    def test_guard_input_and_arguments(self):
        with self.assertRaises(desktop.DesktopError):
            desktop.input_command({"key": "press", "value": "alt+c --bad"})
        with self.assertRaises(desktop.DesktopError):
            desktop.input_command({"key": "click", "x": True, "y": 2, "button": 1})
        text = "$(literal) ~!"
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
