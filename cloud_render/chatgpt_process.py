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
def _own_profile(path, *, marker_name=PROFILE_MARKER):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('云端配置目录不能包含上级目录引用。')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open('/', flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=directory)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        state = os.fstat(directory)
        home = Path.home()
        if home.exists() and os.path.samestat(state, home.stat()):
            raise ValueError('请在主目录内另建专用云端配置目录。')
        if state.st_uid != os.geteuid() or state.st_mode & 0o077:
            raise PermissionError('云端工作区配置目录须属于当前用户且仅该用户可访问。')
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('该云端配置已有运行中的工作区。') from exc
        entries = os.listdir(directory)
        if not entries:
            marker = os.open(marker_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory)
            os.close(marker)
        else:
            marker = (os.stat(marker_name, dir_fd=directory, follow_symlinks=False)
                      if marker_name in entries else None)
            if (not marker or not stat.S_ISREG(marker.st_mode) or marker.st_uid != state.st_uid or
                    marker.st_mode & 0o077 or marker.st_nlink != 1):
                raise RuntimeError('请选择空目录或此前由本服务建立的专用配置目录。')
        yield directory
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
async def open_chatgpt(profile: Path, *, executable: Path = Path('/usr/bin/chatgpt'), system_browser=None):
    from playwright.async_api import async_playwright

    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError('云端缺少可执行的 ChatGPT 客户端。')
    # Existing standalone deployments may have a symlinked home or parent.
    # Resolve its parent once and keep the profile directory itself no-follow.
    root = profile.absolute()
    root = root.parent.resolve() / root.name
    with _own_profile(root):
        home = root / 'home'
        home.mkdir(mode=0o700, exist_ok=True)
        user_data = root / 'chromium'
        env = _cloud_environment(home)
        if system_browser is not None:
            env = system_browser.environment(env)
        async with async_playwright() as owner:
            # Use only the private pipe and the app profile. Playwright's ordinary test
            # switches would change the vendor password store, features and renderer sandbox.
            context = await owner.chromium.launch_persistent_context(
                str(user_data), executable_path=str(executable), headless=False, chromium_sandbox=True,
                no_viewport=True, ignore_default_args=True, env=env,
                args=['--remote-debugging-pipe', '--user-data-dir=' + str(user_data)], timeout=30000,
            )
            browser = context.browser
            try:
                page = await asyncio.wait_for(_original_window(context), timeout=30)
                yield page
            finally:
                if browser is None or browser.is_connected():
                    await context.close()
