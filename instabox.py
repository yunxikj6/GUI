"""InstaBox control-plane transport for a single private workspace."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path, PurePosixPath


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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


class InstaBox:
    base = 'https://api.instabox.sh'

    def __init__(self, key: str, machine_id: str | None = None, project: str | None = None):
        if not key.strip():
            raise BridgeError('missing_key', '需要 InstaBox API 密钥。')
        self.project = project or '/data/cloud-workspace/project'
        dest = PurePosixPath(self.project)
        if not dest.is_absolute() or dest.parent == dest or '..' in dest.parts or chr(0) in self.project:
            raise BridgeError('invalid_project', '请提供云机上项目的绝对目录。')
        self.project = str(dest)
        self.custom_project = project is not None
        self.key = key.strip()
        machines = self.api('GET', '/v1/machines').get('machines', [])
        if not machine_id and len(machines) != 1:
            raise BridgeError('select_machine', '请用 --machine 指定已有 Linux 实例。')
        self.machine_id = machine_id or machines[0]['id']
        if not self.machine_id.startswith('m_') or not self.machine_id.replace('_', '').isalnum():
            raise BridgeError('invalid_machine', '实例标识格式无效。')
        self.path = '/v1/machines/' + self.machine_id
        self.worker = None

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
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                payload = response.read(6 * 1024 * 1024)
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

    def setup(self, source: Path):
        self.ensure_running()
        bundle = {n: (source / n).read_text(encoding='utf-8') for n in WORKER_FILES}
        raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()
        release = hashlib.sha256(raw).hexdigest()[:20]
        directory = '/data/cloud-workspace/runtime/' + release
        self.exec(['python3', '-c', _INSTALL, directory], gzip.compress(raw).hex())
        self.worker = directory + '/workspace.py'
        result = self.rpc({'op': 'status' if self.custom_project else 'init'})
        if not result.get('ok'):
            error = result.get('error', {})
            raise BridgeError(error.get('code', 'init_error'), error.get('message', '项目初始化失败。'))

    def rpc(self, request):
        # Send arguments as stdin so project text and prompts are never shell
        # fragments and larger saves do not hit the guest's argv length limit.
        argv = ['python3', self.worker, '--stdin']
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
