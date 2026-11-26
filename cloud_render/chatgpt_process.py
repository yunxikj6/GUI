"""Start an isolated instance of the cloud-installed OpenAI desktop application."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import os
from pathlib import Path
import stat


APP_PAGE = 'app://-/index.html'
PROFILE_MARKER = '.cloudworkspace-chatgpt-v1'


@contextmanager
def _own_profile(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        state = os.fstat(directory)
        if state.st_uid != os.geteuid() or state.st_mode & 0o077:
            raise PermissionError('云端工作区配置目录须属于当前用户且仅该用户可访问。')
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('该云端配置已有运行中的工作区。') from exc
        entries = os.listdir(directory)
        if not entries:
            marker = os.open(PROFILE_MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory)
            os.close(marker)
        elif PROFILE_MARKER not in entries or not stat.S_ISREG(os.stat(
            PROFILE_MARKER, dir_fd=directory, follow_symlinks=False
        ).st_mode):
            raise RuntimeError('请选择空目录或此前由本服务建立的专用配置目录。')
        yield
    finally:
        os.close(directory)


def _cloud_environment(home):
    env = {name: value for name, value in os.environ.items() if name in (
        'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'XDG_SESSION_TYPE',
        'XDG_CURRENT_DESKTOP', 'XDG_DATA_DIRS', 'DBUS_SESSION_BUS_ADDRESS', 'LANG', 'LANGUAGE',
        'LC_ALL', 'LC_CTYPE', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY',
        'NO_PROXY', 'https_proxy', 'http_proxy', 'no_proxy')}
    if 'DISPLAY' in env and 'XAUTHORITY' not in env:
        xauthority = Path.home() / '.Xauthority'
        if xauthority.is_file():
            env['XAUTHORITY'] = str(xauthority)
    env.update(HOME=str(home), PATH='/usr/local/bin:/usr/bin:/bin',
               XDG_CONFIG_HOME=str(home / '.config'), XDG_CACHE_HOME=str(home / '.cache'),
               XDG_DATA_HOME=str(home / '.local/share'), XDG_STATE_HOME=str(home / '.local/state'),
               CODEX_HOME=str(home / '.codex'))
    return env


async def _original_window(context):
    while True:
        matches = [page for page in context.pages if page.url == APP_PAGE]
        if len(matches) > 1:
            raise RuntimeError('云端客户端打开了多个原厂主窗口，无法选择唯一的显示目标。')
        if matches:
            return matches[0]
        await asyncio.sleep(0.05)


@asynccontextmanager
async def open_chatgpt(profile: Path, *, executable: Path = Path('/usr/bin/chatgpt')):
    from playwright.async_api import async_playwright

    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError('云端缺少可执行的 ChatGPT 客户端。')
    root = profile.absolute()
    with _own_profile(root):
        home = root / 'home'
        home.mkdir(mode=0o700, exist_ok=True)
        user_data = root / 'chromium'
        async with async_playwright() as owner:
            # Use only the private pipe and the app profile. Playwright's ordinary test
            # switches would change the vendor password store, features and renderer sandbox.
            context = await owner.chromium.launch_persistent_context(
                str(user_data), executable_path=str(executable), headless=False, chromium_sandbox=True,
                no_viewport=True, ignore_default_args=True, env=_cloud_environment(home),
                args=['--remote-debugging-pipe', '--user-data-dir=' + str(user_data)], timeout=30000,
            )
            try:
                page = await asyncio.wait_for(_original_window(context), timeout=30)
                yield page
            finally:
                await context.close()
