#!/usr/bin/env python3
"""Verify an explicit user's action in one nonce-tagged nested Ubuntu GNOME session."""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import time
from urllib.parse import unquote_to_bytes


DIR_FLAGS = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
STATE = '.verify.json'
DBUS = 'org.freedesktop.DBus'
FM = 'org.freedesktop.FileManager1'
PROC = Path('/proc')


class Failure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def identity(info):
    return [info.st_dev, info.st_ino]


def entry(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def same_dir(info, expected):
    return bool(info and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and identity(info) == expected)


def boot_id():
    return (PROC / 'sys/kernel/random/boot_id').read_text().strip()


def read_state(fd, nonce):
    try:
        handle = os.open(STATE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                    info.st_mode & 0o077 or info.st_size > 8192):
                raise Failure('unsafe_fixture')
            value = json.load(stream)
        if (not isinstance(value, dict) or value.get('nonce') != nonce or value.get('version') != 1 or
                value.get('root') != identity(os.fstat(fd)) or not isinstance(value.get('files'), list)):
            raise Failure('unsafe_fixture')
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise Failure('unsafe_fixture') from exc


def write_state(fd, value):
    name = '.verify-' + secrets.token_hex(12) + '.tmp'
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        if entry(fd, name):
            os.unlink(name, dir_fd=fd)


@contextmanager
def locked_root(root):
    fd = os.open(root, DIR_FLAGS)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or not os.path.samestat(info, root.lstat()):
            raise Failure('unsafe_fixture')
        yield fd
    finally:
        os.close(fd)


def prepare(root, nonce, expected):
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise Failure('already_exists') from exc
    with locked_root(root) as fd:
        os.mkdir('Files', mode=0o700, dir_fd=fd)
        write_state(fd, {'version': 1, 'nonce': nonce, 'root': identity(os.fstat(fd)), 'files': identity(entry(fd, 'Files')),
                         'boot': boot_id(), 'start': int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK'))})
    return {'status': 'prepared', 'folder': str(root/'Files'), 'expected': expected}


def process_state(proc):
    # Fields after the final ')' begin with kernel field 3; field 22 is the boot-relative start time.
    fields = (proc / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
    return int(fields[19]) if fields[0] not in (b'Z', b'X') else None


def proc_details(pid, executable, keys):
    proc = PROC / str(pid)
    try:
        if proc.stat().st_uid != os.getuid() or not os.path.samefile(proc/'exe', executable):
            return None
        start = process_state(proc)
        args = [os.fsdecode(v) for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
        env = {}
        for item in (proc/'environ').read_bytes().split(b'\0'):
            name, sep, value = item.partition(b'=')
            if sep and name in keys:
                env[name] = os.fsdecode(value)
        if start is not None and start == process_state(proc):
            return start, args, env
    except (OSError, IndexError, ValueError):
        pass
    return None


def valid_bus(address):
    # The private dbus-run-session daemon is local. Do not contact TCP or an alternative address.
    m = re.fullmatch(r'unix:(path|abstract)=([^,;\s]+)(?:,guid=[0-9a-fA-F]{32})?', address)
    if not m:
        return False
    if m[1] == 'abstract':
        return bool(re.fullmatch(r'(?:/tmp/)?dbus-[A-Za-z0-9_-]+', m[2]))
    socket = Path(os.fsdecode(unquote_to_bytes(m[2])))
    if not socket.is_absolute() or socket == Path(f'/run/user/{os.getuid()}/bus'):
        return False
    try:
        info = socket.lstat()
        return stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
    except OSError:
        return False


def shell_details(pid, nonce):
    keys = {b'CLOUDWORKSPACE_VERIFY_ID', b'GNOME_SHELL_SESSION_MODE', b'XDG_SESSION_TYPE', b'DBUS_SESSION_BUS_ADDRESS'}
    details = proc_details(pid, '/usr/bin/gnome-shell', keys)
    if not details:
        return None
    start, argv, env = details
    displays = [a[len('--wayland-display='):] for a in argv if a.startswith('--wayland-display=')]
    if (env.get(b'CLOUDWORKSPACE_VERIFY_ID') != nonce or env.get(b'GNOME_SHELL_SESSION_MODE') != 'ubuntu' or
            env.get(b'XDG_SESSION_TYPE') != 'wayland' or '--nested' not in argv or '--wayland' not in argv or len(displays) != 1 or
            not re.fullmatch(r'/tmp/local-gnome-[A-Za-z0-9_-]+/wayland-0', displays[0])):
        return None
    display = Path(displays[0])
    try:
        directory, socket = display.parent.lstat(), display.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077 or
                not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != os.getuid()):
            return None
    except OSError:
        return None
    address = env.get(b'DBUS_SESSION_BUS_ADDRESS', '')
    if valid_bus(address):
        return {'pid': pid, 'start': start, 'bus': address, 'display': str(display), 'socket': identity(socket)}
    return None


def call(address, dest, path, method, *arguments, timeout=4):
    try:
        result = subprocess.run(['/usr/bin/gdbus', 'call', '--session', '--timeout', str(timeout), '--dest', dest,
                                 '--object-path', path, '--method', method, *arguments], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'DBUS_SESSION_BUS_ADDRESS': address}, timeout=timeout+2)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise Failure('bus_unavailable') from exc
    if result.returncode:
        raise Failure('bus_unavailable')
    return result.stdout.strip()


def bus_call(address, method, *arguments):
    return call(address, DBUS, '/org/freedesktop/DBus', DBUS+'.'+method, *arguments)


def service_owner(address, name):
    def unique():
        m = re.fullmatch(r"\('(:[0-9]+\.[0-9]+)',\)", bus_call(address, 'GetNameOwner', name))
        if not m:
            raise Failure('bus_unavailable')
        return m[1]
    owner = unique()
    m = re.fullmatch(r'\(uint32 (\d+),\)', bus_call(address, 'GetConnectionUnixProcessID', owner))
    if not m or unique() != owner:
        raise Failure('session_changed')
    return owner, int(m[1])


def shell_owner(shell):
    unique, pid = service_owner(shell['bus'], 'org.gnome.Shell')
    if pid != shell['pid']:
        raise Failure('session_changed')
    return unique


def bus_id(address):
    match = re.fullmatch(r"\('([a-fA-F0-9]{32})',\)", bus_call(address, 'GetId'))
    if not match:
        raise Failure('bus_unavailable')
    return match[1]


def files_owner(shell):
    unique, pid = service_owner(shell['bus'], FM)
    proc = proc_details(pid, '/usr/bin/nautilus', {b'WAYLAND_DISPLAY'})
    if not proc or proc[2].get(b'WAYLAND_DISPLAY') != shell['display']:
        raise Failure('session_changed')
    return {'owner': unique, 'pid': pid, 'start': proc[0]}


def active(binding, state):
    shell = binding['shell']
    try:
        return (state['boot'] == boot_id() and shell_details(shell['pid'], state['nonce']) == shell and
                bus_id(shell['bus']) == binding['busId'] and shell_owner(shell) == binding['shellOwner'] and
                files_owner(shell) == binding['files'])
    except Failure:
        return False


def discover(state):
    if state['boot'] != boot_id():
        raise Failure('session_changed')
    matches = []
    for p in PROC.iterdir():
        if p.name.isdecimal():
            shell = shell_details(int(p.name), state['nonce'])
            if shell and shell['start'] >= state['start']:
                matches.append(shell)
    if len(matches) > 1:
        raise Failure('ambiguous_session')
    return matches[0] if matches else None


def public(status, binding, expected):
    result = {'status': status, 'shellPid': binding['shell']['pid'], 'filesPid': binding['files']['pid'], 'expected': expected}
    if status == 'attached':
        result.update(backend='nested', ubuntuVersion=binding['ubuntuVersion'], wslgPresent=Path('/mnt/wslg/versions.txt').is_file())
    return result


def ubuntu_version():
    release = platform.freedesktop_os_release()
    version = release.get('VERSION_ID', '')
    if release.get('ID') != 'ubuntu' or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', version):
        raise Failure('unsupported_os')
    return version


def attach(root, fd, files_fd, state, expected):
    existing = state.get('binding')
    if existing:
        if not active(existing, state):
            raise Failure('session_changed')
        return public('attached', existing, expected)
    shell = discover(state)
    if not shell:
        return {'status': 'not_ready'}
    if entry(files_fd, expected):
        raise Failure('unexpected_entry')
    try:
        version = ubuntu_version()
        server = bus_id(shell['bus'])
        unique = shell_owner(shell)
        bus_call(shell['bus'], 'StartServiceByName', FM, '0')
        files = files_owner(shell)
    except Failure as exc:
        if exc.code == 'bus_unavailable':
            return {'status': 'not_ready'}
        raise
    try:
        call(shell['bus'], files['owner'], '/org/freedesktop/FileManager1', FM+'.ShowFolders',
             json.dumps([(root/'Files').as_uri()]), '', timeout=45)
    except Failure as exc:
        raise Failure('file_manager_error') from exc
    binding = {'shell': shell, 'busId': server, 'shellOwner': unique, 'files': files, 'ubuntuVersion': version}
    if not active(binding, state) or not same_dir(entry(fd, 'Files'), state['files']):
        raise Failure('session_changed')
    if entry(files_fd, expected):
        raise Failure('unexpected_entry')
    state['binding'] = binding
    write_state(fd, state)
    return public('attached', binding, expected)


def active_binding(state):
    binding = state.get('binding')
    if not binding:
        raise Failure('not_attached')
    if not active(binding, state):
        raise Failure('session_changed')
    return binding


def prompt(fd, files_fd, state, expected):
    binding = active_binding(state)
    if entry(files_fd, expected):
        raise Failure('unexpected_entry')
    if not state.get('prompted'):
        try:
            call(binding['shell']['bus'], 'org.freedesktop.Notifications', '/org/freedesktop/Notifications',
                 'org.freedesktop.Notifications.Notify', 'Ubuntu 桌面验证', '0', '', 'Ubuntu 桌面验证',
                 f'在此窗口按 Ctrl+Shift+N，新建文件夹 {expected}', '[]', '{}', '10000', timeout=2)
        except Failure as exc:
            raise Failure('notification_failed') from exc
        state['prompted'] = True
        write_state(fd, state)
    return public('prompted', binding, expected)


def check(fd, files_fd, state, expected):
    binding = active_binding(state)
    info = entry(files_fd, expected)
    if not info:
        return {'status': 'pending', 'expected': expected}
    if not state.get('prompted') or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Failure('unexpected_entry')
    challenge = os.open(expected, DIR_FLAGS, dir_fd=files_fd)
    try:
        if (not os.path.samestat(info, os.fstat(challenge)) or os.listdir(challenge) or
                not same_dir(entry(files_fd, expected), identity(info)) or not same_dir(entry(fd, 'Files'), state['files'])):
            raise Failure('unexpected_entry')
    finally:
        os.close(challenge)
    verified = [*identity(info), info.st_ctime_ns]
    if 'verified' in state and state['verified'] != verified:
        raise Failure('unexpected_entry')
    if 'verified' not in state:
        state['verified'] = verified
        write_state(fd, state)
    return public('verified', binding, expected)


def remove_empty(path, parent=None):
    try:
        os.rmdir(path, dir_fd=parent)
        return True
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
            return False
        raise


def finish_cleanup(root, fd, state):
    if set(os.listdir(fd)) != {STATE} or not os.path.samestat(root.lstat(), os.fstat(fd)):
        return {'status': 'preserved'}
    if read_state(fd, state['nonce']) != state:
        return {'status': 'preserved'}
    os.unlink(STATE, dir_fd=fd)
    if remove_empty(root):
        return {'status': 'cleaned'}
    # Someone added an entry just before rmdir. Restore our marker exclusively for future cleanup.
    try:
        marker = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    except FileExistsError:
        return {'status': 'preserved'}
    with os.fdopen(marker, 'w', encoding='utf-8') as stream:
        json.dump(state, stream, separators=(',', ':'))
    return {'status': 'preserved'}


def cleanup(root, fd, files_fd, state, expected):
    # Unknown additions and replacements are user data. Leave the complete fixture for inspection.
    children = os.listdir(files_fd)
    if set(os.listdir(fd)) != {STATE, 'Files'} or set(children) - {expected}:
        return {'status': 'preserved'}
    if expected in children:
        info = entry(files_fd, expected)
        if not info or not same_dir(info, state.get('verified', [])[:2]) or state['verified'][2:] != [info.st_ctime_ns]:
            return {'status': 'preserved'}
        target = os.open(expected, DIR_FLAGS, dir_fd=files_fd)
        try:
            if os.listdir(target) or not os.path.samestat(os.fstat(target), info) or not same_dir(entry(files_fd, expected), identity(info)):
                return {'status': 'preserved'}
            if not remove_empty(expected, files_fd):
                return {'status': 'preserved'}
        finally:
            os.close(target)
    if os.listdir(files_fd) or not same_dir(entry(fd, 'Files'), state['files']):
        return {'status': 'preserved'}
    state['cleaning'] = True
    write_state(fd, state)
    if not remove_empty('Files', fd):
        return {'status': 'preserved'}
    return finish_cleanup(root, fd, state)


def execute(command, nonce):
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise Failure('invalid_nonce')
    expected = 'Verified-' + nonce[:8]
    root = Path('/tmp') / f'cloudworkspace-verify-{os.getuid()}-{nonce}'
    if command == 'prepare':
        return prepare(root, nonce, expected)
    if command not in ('attach', 'prompt', 'check', 'cleanup'):
        raise Failure('invalid_command')
    try:
        with locked_root(root) as fd:
            state = read_state(fd, nonce)
            files_info = entry(fd, 'Files')
            if state.get('cleaning') and command != 'cleanup':
                raise Failure('unsafe_fixture')
            if command == 'cleanup' and state.get('cleaning') and files_info is None:
                return finish_cleanup(root, fd, state)
            if not same_dir(files_info, state['files']):
                if command == 'cleanup':
                    return {'status': 'preserved'}
                raise Failure('unsafe_fixture')
            files_fd = os.open('Files', DIR_FLAGS, dir_fd=fd)
            try:
                if not same_dir(os.fstat(files_fd), state['files']):
                    raise Failure('unsafe_fixture')
                if command == 'attach':
                    return attach(root, fd, files_fd, state, expected)
                if command == 'prompt':
                    return prompt(fd, files_fd, state, expected)
                if command == 'check':
                    return check(fd, files_fd, state, expected)
                return cleanup(root, fd, files_fd, state, expected)
            finally:
                os.close(files_fd)
    except FileNotFoundError as exc:
        if root.exists() or root.is_symlink():
            raise Failure('unsafe_fixture') from exc
        if command == 'cleanup':
            return {'status': 'cleaned'}
        raise Failure('not_prepared') from exc


def main(argv):
    try:
        if len(argv) != 2:
            raise Failure('invalid_arguments')
        result = execute(*argv)
    except Failure as exc:
        result = {'status': 'error', 'code': exc.code}
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        result = {'status': 'error', 'code': 'unsafe_fixture'}
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return int(result['status'] == 'error')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
