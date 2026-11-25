"""Run the Cloud Workbench on loopback. Requires Python 3.10 or newer."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from instabox import BridgeError, InstaBox

APP = Path(__file__).resolve().parent
PUBLIC_OPS = {'status', 'tree', 'file', 'diff', 'tasks', 'start', 'events', 'save',
              'proposal', 'decideProposal'}
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/index.html': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/proposals.js': ('proposals.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


class LocalWorkspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.env = {**os.environ, 'CLOUD_WORKSPACE_ROOT': str(self.root), 'PYTHONIOENCODING': 'utf-8'}
        self.env.pop('INSTABOX_API_KEY', None)
        init = self.rpc({'op': 'init'})
        if not init.get('ok'):
            error = init.get('error', {})
            raise BridgeError(error.get('code', 'init_error'), error.get('message', '本机项目初始化失败。'))

    def rpc(self, req):
        process = subprocess.run([sys.executable, str(APP / 'cloud' / 'workspace.py'), '--stdin'],
                                 input=json.dumps(req, ensure_ascii=False).encode(),
                                 capture_output=True, env=self.env, timeout=45)
        try:
            return json.loads(process.stdout)
        except ValueError:
            raise BridgeError('local_worker', '本机工作进程未返回有效数据。') from None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True


def workspace_identity(workspace):
    """The launcher knows which project it serves even between cloud connections."""
    if isinstance(workspace, LocalWorkspace):
        mode = 'local'
        project = workspace.root.name
        identity = str(workspace.root)
    else:
        mode = 'instabox'
        project = PurePosixPath(workspace.project).name
        identity = workspace.machine_id + ':' + workspace.project
    return {'project': project, 'mode': mode,
            'workspaceId': hashlib.sha256((mode + ':' + identity).encode()).hexdigest()[:20]}


class Handler(BaseHTTPRequestHandler):
    server_version = 'CloudWorkbench/0.1'

    def log_message(self, *args):
        # No URL, file name, prompt or secret is written to an access log.
        pass

    def send_bytes(self, status, content, mime):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(),
                        'application/json; charset=utf-8')

    def error(self, status, code, message):
        self.send_json(status, {'ok': False, 'error': {'code': code, 'message': message}})

    def allowed(self):
        port = self.server.server_address[1]
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in hosts or (origin is not None and origin != 'http://' + host) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.error(403, 'origin', '请求来源不受信任。')
            return False
        return True

    def desktop(self):
        from desktop import handle_desktop
        if isinstance(self.server.workspace, LocalWorkspace) and (
            self.command == 'POST' or urllib.parse.urlsplit(self.path).path in {'/desktop/frame', '/desktop/vnc'}
        ):
            self.error(409, 'local_mode', 'Linux 桌面连接需要 InstaBox 模式。')
            return
        handle_desktop(self, self.server.workspace)

    def do_GET(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/desktop/'):
            self.desktop()
        elif path == '/api/workspace':
            self.send_json(200, {'ok': True, 'data': workspace_identity(self.server.workspace)})
        elif path in STATIC:
            name, mime = STATIC[path]
            self.send_bytes(200, (APP / 'web' / name).read_bytes(), mime)
        elif path == '/favicon.ico':
            self.send_bytes(204, b'', 'image/x-icon')
        else:
            self.error(404, 'not_found', '未找到。')

    def do_POST(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/desktop/'):
            self.desktop()
            return
        if path != '/api/rpc':
            self.error(404, 'not_found', '未找到。')
            return
        if self.headers.get_content_type() != 'application/json':
            self.error(415, 'content_type', '请求需要 JSON。')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1800000:
                self.error(413, 'request_size', '请求过大或为空。')
                return
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict) or req.get('op') not in PUBLIC_OPS:
                self.error(400, 'invalid_request', '未知的工作区操作。')
                return
            workspace = self.server.workspace
            expected = self.headers.get('X-Workspace-Id')
            if expected is not None and expected != workspace_identity(workspace)['workspaceId']:
                self.error(409, 'workspace_changed', '启动器已切换项目，请刷新工作区。')
                return
            result = workspace.rpc(req)
            if req['op'] == 'status' and result.get('ok'):
                result['data'].update(workspace_identity(workspace))
            self.send_json(200, result)
        except (ValueError, TypeError, UnicodeError):
            self.error(400, 'invalid_json', '请求格式无效。')
        except (BridgeError, subprocess.TimeoutExpired) as exc:
            self.error(503, getattr(exc, 'code', 'timeout'), str(exc) if isinstance(exc, BridgeError) else '请求超时。')
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    args = argparse.ArgumentParser(description='云端工作台 · 本机图形客户端')
    args.add_argument('--local', action='store_true', help='开发模式，使用本机 Linux 工作进程')
    args.add_argument('--project', type=Path, help='本机开发模式使用的项目目录')
    args.add_argument('--remote-project', help='云机已有项目的绝对目录，默认使用独立示例仓库')
    args.add_argument('--machine', help='InstaBox 实例 ID，只有一台时自动选择')
    args.add_argument('--port', type=int, default=8765)
    args.add_argument('--no-browser', action='store_true')
    opt = args.parse_args()
    if opt.local and not opt.project:
        args.error('--local 需要 --project 指定开发用项目目录')
    if not opt.local and opt.project:
        args.error('--project 仅适用于 --local')
    if opt.local and (opt.remote_project or opt.machine):
        args.error('--local 不接受云机参数')
    try:
        if opt.local:
            workspace = LocalWorkspace(opt.project)
        else:
            key = os.environ.get('INSTABOX_API_KEY') or getpass.getpass('InstaBox API key（不回显）: ')
            workspace = InstaBox(key, opt.machine, opt.remote_project)
            print('正在连接云机并检查工作区…', flush=True)
            workspace.setup(APP / 'cloud')
        with LocalServer(('127.0.0.1', opt.port), Handler) as server:
            server.workspace = workspace
            addr = f'http://127.0.0.1:{server.server_port}/'
            print(f'云端工作台 {addr}', flush=True)
            if not opt.no_browser:
                threading.Timer(.2, webbrowser.open, args=(addr,)).start()
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (BridgeError, OSError) as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
