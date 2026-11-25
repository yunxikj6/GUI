"""Find an Ubuntu Python for the stdio adapter or install its dependencies after consent."""
import ast
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

DOWNLOAD_OPTIONS = ("index-url", "extra-index-url", "find-links", "no-index", "trusted-host",
                    "proxy", "cert", "client-cert", "timeout", "retries", "keyring-provider")

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements-mcp.txt"

# Run in the candidate interpreter. Checking imports also catches broken native dependencies.
PROBE = r"""
import re
import sys
from importlib.metadata import version
from pathlib import Path
if sys.version_info < (3, 10):
    sys.exit(1)
def release(number):
    return tuple(map(int, re.sub(r"(?:\.0)+$", "", number).split(".")))
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = line.partition("#")[0].strip()
    if not line:
        continue
    # The manifest specifies inclusive/exclusive stable release bounds. A working
    # stdio environment may have neither pip nor packaging installed.
    spec = re.fullmatch(r"([\w.-]+)\s*>=\s*([\d.]+)\s*,\s*<\s*([\d.]+)", line)
    if not spec:
        sys.exit(1)
    name, lower, upper = spec.groups()
    found = re.fullmatch(r"(?:0!)?(\d+(?:\.\d+)*)(?:\.post\d+)?(?:\+[a-z0-9._-]+)?",
                         version(name), re.IGNORECASE)
    if not found or not release(lower) <= release(found[1]) < release(upper):
        sys.exit(1)
from keyring.backends.SecretService import Keyring
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
"""


def _storage():
    data = Path(os.environ.get("XDG_DATA_HOME") or "")
    if not data.is_absolute():
        data = Path.home() / ".local" / "share"
    return data / "cloudworkspace" / "mcp-runtime"


def _private(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _available(commands):
    seen = set()
    for command in commands:
        if not command:
            continue
        path = Path(command).absolute()
        # The venv's python can be a symlink to the system python. Preserve its own path.
        if path not in seen and path.is_file() and os.access(path, os.X_OK):
            seen.add(path)
            yield str(path)


def _external(preferred):
    activated = os.environ.get("VIRTUAL_ENV")
    return (preferred, ROOT / ".venv-mcp" / "bin" / "python",
            Path(activated) / "bin" / "python" if activated and os.path.isabs(activated) else None,
            sys.executable, shutil.which("python3"), "/usr/bin/python3")


def _child_env():
    env = os.environ.copy()
    env.pop("INSTABOX_API_KEY", None)
    return env


def _usable(python):
    try:
        result = subprocess.run([python, "-E", "-B", "-c", PROBE, str(REQUIREMENTS)],
                                cwd="/", env=_child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_runtime(preferred=None) -> str | None:
    """Read-only discovery. Return an absolute interpreter path, preserving the venv."""
    storage = _storage()
    managed = []
    try:
        if _private(storage):
            for env in sorted(storage.glob("venv-*")):
                if _private(env) and (env / ".ready").is_file():
                    managed.append(env / "env" / "bin" / "python")
    except OSError:
        pass
    external = _external(preferred)
    return next((p for p in _available([preferred, *managed, *external[1:]]) if _usable(p)), None)


def _install(command, env, message, timeout):
    try:
        subprocess.run(command, env=env, cwd="/", stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(message) from error


def _pip_env(python, env):
    # Ask this pip to discover system, user, site and PIP_CONFIG_FILE settings. Keep
    # only download options, so a configured target, root or prefix cannot escape the venv.
    try:
        result = subprocess.run([python, "-I", "-m", "pip", "config", "list"], env=env, cwd="/",
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                check=True, timeout=20)
        settings = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.partition(".")[2] in DOWNLOAD_OPTIONS:
                settings[key] = ast.literal_eval(value)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as error:
        raise RuntimeError("无法读取 pip 软件包索引配置，请检查 pip config list 后重试。") from error

    safe = {key: value for key, value in env.items() if not key.startswith("PIP_")}
    safe["PIP_CONFIG_FILE"] = os.devnull
    for option in DOWNLOAD_OPTIONS:
        key = "PIP_" + option.upper().replace("-", "_")
        value = env.get(key) or settings.get("install." + option) or settings.get("global." + option)
        if value:
            safe[key] = value
    return safe


def prepare_runtime(preferred=None, *, progress=None) -> str:
    """Called after installation consent. Reuse working environments without modifying them."""
    runtime = find_runtime(preferred)
    if runtime:
        return runtime

    storage = _storage()
    try:
        storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _private(storage):
            raise RuntimeError("MCP 运行环境目录必须是当前用户独占的普通目录。")
        fd = os.open(storage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise RuntimeError("无法打开用户的 MCP 运行环境目录，请检查目录权限。") from error

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if progress:
                progress("正在等待另一项 MCP 运行环境安装完成…")
            fcntl.flock(fd, fcntl.LOCK_EX)
        runtime = find_runtime(preferred)
        if runtime:
            return runtime
        # An interrupted pip may have left a partial venv. Completed generations can still
        # be referenced by an existing client and stay in place across upgrades.
        for entry in storage.glob("venv-*"):
            if _private(entry) and (entry / ".installing").is_file():
                shutil.rmtree(entry)

        generation = Path(tempfile.mkdtemp(prefix="venv-", dir=storage))
        try:
            pending = generation / ".installing"
            pending.touch()
            if progress:
                progress("正在创建用户专用 Python 环境…")
            env = _child_env()
            for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
                env.pop(key, None)
            destination = generation / "env"
            creators = _available(_external(preferred))
            for python in creators:
                try:
                    _install([python, "-I", "-c",
                              "import ensurepip, sys, venv; sys.exit(sys.version_info < (3, 10))"],
                             env, "此 Python 缺少所需的 venv 组件。", 10)
                    _install([python, "-I", "-m", "venv", "--clear", str(destination)], env,
                             "无法创建 Python 虚拟环境。", 120)
                    break
                except RuntimeError:
                    continue
            else:
                raise RuntimeError("无法创建 Python 虚拟环境。请安装 Ubuntu 的 python3-venv 后重试。")

            installed = str(destination / "bin" / "python")
            if progress:
                progress("正在安装 MCP 与系统密钥环依赖，首次安装可能需要几分钟…")
            _install([installed, "-I", "-m", "pip", "--disable-pip-version-check", "--no-input",
                      "install", "--no-user", "-r", str(REQUIREMENTS)], _pip_env(installed, env),
                     "MCP 依赖安装失败。请检查 Python 软件包索引和网络连接后重试。", 600)
            if not _usable(installed):
                raise RuntimeError("安装后的 Python 无法加载 MCP 与系统密钥环依赖。请检查后重试。")
            pending.rename(generation / ".ready")
            return installed
        except BaseException:
            shutil.rmtree(generation, ignore_errors=True)
            raise
    except OSError as error:
        raise RuntimeError("无法写入用户的 MCP 运行环境，请检查磁盘空间和目录权限。") from error
    finally:
        os.close(fd)
