"""Open the local display in a fresh browser app window using inherited pipes."""
import atexit
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def _installed_browser():
    if sys.platform == 'win32':
        import ctypes
        # The Edge installer uses a Program Files or per-user AppData directory.
        for folder in (42, 38, 28):  # CSIDL_PROGRAM_FILESX86, PROGRAM_FILES, LOCAL_APPDATA
            path = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, folder, None, 0, path) == 0:
                exe = Path(path.value) / 'Microsoft/Edge/Application/msedge.exe'
                if exe.is_file():
                    return str(exe)
    elif sys.platform == 'darwin':
        for base in (Path('/Applications'), Path.home() / 'Applications'):
            for name in ('Microsoft Edge', 'Google Chrome', 'Chromium'):
                exe = base / f'{name}.app/Contents/MacOS/{name}'
                if exe.is_file():
                    return str(exe)
    else:
        for name in ('chromium', 'google-chrome', 'microsoft-edge', 'chromium-browser'):
            exe = shutil.which(name)
            if exe:
                return exe
    return None


# Remap Chromium's fixed POSIX pipe descriptors in a fresh process. preexec_fn
# cannot safely run Python in a fork of the multithreaded viewer process.
_POSIX_EXEC = '''import os, sys, fcntl
r = fcntl.fcntl(int(sys.argv[1]), fcntl.F_DUPFD_CLOEXEC, 5)
w = fcntl.fcntl(int(sys.argv[2]), fcntl.F_DUPFD_CLOEXEC, 5)
os.close(int(sys.argv[1])); os.close(int(sys.argv[2]))
os.dup2(r, 3); os.dup2(w, 4)
os.execv(sys.argv[3], sys.argv[3:])'''


def _spawn(exe, origin, profile, child_read, child_write):
    args = [exe, '--app=' + origin, '--user-data-dir=' + profile, '--remote-debugging-pipe',
            '--no-first-run', '--no-default-browser-check', '--disable-sync',
            '--disable-background-networking', '--disable-background-mode', '--disable-features=Translate,msEdgeTranslate']
    env = os.environ.copy()
    for key in ('INSTABOX_API_KEY', 'CLOUDWORKSPACE_RENDER_TOKEN'):
        env.pop(key, None)
    io = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, env=env)
    if sys.platform == 'win32':
        import msvcrt
        handles = [msvcrt.get_osfhandle(fd) for fd in (child_read, child_write)]
        info = subprocess.STARTUPINFO()
        info.lpAttributeList = {'handle_list': handles}
        args.append('--remote-debugging-io-pipes=' + ','.join(map(str, handles)))
        try:
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            return subprocess.Popen(args, startupinfo=info, **io)
        finally:
            for handle in handles:
                os.set_handle_inheritable(handle, False)
    return subprocess.Popen([sys.executable, '-I', '-S', '-c', _POSIX_EXEC, str(child_read), str(child_write), *args],
                            pass_fds=(child_read, child_write), start_new_session=True, **io)


def _drive(source, sink, origin, token, on_open):
    # Only this fresh browser has the other ends of the pipes. The capability
    # is delivered to our own viewer function and never enters browser history.
    counter = 0

    def replies():
        pending = bytearray()
        while True:
            end = pending.find(0)
            if end == -1:
                chunk = os.read(source.fileno(), 65536)
                if not chunk or len(pending) + len(chunk) > 1024 * 1024:
                    raise OSError('invalid browser reply')
                pending.extend(chunk)
            else:
                message = json.loads(pending[:end])
                del pending[:end + 1]
                yield message

    stream = replies()

    def call(method, params, session=None):
        nonlocal counter
        counter += 1
        message = {'id': counter, 'method': method, 'params': params}
        if session:
            message['sessionId'] = session
        sink.write(json.dumps(message).encode() + b'\0')
        sink.flush()
        for reply in stream:
            if reply.get('id') == counter:
                if 'error' in reply:
                    raise OSError('browser refused the app connection')
                return reply['result']
        raise OSError('closed browser pipe')

    while True:
        pages = [t for t in call('Target.getTargets', {})['targetInfos']
                 if t['type'] == 'page' and t['url'] == origin]
        if len(pages) == 1:
            break
        time.sleep(.05)
    session = call('Target.attachToTarget', {'targetId': pages[0]['targetId'], 'flatten': True})['sessionId']
    call('Page.enable', {}, session)

    def deliver():
        expression = 'location.href === ' + json.dumps(origin) + ' && window.receiveWorkspaceDisplayToken'
        result = call('Runtime.evaluate', {'expression': expression, 'silent': True}, session)['result']
        if result.get('type') != 'function' or 'objectId' not in result:
            return False
        # The object id belongs to this document's execution context. If it is
        # replaced during navigation Chrome invalidates the id, so a new page
        # cannot receive the capability through a stale origin check.
        obj = result['objectId']
        try:
            reply = call('Runtime.callFunctionOn', {'objectId': obj,
                         'functionDeclaration': 'function(value) { return this(value); }',
                         'arguments': [{'value': token}], 'returnByValue': True, 'silent': True}, session)
            return reply['result'].get('value') is True
        finally:
            call('Runtime.releaseObject', {'objectId': obj}, session)

    while not deliver():
        time.sleep(.05)
    on_open()
    for event in stream:
        method = event.get('method')
        if method == 'Target.detachedFromTarget' and event.get('params', {}).get('sessionId') == session:
            return
        if event.get('sessionId') != session:
            continue
        if method in ('Inspector.detached', 'Inspector.targetCrashed'):
            return
        if method == 'Page.loadEventFired':
            deliver()


def open_app_window(url, on_closed=None):
    match = re.fullmatch(r'(http://127\.0\.0\.1:[0-9]{1,5}/)#[A-Za-z0-9_-]{43}', url)
    if match is None:
        return False
    exe = _installed_browser()
    if not exe:
        return False
    ready = threading.Event()
    opened = []

    def window():
        try:
            with tempfile.TemporaryDirectory(prefix='cloud-workspace-window-', ignore_cleanup_errors=True) as profile:
                child_read, parent_write = os.pipe()
                parent_read, child_write = os.pipe()
                with os.fdopen(parent_read, 'rb', buffering=0) as source, os.fdopen(parent_write, 'wb') as sink:
                    try:
                        process = _spawn(exe, match[1], profile, child_read, child_write)
                    finally:
                        os.close(child_read)
                        os.close(child_write)
                    def stop():
                        try:
                            if process.poll() is None:
                                process.terminate()
                            process.wait(timeout=3)
                        except (OSError, subprocess.TimeoutExpired):
                            process.kill()
                        shutil.rmtree(profile, ignore_errors=True)

                    timeout = threading.Timer(20, process.kill)
                    timeout.start()
                    atexit.register(stop)
                    def connected():
                        timeout.cancel()
                        opened.append(True)
                        ready.set()
                    try:
                        _drive(source, sink, match[1], url.split('#', 1)[1], connected)
                    finally:
                        timeout.cancel()
                        stop()
                        atexit.unregister(stop)
        except (KeyError, ValueError, OSError, subprocess.SubprocessError):
            pass
        finally:
            ready.set()
            if opened and on_closed is not None:
                on_closed()

    threading.Thread(target=window, name='cloud-app-window', daemon=True).start()
    ready.wait(25)
    return bool(opened)
