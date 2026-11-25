"""Merge the selected read-only project into the current user's Claude Desktop settings."""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat

from workspace_mcp import is_previous_readonly_server

_SERVER = "instabox-workspace"
_SPACE = re.compile(r"[ \t\r\n]*")
_CHANGED = "Claude 配置在保存期间已改变。请检查当前配置后重试。"


def config_path():
    root = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not root.is_absolute():
        root = Path.home() / ".config"
    return root / "Claude" / "claude_desktop_config.json"


def _invalid_constant(_):
    raise ValueError("Claude 配置包含无效的 JSON 数字。")


# Only offsets matter outside the managed entry. Preserve even arbitrarily large
# numeric literals and duplicate keys in unrelated settings verbatim.
_DECODER = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=_invalid_constant)


def _member(text, start, wanted):
    """Locate one member in a validated object, or its insertion point."""
    if text[start] != "{":
        raise ValueError("Claude 配置及其中的 mcpServers 必须是 JSON 对象。")
    pos = _SPACE.match(text, start + 1).end()
    insert_at = start + 1
    found = None
    while text[pos] != "}":
        key, end = _DECODER.raw_decode(text, pos)
        colon = _SPACE.match(text, end).end()
        value = _SPACE.match(text, colon + 1).end()
        _, end = _DECODER.raw_decode(text, value)
        if key == wanted:
            if found is not None:
                raise ValueError(f"Claude 配置有重复的 {wanted} 项，请先在 Claude 中检查配置。")
            found = (value, end)
        insert_at = end
        pos = _SPACE.match(text, end).end()
        if text[pos] == ",":
            pos = _SPACE.match(text, pos + 1).end()
    return found, insert_at


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Claude 中同名项目连接含有重复的配置键，请先检查该连接。")
    return result


def _merge(original, server, previous):
    if original is None:
        return (json.dumps({"mcpServers": {_SERVER: server}}, indent=2) + "\n").encode()
    bom = original.startswith(b"\xef\xbb\xbf")
    try:
        text = original.decode("utf-8-sig")
        root = _SPACE.match(text).end()
        _, end = _DECODER.raw_decode(text, root)
        if _SPACE.match(text, end).end() != len(text):
            raise ValueError("Claude 配置中有多余的 JSON 内容。")
        group, pos = _member(text, root, "mcpServers")
        name, value = "mcpServers", {_SERVER: server}
        found = None
        if group is not None:
            root = group[0]
            found, pos = _member(text, root, _SERVER)
            name, value = _SERVER, server
        if found is not None:
            start, end = found
            current = json.loads(text[start:end], object_pairs_hook=_unique)
            if current == server:
                return original
            if not (previous is not None and current == previous
                    or is_previous_readonly_server(current, server, host="claude")):
                raise ValueError("Claude 已有其他 instabox-workspace 连接。请先在 Claude 中检查同名配置。")
            text = text[:start] + json.dumps(server) + text[end:]
        else:
            prefix = ", " if pos != root + 1 else ""
            text = text[:pos] + prefix + json.dumps(name) + ": " + json.dumps(value) + text[pos:]
        return text.encode("utf-8-sig" if bom else "utf-8")
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Claude 配置不是有效的 UTF-8 JSON，请先在 Claude 中检查配置。") from None


def _identity(st):
    # rename updates ctime even when the underlying data and permissions are unchanged.
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns


def _identity_at(directory, name):
    try:
        return _identity(os.stat(name, dir_fd=directory, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _read(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("Claude 配置文件是符号链接，请在 Claude 中手动合并连接。") from None
        raise
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("Claude 配置须是当前用户拥有的普通文件，请在 Claude 中检查。")
    with os.fdopen(fd, "rb") as f:
        data = f.read()
        if st.st_ctime_ns != os.fstat(fd).st_ctime_ns:
            raise ValueError(_CHANGED)
    return st, data


def _matches(directory, name, stamp, data):
    if _identity_at(directory, name) != stamp:
        return False
    if data is None:
        return True
    try:
        current, content = _read(directory, name)
    except (OSError, ValueError):
        return False
    return current is not None and _identity(current) == stamp and content == data


def _exchange(directory, left, right):
    # Atomically retain what *actually* occupied b, even for editors which ignore flock.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is not None and rename(directory, os.fsencode(left), directory, os.fsencode(right), 2) == 0:
        return
    error = ctypes.get_errno() if rename is not None else errno.ENOSYS
    if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise ValueError("此文件系统不支持安全地替换现有配置。请在 Claude 的“编辑配置”中手动合并连接。")
    raise OSError(error, os.strerror(error))


def _restore(directory, name, temporary, installed, data):
    """Restore a concurrent edit while it is still our version at the destination."""
    while _matches(directory, name, installed, data):
        incoming = _identity_at(directory, temporary)
        incoming_data = None
        try:
            _, incoming_data = _read(directory, temporary)
        except (OSError, ValueError):
            pass  # Can also restore a concurrently installed symlink without following it.
        try:
            _exchange(directory, temporary, name)
        except FileNotFoundError:
            break
        if _matches(directory, temporary, installed, data):
            return True
        # A third writer won the race just before the rollback. Its file is now
        # at temporary. Restore that newer version while the prior one is visible.
        installed, data = incoming, incoming_data
    return False


def _configure(directory, name, server, previous):
    st, original = _read(directory, name)
    merged = _merge(original, server, previous)
    expected = _identity(st) if st else None
    if merged == original:
        if not _matches(directory, name, expected, original):
            raise ValueError(_CHANGED)
        return False

    temporary = ".cloudworkspace-claude-" + secrets.token_hex(12)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    remove_temporary = True
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(merged)
            f.flush()
            if st:
                os.fchmod(fd, st.st_mode & 0o777)
            os.fsync(fd)
            installed = _identity(os.fstat(fd))
        if st is None:
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            except FileExistsError:
                raise ValueError(_CHANGED) from None
        else:
            if _identity_at(directory, name) != expected:
                raise ValueError(_CHANGED)
            try:
                _exchange(directory, temporary, name)
            except FileNotFoundError:
                raise ValueError(_CHANGED) from None
            # temporary now holds a real editor's file until its bytes are verified.
            remove_temporary = False
            if not _matches(directory, temporary, expected, original):
                try:
                    remove_temporary = _restore(directory, name, temporary, installed, merged)
                except (OSError, ValueError):
                    pass
                if not remove_temporary:
                    raise ValueError(f"{_CHANGED} 另一次修改保存在配置目录的 {temporary} 中。")
                os.fsync(directory)
                raise ValueError(_CHANGED)
            remove_temporary = True
        os.fsync(directory)
        return True
    finally:
        if remove_temporary:
            os.unlink(temporary, dir_fd=directory)


def configure(server: dict, *, previous: dict | None = None, path: Path | None = None) -> bool:
    """Return True when written; report ambiguous settings or concurrent changes as ValueError."""
    if (not isinstance(server, dict) or not isinstance(server.get("command"), str)
            or not os.path.isabs(server["command"]) or not is_previous_readonly_server(server, server, host="claude")):
        raise ValueError("请选择有效的云机和绝对项目目录以建立只读 Claude 连接。")
    path = Path(path) if path is not None else config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        return _configure(directory, path.name, server, previous)
    finally:
        os.close(directory)
