"""Register the real system browser in one private cloud desktop session."""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil


DESKTOP_ID = 'cloud-workspace-system-browser.desktop'


def _exec_argument(value):
    # Exec is parsed twice: the Desktop Entry string, then its quoted argv.
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid control character in the browser launch path.')
    escaped = ''.join('\\' + c if c in '\\"$`' else c for c in value)
    return '"' + escaped.replace('\\', '\\\\').replace('%', '%%') + '"'


@dataclass(frozen=True)
class SystemBrowserRegistration:
    root: Path
    executable: Path
    platform: str

    @property
    def profile(self):
        return self.root / 'profile'

    @property
    def arguments(self):
        return (f'--user-data-dir={self.profile}', f'--ozone-platform={self.platform}',
                '--no-first-run', '--no-default-browser-check')

    @classmethod
    def create(cls, root: Path, *, executable: Path, platform: str):
        """Create a fresh registration inside the source owner's session scope.

        XDG gives the URL to Chromium itself. Reopening a URL uses Chromium's
        existing profile singleton in the same display and network namespace.
        The session owner closes its browser before reclaiming this directory.
        """
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('An installed cloud Chromium executable is required.')
        if platform not in ('x11', 'wayland'):
            raise ValueError('An X11 or Wayland cloud display is required.')
        if not root.is_absolute() or ':' in str(root) or '..' in root.parts:
            raise ValueError('An absolute session directory usable in XDG search paths is required.')
        record = cls(root, executable, platform)
        command = ' '.join(_exec_argument(arg) for arg in (str(executable), *record.arguments))
        root.mkdir(mode=0o700)
        try:
            apps = root / 'data' / 'applications'
            apps.mkdir(parents=True)
            config = root / 'config'
            config.mkdir()
            record.profile.mkdir(mode=0o700)
            (apps / DESKTOP_ID).write_text(
                '[Desktop Entry]\nType=Application\nName=Chromium\nNoDisplay=true\n'
                f'Exec={command} --new-window -- %u\nTerminal=false\nMimeType=x-scheme-handler/https;\n',
                encoding='utf-8')
            (config / 'mimeapps.list').write_text(
                f'[Default Applications]\nx-scheme-handler/https={DESKTOP_ID};\n', encoding='utf-8')
        except BaseException:
            shutil.rmtree(root)
            raise
        return record

    def environment(self, base):
        """Use this handler for the app without changing the user's defaults."""
        env = dict(base)
        for key, folder, fallback in (
                ('XDG_CONFIG_DIRS', 'config', '/etc/xdg'),
                ('XDG_DATA_DIRS', 'data', '/usr/local/share:/usr/share')):
            rest = env.get(key) or fallback
            env[key] = os.pathsep.join((str(self.root / folder), rest))
        return env
