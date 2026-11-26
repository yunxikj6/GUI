"""Dispatch the native desktop entry without starting the HTTP workbench."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def start(check=False, distro=None, setup=False):
    if sys.platform == 'win32':
        machine = (os.environ.get('PROCESSOR_ARCHITEW6432') or os.environ.get('PROCESSOR_ARCHITECTURE', '')).lower()
        architecture = {'amd64': 'x64', 'arm64': 'arm64'}.get(machine)
        if architecture is None:
            print('本机入口需要 x64 或 ARM64 版 Windows。', file=sys.stderr)
            return 2
        command = [str(ROOT / 'windows' / f'CloudLinuxWorkspace-{architecture}.exe')]
        if distro:
            command += ['--distro', distro]
        if setup:
            command.append('--setup-native')
        elif check:
            command.append('--check-native')
        elif not distro:
            command.append('--native')
    elif sys.platform.startswith('linux') and not distro:
        if setup and not os.environ.get('WSL_DISTRO_NAME'):
            print('桌面依赖安装入口适用于 Windows 上的 Ubuntu 24.04 WSLg。', file=sys.stderr)
            return 2
        script = 'setup-ubuntu.sh' if setup else 'start-workspace.sh'
        command = ['/bin/bash', str(ROOT / 'linux' / script)]
        if os.environ.get('WSL_DISTRO_NAME'):
            command.append('--wsl')
        if check:
            command.append('--check')
    else:
        print('本机桌面入口支持 Windows WSL 2 或有图形会话的 Linux；--distro 仅用于 Windows。', file=sys.stderr)
        return 2
    env = os.environ.copy()
    env.pop('INSTABOX_API_KEY', None)
    env.pop('CLOUDWORKSPACE_RENDER_TOKEN', None)
    try:
        return subprocess.run(command, cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f'无法启动本机桌面：{exc}', file=sys.stderr)
        return 1
