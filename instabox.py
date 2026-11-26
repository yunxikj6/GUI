"""InstaBox control-plane transport for a single private workspace."""
from __future__ import annotations

from functools import cached_property
import gzip
import hashlib
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

PROVIDER_ORIGIN = 'https://api.instabox.sh'


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_provider_request(request, timeout):
    # Both JSON and private spool reads stay on the fixed, authenticated HTTPS origin.
    if (not request.full_url.startswith(PROVIDER_ORIGIN + '/v1/')
            or any(c in request.full_url for c in '#\\\r\n\0')
            or request.get_method() not in ('GET', 'POST')):
        raise ValueError('expected the private InstaBox HTTPS API origin')
    for name, value in request.header_items():
        if name.lower() not in ('accept', 'content-type', 'x-api-key', 'idempotency-key'):
            raise ValueError('unexpected InstaBox request header')
        if not isinstance(value, str) or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid InstaBox request header value')
    https = urllib.request.HTTPSHandler(context=ssl.create_default_context())
    return urllib.request.build_opener(NoRedirect(), https).open(request, timeout=timeout)


def json_bytes(value):
    # Linux filenames can contain undecodable bytes. Preserve their surrogate escapes
    # as JSON \uXXXX sequences while sending ordinary Unicode as UTF-8.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8', 'backslashreplace')


# Install an immutable worker directory in one rename. Another client may
# install the same version while this one is connecting.
WORKER_FILES = ('tasks.py', 'workspace.py', 'proposals.py', 'codex_tools.py')
_INSTALL = f"""
import gzip, json, os, pathlib, shutil, sys, tempfile
dest = pathlib.Path(sys.argv[1])
dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if not dest.exists():
    stage = pathlib.Path(tempfile.mkdtemp(dir=dest.parent))
    try:
        for name, source in json.loads(gzip.decompress(bytes.fromhex(input()))).items():
            if name not in {WORKER_FILES!r}:
                raise ValueError('unexpected release file')
            (stage / name).write_text(source, encoding='utf-8')
        try:
            stage.rename(dest)
        except OSError:
            if not dest.is_dir():
                raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
print('ready')
"""


class InstaBoxAccount:
    base = PROVIDER_ORIGIN

    def __init__(self, key: str):
        if not key.strip():
            raise BridgeError('missing_key', '需要 InstaBox API 密钥。')
        self.key = key.strip()

    def machine_page(self, cursor=None):
        query = {'limit': 30}
        if cursor is not None:
            query['cursor'] = cursor
        return self.api('GET', '/v1/machines?' + urllib.parse.urlencode(query))

    def api(self, method, path, body=None, timeout=45, headers=None):
        if not path.startswith('/v1/') or '#' in path:
            raise ValueError('expected an InstaBox API path')
        request_headers = {'Accept': 'application/json', 'X-API-Key': self.key}
        data = None
        if body is not None:
            data = json_bytes(body)
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=request_headers)
        try:
            with open_provider_request(request, timeout) as response:
                payload = response.read(6 * 1024 * 1024 + 1)
                if len(payload) > 6 * 1024 * 1024:
                    raise ValueError('InstaBox response too large')
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(32768)).get('error', {})
                code = str(error.get('code') or f'http_{exc.code}')
            except (ValueError, AttributeError):
                code = f'http_{exc.code}'
            raise BridgeError(code, f'InstaBox 请求失败（HTTP {exc.code}，{code}）。') from None
        except (OSError, socket.timeout, ValueError, urllib.error.URLError) as exc:
            raise BridgeError('network', '无法取得 InstaBox 响应，请检查网络后重试。') from exc


class InstaBox(InstaBoxAccount):
    def __init__(self, key: str, machine_id: str | None = None, project: str | None = None,
                 *, source: Path | None = None):
        super().__init__(key)
        self.project = project or '/data/cloud-workspace/project'
        dest = PurePosixPath(self.project)
        if not dest.is_absolute() or dest.parent == dest or '..' in dest.parts or chr(0) in self.project:
            raise BridgeError('invalid_project', '请提供云机上项目的绝对目录。')
        self.project = str(dest)
        self.custom_project = project is not None
        machines = self.api('GET', '/v1/machines').get('machines', [])
        if not machine_id and len(machines) != 1:
            raise BridgeError('select_machine', '请用 --machine 指定已有 Linux 实例。')
        self.machine_id = machine_id or machines[0]['id']
        if not self.machine_id.startswith('m_') or not self.machine_id.replace('_', '').isalnum():
            raise BridgeError('invalid_machine', '实例标识格式无效。')
        self.path = '/v1/machines/' + self.machine_id
        self.worker = None
        self._source = source if source is not None else Path(__file__).resolve().parent / 'cloud'
        self._setup_lock = threading.Lock()

    def ensure_running(self):
        deadline = time.monotonic() + 150
        issued = False
        while time.monotonic() < deadline:
            state = self.api('GET', self.path)['state']
            if state == 'running':
                return
            if state in {'failed', 'deleted'}:
                raise BridgeError('machine_unavailable', f'实例状态为 {state}。')
            if state in {'stopped', 'suspended'} and not issued:
                action = 'resume' if state == 'suspended' else 'start'
                self.api('POST', self.path + '/' + action, {}, headers={'Idempotency-Key': str(uuid.uuid4())})
                issued = True
            time.sleep(1.5)
        raise BridgeError('machine_timeout', '等待实例就绪超时，请查看实例状态后重连。')

    def exec(self, argv, stdin='', timeout=45, envs=None):
        result = self.api('POST', self.path + '/exec', {
            'argv': argv, 'stdin': stdin, 'noHistory': True, 'timeoutSeconds': timeout, 'envs': envs or {}
        }, timeout=timeout + 45)
        if result.get('timedOut') or result.get('truncated') or result.get('exitCode') != 0:
            raise BridgeError('worker_error', '云端工作进程未能返回完整结果。')
        return result.get('stdout', '')

    @cached_property
    def _file_worker(self):
        return (self._source / 'workspace.py').read_text(encoding='utf-8')

    def file_request(self, request):
        """Run a scoped request against the existing project without initializing it."""
        raw = self.exec(['python3', '-I', '-B', '-c', self._file_worker, '--stdin'],
                        json_bytes(request).decode('utf-8'), envs={'CLOUD_WORKSPACE_ROOT': self.project})
        try:
            result = json.loads(raw)
            if result['ok'] is False:
                error = result['error']
                if isinstance(error['code'], str) and isinstance(error['message'], str):
                    raise BridgeError(error['code'], error['message'])
            if result['ok'] is True and isinstance(result['data'], dict):
                return result['data']
        except (ValueError, TypeError, KeyError):
            pass
        raise BridgeError('invalid_response', '未取得有效的工作区响应。')

    def setup(self, source: Path):
        """Explicitly install and validate the selected version of the workspace tools."""
        with self._setup_lock:
            self._install_worker(source)

    def _install_worker(self, source: Path):
        self.ensure_running()
        try:
            bundle = {n: (source / n).read_text(encoding='utf-8') for n in WORKER_FILES}
        except (OSError, UnicodeError) as exc:
            raise BridgeError('worker_source', '无法读取本机工作区组件，请检查程序文件后重试。') from exc
        raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()
        release = hashlib.sha256(raw).hexdigest()[:20]
        directory = '/data/cloud-workspace/runtime/' + release
        self.exec(['python3', '-c', _INSTALL, directory], gzip.compress(raw).hex())
        worker = directory + '/workspace.py'
        initial = {'op': 'status' if self.custom_project else 'init'}
        result = self._worker_rpc(worker, initial)
        if not result.get('ok'):
            error = result.get('error', {})
            raise BridgeError(error.get('code', 'init_error'), error.get('message', '项目初始化失败。'))
        self.worker = worker
        return initial, result

    def rpc(self, request):
        with self._setup_lock:
            if self.worker is None:
                initial, result = self._install_worker(self._source)
                if request == initial:
                    return result
            worker = self.worker
        return self._worker_rpc(worker, request)

    def _worker_rpc(self, worker, request):
        # Send arguments as stdin so project text and prompts are never shell
        # fragments and larger saves do not hit the guest's argv length limit.
        argv = ['python3', worker, '--stdin']
        stdin = json_bytes(request).decode('utf-8')
        env = {'CLOUD_WORKSPACE_ROOT': self.project}
        if 'CLOUD_WORKSPACE_GIT' in os.environ:
            env['CLOUD_WORKSPACE_GIT'] = os.environ['CLOUD_WORKSPACE_GIT']
        try:
            output = self.exec(argv, stdin, envs=env)
        except BridgeError as exc:
            if exc.code not in {'machine_not_running', 'machine_suspended'}:
                raise
            # A rejected exec never entered the guest. A transport timeout is
            # ambiguous, so it is surfaced and never automatically resubmitted.
            self.ensure_running()
            output = self.exec(argv, stdin, envs=env)
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                raise ValueError()
            return result
        except ValueError:
            raise BridgeError('invalid_response', '云端返回了无法读取的工作区数据。') from None
