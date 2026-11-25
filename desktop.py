"""Loopback-only desktop viewer. The browser and InstaBox speak the same RFB WebSocket."""
import base64
import binascii
import functools
import hashlib
import http.client
import json
from pathlib import Path
import select
import re
import ssl
import tarfile
import threading
from urllib.parse import quote, unquote, urlsplit
from urllib.request import getproxies, proxy_bypass


ASSETS = Path(__file__).resolve().parent / "desktop_assets"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
API_HOST = "api.instabox.sh"
_START_LOCK = threading.Lock()


class DesktopError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def send(handler, status, media, body):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", media)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True



def send_json(handler, status, data):
    send(handler, status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode())


@functools.lru_cache(maxsize=1)
def vendor_scripts():
    # Read only known JavaScript files. Never extract archive paths to disk.
    with tarfile.open(ASSETS / "novnc-1.7.0.tgz", "r:gz") as package:
        return {
            entry.name.removeprefix("package/"): package.extractfile(entry).read()
            for entry in package.getmembers()
            if entry.isfile() and entry.name.startswith(("package/core/", "package/vendor/"))
            and entry.name.endswith(".js")
        }


def upstream_connection():
    """Open verified TLS, respecting the same HTTP CONNECT proxy as the API client."""
    context = ssl.create_default_context()
    proxy = getproxies().get("https")
    if not proxy or proxy_bypass(API_HOST):
        return http.client.HTTPSConnection(API_HOST, timeout=15, context=context)
    dest = urlsplit(proxy)
    if dest.scheme != "http" or not dest.hostname:
        raise DesktopError(502, "当前代理不能转发桌面 WebSocket。")
    headers = {}
    if dest.username is not None:
        auth = (unquote(dest.username) + ":" + unquote(dest.password or "")).encode()
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(auth).decode()
    conn = http.client.HTTPSConnection(dest.hostname, dest.port or 80, timeout=15, context=context)
    conn.set_tunnel(API_HOST, 443, headers=headers)
    return conn


def request_upgrade(conn, instance, key):
    """Only the server-side handshake carries the account credential."""
    path = "/v1/machines/" + quote(instance.machine_id, safe="") + "/vnc"
    conn.request("GET", path, headers={
        "Host": API_HOST, "Connection": "Upgrade", "Upgrade": "websocket",
        "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13",
        "X-API-Key": instance.key,
    })
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.sock.recv(4096)
        if not chunk or len(data) >= 32768:
            raise DesktopError(502, "云端桌面没有完成连接。")
        data.extend(chunk)
    header, pending = data.split(b"\r\n\r\n", 1)
    lines = header.decode("latin-1").split("\r\n")
    code = lines[0].split(" ")[1:2]
    values = {}
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep:
            values[k.lower()] = v.strip()
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    if code != ["101"] or values.get("sec-websocket-accept") != accept or values.get("upgrade", "").lower() != "websocket":
        raise DesktopError(502, "云端桌面通道连接失败（HTTP %s）。" % (code[0] if code and code[0].isdigit() else "未知"))
    return accept, pending


def relay(browser, remote):
    # Keep per-direction buffers bounded, forwarding framed data without decoding.
    # select handles the underlying FD; OpenSSL may also have plaintext queued.
    remote.settimeout(20)
    browser.settimeout(20)
    while True:
        while remote.pending():
            browser.sendall(remote.recv(65536))
        readable, _, _ = select.select([browser, remote], [], [], 95)
        if not readable:
            return
        for src in readable:
            data = src.recv(65536)
            if not data:
                return
            (remote if src is browser else browser).sendall(data)


def websocket(handler, instance):
    hdr = handler.headers
    # Require the interactive page's origin even if the HTTP server allows
    # origin-less requests from command-line clients elsewhere.
    origin = urlsplit(hdr.get("Origin", ""))
    if origin.scheme not in ("http", "https") or origin.netloc != hdr.get("Host"):
        raise DesktopError(403, "桌面通道需要本机工作区打开。")
    key = hdr.get("Sec-WebSocket-Key", "")
    try:
        valid = len(base64.b64decode(key, validate=True)) == 16
    except (ValueError, binascii.Error):
        valid = False
    if not (valid and hdr.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in [v.strip().lower() for v in hdr.get("Connection", "").split(",")]
            and hdr.get("Sec-WebSocket-Version") == "13"):
        raise DesktopError(400, "需要 WebSocket 连接。")
    conn = upstream_connection()
    try:
        accept, pending = request_upgrade(conn, instance, key)
        handler.send_response(101)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.close_connection = True
        try:
            handler.wfile.flush()
            if pending:
                handler.connection.sendall(pending)
            relay(handler.connection, conn.sock)
        except OSError:
            # One peer closed or stalled after completing the upgrade.
            pass
    finally:
        conn.close()


# scrot creates a JPEG at the screen's native resolution. Pass the image through
# the documented exec transport as bounded base64, avoiding a public display port.
CAPTURE = """set -eu
umask 077
image=$(mktemp --suffix=.jpg)
trap 'rm -f -- "$image"' EXIT
if command -v scrot >/dev/null; then
    scrot -o -q 55 "$image" >/dev/null
else
    import -window root -quality 55 "jpeg:$image"
fi
test "$(wc -c < "$image")" -le 750000
base64 -w 0 "$image"
"""


def exec_desktop(instance, command):
    data = {"timeoutSeconds": 12, "noHistory": True, "envs": {"DISPLAY": ":1"}, **command}
    result = instance.api("POST", "/v1/machines/" + quote(instance.machine_id, safe="") + "/exec", data, timeout=25)
    if result.get("exitCode") != 0 or result.get("truncated"):
        raise DesktopError(502, "逐帧操作失败。请检查云机 X11 桌面和截屏工具。")
    return result.get("stdout", "")


def frame(handler, instance):
    raw = base64.b64decode(exec_desktop(instance, {"cmd": CAPTURE}), validate=True)
    if not raw.startswith(b"\xff\xd8") or len(raw) > 750000:
        raise DesktopError(502, "云端没有返回有效的桌面图像。")
    send(handler, 200, "image/jpeg", raw)


def input_command(payload):
    if not isinstance(payload, dict):
        raise DesktopError(400, "输入格式无效。")
    key = payload.get("key")
    if key == "click":
        x, y, button = (payload.get(k) for k in ("x", "y", "button"))
        if (all(type(i) is int for i in (x, y, button)) and 0 <= x < 8192
                and 0 <= y < 8192 and 1 <= button <= 5):
            return {"argv": ["xdotool", "mousemove", str(x), str(y), "click", str(button)]}
    if key == "press":
        v = payload.get("value")
        if isinstance(v, str) and re.fullmatch(r"(?:(?:ctrl|alt|shift)\+){0,3}(?:[a-zA-Z0-9]|Return|Tab|BackSpace|Delete|Home|End|Up|Down|Left|Right|Escape|space)", v):
            return {"argv": ["xdotool", "key", "--", v]}
    if key == "type":
        v = payload.get("value")
        if isinstance(v, str) and 0 < len(v) <= 2048:
            if not re.fullmatch(r"[ -~]+", v):
                raise DesktopError(422, "整段文字未发送。逐帧输入仅支持单行可打印 ASCII。其他文字请切回实时桌面，打开“剪贴板”发送后在云端粘贴。")
            return {"argv": ["xdotool", "type", "--delay", "1", "--file", "-"], "stdin": v}
    raise DesktopError(400, "无法识别此桌面输入。")


def desktop_input(handler, instance):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16384 or handler.headers.get("Transfer-Encoding"):
            raise ValueError()
        cmd = input_command(json.loads(handler.rfile.read(length)))
    except (ValueError, UnicodeError):
        raise DesktopError(400, "输入内容无效或过长。") from None
    # Respect the desktop provider's human/agent takeover just like the VNC
    # gateway. Never send an input after the lease quietly returned to an agent.
    state = instance.api("POST", "/v1/machines/" + quote(instance.machine_id, safe="") + "/desktop", {"action": "status"})
    if not state.get("yours") or state.get("controller") != "human":
        raise DesktopError(409, "输入控制已交还。请重新接管输入。")
    exec_desktop(instance, cmd)
    send_json(handler, 200, {"ok": True})


def desktop_action(instance, action):
    path = "/v1/machines/" + quote(instance.machine_id, safe="") + "/desktop"
    requests = ({"action": action},)
    if action == "start":
        requests = ({"action": "status"}, {"action": "start", "width": 1280, "height": 800})
    for body in requests:
        state = instance.api("POST", path, body, timeout=45)
        if state.get("running") is True:
            return {k: state.get(k) for k in ("installed", "running", "controller", "yours", "agentPausable", "kind")}
        if state.get("installing"):
            raise DesktopError(409, "这台云机正在安装图形桌面。请等安装完成后重新连接。")
        if state.get("failed"):
            raise DesktopError(409, "这台云机的图形桌面安装失败。请在 InstaBox 查看安装日志，修复后重新连接。")
        if state.get("installed") is False:
            raise DesktopError(409, "这台云机尚未安装图形桌面。请在 InstaBox 安装桌面，或改用带桌面的 agent-browser 模板实例。")
        if state.get("running") is not False:
            raise DesktopError(502, "无法确认云机的图形桌面状态，请重新连接。")
    raise DesktopError(409, "云机的图形桌面尚未就绪，请稍后重新连接。")


def handle_desktop(handler, instance):
    path = urlsplit(handler.path).path
    try:
        if handler.command == "GET":
            if path in ("/desktop", "/desktop/", "/desktop/index.html"):
                return send(handler, 200, "text/html; charset=utf-8", (ASSETS / "index.html").read_bytes())
            if path == "/desktop/frame":
                return frame(handler, instance)
            if path == "/desktop/vnc":
                return websocket(handler, instance)
            if path.startswith("/desktop/novnc/"):
                script = vendor_scripts().get(path[len("/desktop/novnc/"):])
                if script is not None:
                    return send(handler, 200, "text/javascript; charset=utf-8", script)
            assets = {"style.css": "text/css", "viewer.js": "text/javascript", "NOTICE.txt": "text/plain"}
            name = path.removeprefix("/desktop/")
            if name in assets:
                return send(handler, 200, assets[name] + "; charset=utf-8", (ASSETS / name).read_bytes())
        if handler.command == "POST" and path == "/desktop/input":
            return desktop_input(handler, instance)
        actions = {"/desktop/start": "start", "/desktop/takeover": "takeover", "/desktop/handback": "handback"}
        if handler.command == "POST" and path in actions:
            # Empty body only; these commands take no input from the viewer.
            if handler.headers.get("Content-Length", "0") != "0" or handler.headers.get("Transfer-Encoding"):
                raise DesktopError(400, "此操作无需请求内容。")
            action = actions[path]
            if action == "start":
                # Tabs share the launcher's cloud machine. A second tab must
                # observe the first start before choosing its display size.
                with _START_LOCK:
                    result = desktop_action(instance, action)
            else:
                result = desktop_action(instance, action)
            return send_json(handler, 200, result)
        raise DesktopError(404, "找不到该桌面资源。")
    except DesktopError as e:
        return send_json(handler, e.status, {"error": str(e)})
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
        return
    except (OSError, http.client.HTTPException):
        return send_json(handler, 502, {"error": "连接云端桌面失败。请检查实例和网络后重试。"})
    except Exception:
        # Provider errors may include private request context.
        return send_json(handler, 502, {"error": "InstaBox 未能完成桌面操作，请检查机器状态和密钥权限。"})
