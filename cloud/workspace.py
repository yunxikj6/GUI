"""One-request, JSON-only Linux workspace API for the authenticated local bridge."""

import base64
import binascii
import codecs
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import heapq
import io
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path("/data/cloud-workspace/project")
PRIVATE = {".git", ".cloudwork"}
MAX_ENTRIES = 400
_TREE_PAGE_BYTES = 256 * 1024
_TREE_CURSOR_LIMIT = 8192
_NO_TREE_CURSOR = object()
_NO_DIFF_SCOPE = object()
_DIFF_SCOPES = ("staged", "unstaged")
MAX_TEXT = 128 * 1024
MAX_IMAGE = 256 * 1024
MAX_DIFF = 64 * 1024
MAX_REQUEST = 832 * 1024
TEMP_PREFIX = ".cloudwork-save-"
NO_LINK = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
DIRECTORY = NO_LINK | os.O_DIRECTORY
VISIBLE_PATHS = [
    ".", ":(exclude,glob)**/.cloudwork", ":(exclude,glob)**/.cloudwork/**",
    ":(exclude,glob)**/.git", ":(exclude,glob)**/.git/**", ":(exclude,glob)**/.cloudwork-save-*",
]


class WorkspaceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def relative_parts(path):
    if not isinstance(path, str) or len(path) > 4096 or "\x00" in path:
        raise WorkspaceError("invalid_path", "请输入工作区中的相对路径。")
    if not path:
        return []
    parts = path.split("/")
    if any(part in ("", ".", "..") or part in PRIVATE or part.startswith(TEMP_PREFIX) for part in parts):
        raise WorkspaceError("invalid_path", "此路径不属于可访问的工作区。")
    return parts


@contextmanager
def open_path(root, parts, directory=False):
    """Traverse from a pinned directory. Never follow a link, including at the leaf."""
    fd = os.dup(root) if isinstance(root, int) else os.open(root, DIRECTORY)
    try:
        for index, name in enumerate(parts):
            flags = DIRECTORY if directory or index < len(parts) - 1 else NO_LINK | os.O_NONBLOCK
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _directory_id(fd):
    info = os.fstat(fd)
    birth = getattr(info, "st_birthtime_ns", None)
    if birth is None and sys.platform.startswith("linux"):
        # statx on an open descriptor supplies birth time on Linux filesystems
        # that support it. Unlike ctime it survives insertion/deletion of children.
        import ctypes
        import struct
        statx = getattr(ctypes.CDLL(None), "statx", None)
        data = ctypes.create_string_buffer(256)  # Linux struct statx
        if statx is not None and statx(fd, b"", 0x1000, 0x0800, data) == 0:  # AT_EMPTY_PATH, STATX_BTIME
            mask, = struct.unpack_from("=I", data)
            if mask & 0x0800:
                seconds, nanos = struct.unpack_from("=qI", data, 80)  # stx_btime
                birth = seconds * 1_000_000_000 + nanos
    return f"{info.st_dev}:{info.st_ino}:{birth}".encode("ascii")


def _tree_scope(root, path, root_fd, fd):
    project_id = _directory_id(root_fd)
    fields = (b"tree-v1", os.fsencode(os.path.abspath(root)), project_id, os.fsencode(path),
              _directory_id(fd) if path else project_id)
    return hashlib.sha256(b"\x00".join(fields)).digest()


def _tree_cursor(scope, key):
    is_file, _, name = key
    anchor = (b"f" if is_file else b"d") + os.fsencode(name)
    checksum = hashlib.sha256(scope + anchor).digest()[:16]
    return base64.urlsafe_b64encode(checksum + anchor).decode("ascii")


def _tree_boundary(cursor, scope):
    error = WorkspaceError("invalid_cursor", "目录分页位置已失效，请重新加载此目录。")
    if not isinstance(cursor, str) or not 0 < len(cursor) <= _TREE_CURSOR_LIMIT:
        raise error
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise error from None
    tag, name = raw[16:17], os.fsdecode(raw[17:])
    key = (tag == b"f", name.casefold(), name)
    if tag not in (b"d", b"f") or not name or _tree_cursor(scope, key) != cursor:
        raise error
    return key


def _tree_keys(fd, boundary):
    with os.scandir(fd) as listing:
        for entry in listing:
            name = entry.name
            if name in PRIVATE or name.startswith(TEMP_PREFIX):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    is_file = False
                elif entry.is_file(follow_symlinks=False):
                    is_file = True
                else:
                    continue
            except FileNotFoundError:
                continue
            key = (is_file, name.casefold(), name)
            if boundary is None or key > boundary:
                yield key


def _tree_wire_bytes(data):
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace"))


def tree(root, path, cursor=_NO_TREE_CURSOR):
    parts = relative_parts(path)
    result = {"path": path, "entries": [], "truncated": False, "nextCursor": None}
    remaining = _TREE_PAGE_BYTES - _tree_wire_bytes({"ok": True, "data": result}) - _TREE_CURSOR_LIMIT
    try:
        with open_path(root, [], directory=True) as root_fd, open_path(root_fd, parts, directory=True) as fd:
            scope = None if cursor is _NO_TREE_CURSOR else _tree_scope(root, path, root_fd, fd)
            boundary = None if scope is None else _tree_boundary(cursor, scope)
            keys = heapq.nsmallest(MAX_ENTRIES + 1, _tree_keys(fd, boundary))
            result["truncated"] = len(keys) > MAX_ENTRIES
            last = None
            for key in keys[:MAX_ENTRIES]:
                is_file, _, name = key
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not (stat.S_ISREG(info.st_mode) if is_file else stat.S_ISDIR(info.st_mode)):
                    continue
                item = {"name": name, "path": f"{path}/{name}" if path else name,
                        "type": "file" if is_file else "dir", "size": info.st_size if is_file else 0}
                size = _tree_wire_bytes(item) + 1
                if size > remaining:
                    result["truncated"] = True
                    break
                result["entries"].append(item)
                remaining -= size
                last = key
            if result["truncated"]:
                if last is None:
                    raise WorkspaceError("invalid_cursor", "目录内容已变化，请重新加载此目录。")
                result["nextCursor"] = _tree_cursor(scope or _tree_scope(root, path, root_fd, fd), last)
    except OSError as exc:
        if cursor is not _NO_TREE_CURSOR and exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            raise WorkspaceError("invalid_cursor", "原目录已不存在，请重新加载文件目录。") from None
        raise
    return result


def _read_regular_file(root, path, limit):
    with open_path(root, relative_parts(path)) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("unsupported_file", "仅支持预览普通文件。")
        if info.st_size > limit:
            raise WorkspaceError("too_large", "文件超过预览大小上限。")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise WorkspaceError("too_large", "文件超过预览大小上限。")
        return content, info.st_mode


def read_bytes(root, path, limit):
    content, _ = _read_regular_file(root, path, limit)
    return content


def image_type(content):
    signatures = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                  (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")]
    for signature, mime in signatures:
        if content.startswith(signature):
            return mime
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def file_preview(root, path, if_revision=None):
    content = read_bytes(root, path, MAX_IMAGE)
    revision = hashlib.sha256(content).hexdigest()
    mime = image_type(content)
    if not mime:
        if len(content) > MAX_TEXT:
            raise WorkspaceError("too_large", "文本文件超过 128 KiB 预览上限。")
        try:
            if b"\x00" in content:
                raise UnicodeError()
            text = content.decode("utf-8")
        except UnicodeError:
            raise WorkspaceError("unsupported_file", "暂不支持预览此二进制文件格式。") from None
    if revision == if_revision:
        return {"path": path, "revision": revision, "unchanged": True}
    result = {"path": path, "size": len(content), "revision": revision}
    if mime:
        return {**result, "kind": "image", "mime": mime, "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii")}
    return {**result, "kind": "text", "content": text}


def save(root, path, content, revision):
    parts = relative_parts(path)
    if not parts or not isinstance(content, str) or not isinstance(revision, str):
        raise WorkspaceError("invalid_request", "保存时需要文件路径、文本内容和读取时的版本。")
    if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        raise WorkspaceError("invalid_request", "文件版本无效，请重新读取。")
    try:
        data = content.encode("utf-8")
    except UnicodeError:
        raise WorkspaceError("unsupported_file", "文件内容需要使用 UTF-8 编码。") from None
    if len(data) > MAX_TEXT:
        raise WorkspaceError("too_large", "文本文件超过 128 KiB 编辑上限。")
    if b"\x00" in data:
        raise WorkspaceError("unsupported_file", "仅支持保存 UTF-8 文本文件。")

    with open_path(root, parts[:-1], directory=True) as parent_fd:
        # A directory lock survives atomic replacement of the file's inode.
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        target = os.open(parts[-1], NO_LINK | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(target, "rb") as original:
            info = os.fstat(original.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceError("unsupported_file", "仅支持保存普通文本文件。")
            old = original.read(MAX_TEXT + 1)
            if len(old) > MAX_TEXT:
                raise WorkspaceError("too_large", "文本文件超过 128 KiB 编辑上限。")
            if hashlib.sha256(old).hexdigest() != revision:
                raise WorkspaceError("conflict", "云端文件已经改变，请重新读取后再保存。")
            try:
                old.decode("utf-8")
            except UnicodeError:
                raise WorkspaceError("unsupported_file", "仅支持保存 UTF-8 文本文件。") from None
            if b"\x00" in old or image_type(old):
                raise WorkspaceError("unsupported_file", "仅支持保存 UTF-8 文本文件。")
        temporary = TEMP_PREFIX + secrets.token_hex(16)
        tmp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(tmp_fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), info.st_mode & 0o777)
                os.fsync(stream.fileno())
            current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_ino, current.st_ctime_ns) != (info.st_ino, info.st_ctime_ns):
                raise WorkspaceError("conflict", "云端文件已经改变，请重新读取后再保存。")
            os.replace(temporary, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    return {"path": path, "size": len(data), "kind": "text", "revision": hashlib.sha256(data).hexdigest()}


def system_git():
    configured = os.environ.get("CLOUD_WORKSPACE_GIT")
    if configured is not None:
        return shutil.which(configured) if os.path.isabs(configured) else None
    return shutil.which("git", path="/usr/local/bin" + os.pathsep + os.defpath)


def git_command(root, *arguments, git_dir=None, overrides=()):
    env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
           "GIT_DIR": str(git_dir or root / ".git"), "GIT_WORK_TREE": str(root)}
    binary = system_git()
    if binary is None:
        if "CLOUD_WORKSPACE_GIT" in os.environ:
            raise WorkspaceError("git_error", "请检查 CLOUD_WORKSPACE_GIT 配置的 Git 绝对路径及执行权限。")
        raise WorkspaceError("git_error", "未找到系统 Git，请在云服务器安装 Git。")
    command = [binary, "--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.preloadIndex=false", "-c", "core.quotePath=false", *overrides, *arguments]
    return command, env


def git(root, *arguments, git_dir=None, limit=MAX_DIFF, check=True, overrides=()):
    # Bound even a diff of a huge tracked file without buffering it in the worker.
    def limit_output():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))

    command, env = git_command(root, *arguments, git_dir=git_dir, overrides=overrides)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=errors, timeout=12, preexec_fn=limit_output)
        except FileNotFoundError:
            raise WorkspaceError("git_error", "未找到系统 Git，请在云服务器安装 Git。") from None
        if result.returncode:
            errors.seek(0)
            lines = errors.read(4096).lower().splitlines()
            unsupported = (b"fatal: unknown repository extension", b"fatal: expected git repo version")
            if any(line.startswith(unsupported) for line in lines):
                raise WorkspaceError("git_error", "当前系统 Git 不支持此仓库格式，请升级云服务器上的 Git。")
        output.seek(0)
        data = output.read(limit + 1)
    truncated = len(data) > limit
    if check and result.returncode and not truncated:
        raise WorkspaceError("git_error", "Git 无法完成工作区仓库操作，请检查仓库或系统 Git 版本。")
    return data[:limit], truncated, result.returncode


def _git_path_record(path, prefix=b""):
    # Git's worktree pointer and backlink each contain a single filesystem path.
    # O_NONBLOCK also prevents a concurrently replaced marker from opening a FIFO.
    fd = os.open(path, NO_LINK | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError()
        data = os.read(fd, 8193)
    finally:
        os.close(fd)
    target = data.removesuffix(b"\n").removesuffix(b"\r")
    if len(data) > 8192 or not target.startswith(prefix) or len(target) == len(prefix):
        raise ValueError()
    return (path.parent / os.fsdecode(target[len(prefix):])).resolve(strict=True)


def _linked_git_dir(root, marker):
    try:
        admin = _git_path_record(marker, b"gitdir: ")
        # Registered worktrees have a private directory in the common repository's
        # worktrees/ and a backlink to their own .git marker.
        if admin.parent.name != "worktrees":
            raise WorkspaceError("git_error", "当前仅支持普通 Git 仓库和 Git 登记的关联工作树。")
        if _git_path_record(admin / "gitdir") != root.resolve() / ".git":
            raise ValueError()
        common, cut, _ = git(root, "rev-parse", "--git-common-dir", git_dir=admin, limit=8192)
        if cut or (root / os.fsdecode(common.removesuffix(b"\n"))).resolve(strict=True) != admin.parent.parent:
            raise ValueError()
        return admin
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError("git_error", "Git 工作树登记无效，请检查项目的 .git 或用 git worktree repair 修复。") from None


def repository_dir(root):
    """Return the selected root's Git directory, including registered linked worktrees."""
    return _repository_head(root)[0]


def _repository_head(root):
    """Resolve the selected repository and snapshot its HEAD, allowing an unborn branch."""
    marker = root / ".git"
    try:
        mode = marker.lstat().st_mode
    except FileNotFoundError:
        return None, None
    if stat.S_ISDIR(mode):
        git_dir = marker
    elif stat.S_ISREG(mode):
        git_dir = _linked_git_dir(root, marker)
    else:
        raise WorkspaceError("git_error", "无法识别此项目的 .git 登记信息。")

    raw, _, code = git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", git_dir=git_dir, check=False)
    if code:
        # An unborn branch has no exact ref yet. A ref pointing at a lost object
        # is different: Git diff --check can succeed even with that broken HEAD.
        ref, _, _ = git(root, "symbolic-ref", "--quiet", "HEAD", git_dir=git_dir)
        _, _, exists = git(root, "show-ref", "--verify", "--quiet", os.fsdecode(ref.removesuffix(b"\n")),
                           git_dir=git_dir, check=False)
        if exists != 1:
            raise WorkspaceError("git_error", "Git 无法读取当前提交，请检查仓库的引用和对象。")
        return git_dir, None
    return git_dir, raw.rstrip(b"\n").decode("ascii")


def status(root, *, git_only=False):
    branch = ""
    changes = {}
    state, message = "none", "当前项目根目录没有 Git 仓库。"
    try:
        git_dir = repository_dir(root)
        if git_dir is not None:
            raw, _, code = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", git_dir=git_dir, check=False)
            if code:
                raw, _, _ = git(root, "rev-parse", "--short", "HEAD", git_dir=git_dir)
            branch = raw.decode("utf-8", "replace").removesuffix("\n")
            if git_only:
                records, clipped, _ = git(root, "status", "--porcelain=v1", "-z", "--no-renames",
                                           "--untracked-files=normal", "--ignore-submodules=all", "--", *VISIBLE_PATHS,
                                           git_dir=git_dir, overrides=disabled_filters(root, git_dir=git_dir))
                # A truncated final record can end partway through a path.
                items = records.split(b"\x00")[:-1]
                changes = {"entries": [{"status": item[:2].decode("ascii"), "path": os.fsdecode(item[3:])}
                                       for item in items[:MAX_ENTRIES]], "truncated": clipped or len(items) > MAX_ENTRIES}
            state, message = "ready", ""
    except WorkspaceError as exc:
        state, message = "error", str(exc)
    except (OSError, subprocess.TimeoutExpired):
        state, message = "error", "无法读取工作区 Git 信息，请检查服务器的 Git 和文件权限。"
    result = {"project": root.name, "branch": branch, "gitState": state, "gitMessage": message}
    if git_only:
        return {**result, **changes}
    if __package__:
        from . import tasks
    else:
        import tasks
    providers = tasks.capabilities(root)
    if state != "ready":
        providers["check"] = {"available": False, "detail": message}
    return {**result, "providers": providers, "diffScopes": list(_DIFF_SCOPES)}


def disabled_filters(root, *, git_dir=None):
    # A repository's clean/process filter is an external command even when
    # --no-ext-diff is used. Ask Git for its names and override each before diffing.
    raw, truncated, code = git(root, "config", "--null", "--name-only", "--get-regexp",
                               r"^filter\..*\.(clean|process|required)$", git_dir=git_dir, check=False)
    if truncated or code not in (0, 1):
        raise WorkspaceError("git_error", "无法检查仓库的 Git 过滤器设置。")
    overrides = []
    for key in raw.rstrip(b"\x00").split(b"\x00"):
        if key:
            name = os.fsdecode(key)
            overrides.extend(["-c", name + ("=false" if name.endswith(".required") else "=")])
    return overrides


_GIT_PATH_ESCAPES = {
    **{code: f"\\{code:03o}" for code in (*range(32), 127)},
    # os.fsdecode preserves non-UTF-8 filename bytes with surrogateescape.
    **{0xDC00 + byte: f"\\{byte:03o}" for byte in range(128, 256)},
    **str.maketrans({'"': '\\"', "\\": "\\\\", "\a": "\\a", "\b": "\\b", "\t": "\\t",
                     "\n": "\\n", "\v": "\\v", "\f": "\\f", "\r": "\\r"}),
}


def _git_path(path):
    escaped = path.translate(_GIT_PATH_ESCAPES)
    return f'"{escaped}"' if escaped != path else path


def new_file_patch(root, path):
    try:
        content, mode = _read_regular_file(root, path, MAX_TEXT)
        if b"\x00" in content:
            raise UnicodeError()
        text = content.decode("utf-8")
    except (WorkspaceError, UnicodeError, OSError):
        return f"Untracked file: {_git_path(path)} (preview unavailable)\n"
    source, target = _git_path("a/" + path), _git_path("b/" + path)
    git_mode = "100755" if mode & stat.S_IXUSR else "100644"
    header = f"diff --git {source} {target}\nnew file mode {git_mode}\n"
    if not text:
        return header
    # A Git patch counts LF bytes. CR, VT and Unicode separators belong to the content.
    terminated = text.endswith("\n")
    count = text.count("\n") + (not terminated)
    extent = "1" if count == 1 else f"1,{count}"
    added = text.removesuffix("\n").replace("\n", "\n+")
    ending = "\n" if terminated else "\n\\ No newline at end of file\n"
    delimiter = "\t" if " " in path else ""
    return header + f"--- /dev/null\n+++ {target}{delimiter}\n@@ -0,0 +{extent} @@\n+{added}{ending}"


def _has_non_utf8_diff_path(raw, truncated):
    for line in io.BytesIO(raw):
        if line.startswith((b"diff --git ", b"diff --cc ", b"diff --combined ", b"* Unmerged path ")):
            try:
                codecs.utf_8_decode(line, "strict", not truncated or line.endswith(b"\n"))
            except UnicodeDecodeError:
                return True
    return False


def diff(root, scope=_NO_DIFF_SCOPE):
    legacy = scope is _NO_DIFF_SCOPE
    if not legacy and scope not in _DIFF_SCOPES:
        raise WorkspaceError("invalid_request", "Git 差异范围应为 staged 或 unstaged。")
    result = {"diff": "", "truncated": False}
    if not legacy:
        result["scope"] = scope
    git_dir = repository_dir(root)
    if git_dir is None:
        return {**result, "gitState": "none"}

    comparison = []
    if legacy:
        base, _, code = git(root, "rev-parse", "--verify", "HEAD", git_dir=git_dir, check=False)
        if code:
            git(root, "symbolic-ref", "--quiet", "HEAD", git_dir=git_dir)
            base = _empty_tree(root, git_dir)
        comparison = [base.decode("ascii").strip()]
    elif scope == "staged":
        # With no explicit base, --cached also handles a branch with no commits.
        comparison = ["--cached"]
    return {**result, **_read_diff(root, git_dir, comparison, include_untracked=scope != "staged", legacy=legacy)}


def commit_diff(root):
    result = {"scope": "last_commit", "diff": "", "truncated": False}
    git_dir, head = _repository_head(root)
    if git_dir is None:
        return {**result, "gitState": "none"}
    if head is None:
        return {**result, "gitState": "unborn"}

    # Shallow boundaries suppress parent IDs in rev-list and HEAD^, even when the
    # underlying commit names a parent. The tree and first parent fit in 160 bytes
    # for both SHA-1 and SHA-256; never buffer a long commit message or signature.
    header, _, _ = git(root, "cat-file", "commit", head, git_dir=git_dir, limit=160)
    lines = header.split(b"\n", 2)
    oid = rb"[0-9a-f]{%d}" % len(head)
    if len(lines) < 2 or not re.fullmatch(b"tree " + oid, lines[0]):
        raise WorkspaceError("git_error", "Git 无法读取当前提交的树。")
    parent = None
    if lines[1].startswith(b"parent "):
        raw_parent = lines[1][7:]
        if not re.fullmatch(oid, raw_parent):
            raise WorkspaceError("git_error", "Git 无法识别当前提交的第一父提交。")
        parent = raw_parent.decode("ascii")
    result.update(commit=head, parent=parent)
    if parent is None:
        base = _empty_tree(root, git_dir).decode("ascii").strip()
    else:
        kind, _, code = git(root, "cat-file", "-t", parent, git_dir=git_dir, check=False)
        if code:
            return {**result, "gitState": "parent_unavailable"}
        if kind != b"commit\n":
            raise WorkspaceError("git_error", "Git 的第一父对象不是提交，请检查仓库。")
        base = parent
    return {**result, **_read_diff(root, git_dir, [base, head])}


def _empty_tree(root, git_dir):
    # hash-object without -w computes the repository-format ID and does not write a Git object.
    value, _, _ = git(root, "hash-object", "-t", "tree", "--stdin", git_dir=git_dir)
    return value


def _read_diff(root, git_dir, comparison, *, include_untracked=False, legacy=False):
    arguments = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                 "--ignore-submodules=all", *comparison, "--", *VISIBLE_PATHS)
    overrides = disabled_filters(root, git_dir=git_dir)
    raw, truncated, _ = git(root, *arguments, git_dir=git_dir, overrides=overrides)
    if not legacy and _has_non_utf8_diff_path(raw, truncated):
        # Git can quote original path bytes without changing the patch body.
        raw, truncated, _ = git(root, *arguments, git_dir=git_dir, overrides=[*overrides, "-c", "core.quotePath=true"])
    content = bytearray(raw)
    if not truncated and include_untracked:
        paths, names_cut, _ = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *VISIBLE_PATHS,
                                  git_dir=git_dir)
        for name in paths.split(b"\x00")[:-1]:
            remaining = MAX_DIFF - len(content)
            if not remaining:
                truncated = True
                break
            patch = new_file_patch(root, os.fsdecode(name)).encode("utf-8", "replace")
            content.extend(patch[:remaining])
            if len(patch) > remaining:
                truncated = True
                break
        truncated = truncated or names_cut

    if legacy:
        text = content.decode("utf-8", "replace")
    else:
        # A byte limit can leave a valid character incomplete. Only replace
        # genuinely invalid bytes, which may expand when encoded for transport.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(content, final=not truncated)
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_DIFF:
            text = encoded[:MAX_DIFF].decode("utf-8", "ignore")
            truncated = True
    return {"diff": text, "truncated": truncated}


SEED = {
    "README.md": """# 云端工作区

这是一个可以直接运行的 Python 小项目。代码和 Git 仓库保存在这台 Linux 云主机上。

运行 `python3 count_lines.py README.md` 可以统计此文档的行数。
你可以让编程助手解释这个工具或提出改进建议，也可以手动编辑文件并查看 Git 差异。
""",
    "count_lines.py": '''"""Print the line count for each UTF-8 text file passed on the command line."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    for path in parser.parse_args().files:
        with path.open(encoding="utf-8") as stream:
            print(f"{sum(1 for _ in stream):>6} {path}")


if __name__ == "__main__":
    main()
''',
    ".gitignore": ".cloudwork/\n__pycache__/\n*.pyc\n",
}


def initialize(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    with open_path(root.parent, [], directory=True) as parent_fd:
        # Two init requests arriving together must see one fully seeded project.
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        if os.path.lexists(root):
            with open_path(root, [], directory=True) as fd:
                if os.listdir(fd):
                    return {"project": root.name, "created": False}
        with tempfile.TemporaryDirectory(dir=root.parent, prefix=".workspace-init-") as temporary:
            staged = Path(temporary) / "project"
            staged.mkdir(mode=0o700)
            for name, content in SEED.items():
                (staged / name).write_text(content, encoding="utf-8")
            git(staged, "init", "--initial-branch=main", "--template=")
            git(staged, "add", "--", *SEED)
            git(staged, "-c", "user.name=Cloud Workspace", "-c", "user.email=workspace@localhost",
                "-c", "commit.gpgSign=false", "-c", "gc.auto=0", "commit", "-m", "Initialize workspace")
            # POSIX rename cannot overwrite a non-empty project directory.
            staged.rename(root)
    return {"project": root.name, "created": True}


def dispatch(request, root):
    if not isinstance(request, dict):
        raise WorkspaceError("invalid_request", "请求必须是 JSON 对象。")
    op = request.get("op")
    if op == "init":
        return initialize(root)
    with open_path(root, [], directory=True):
        pass
    if op == "status":
        return status(root, git_only=request.get("scope") == "git")
    if op == "tree":
        return tree(root, request.get("path", ""), request.get("cursor", _NO_TREE_CURSOR))
    if op == "file":
        return file_preview(root, request.get("path"), request.get("ifRevision"))
    if op == "save":
        return save(root, request.get("path"), request.get("content"), request.get("revision"))
    if op == "diff":
        return diff(root, request.get("scope", _NO_DIFF_SCOPE))
    if op == "commit_diff":
        if set(request) != {"op"}:
            raise WorkspaceError("invalid_request", "最近一次提交的差异不接受额外参数。")
        return commit_diff(root)
    import tasks
    if op == "tasks":
        if "beforeTaskId" in request:
            return tasks.task_page(root, request["beforeTaskId"])
        return tasks.task_page(root)
    if op == "start":
        return tasks.start_task(root, request.get("requestId"), request.get("provider"),
                                request.get("prompt"), request.get("parentTaskId"),
                                request.get("path"), request.get("revision"))
    if op == "events":
        if "tail" in request or "before" in request:
            if "after" in request or ("tail" in request and "before" in request):
                raise WorkspaceError("invalid_request", "tail、before 和 after 只能使用一个。")
            if "before" in request:
                return tasks.event_window(root, request.get("taskId"), request["before"])
            if request["tail"] is not True:
                raise WorkspaceError("invalid_request", "获取最新输出时 tail 应为 true。")
            return tasks.event_window(root, request.get("taskId"))
        return tasks.events(root, request.get("taskId"), request.get("after", 0))
    if op == "cancel":
        return tasks.cancel_task(root, request.get("taskId"))
    if op == "proposal":
        return tasks.get_proposal(root, request.get("taskId"))
    if op == "decideProposal":
        return tasks.decide_proposal(root, request.get("taskId"), request.get("decision"))
    raise WorkspaceError("invalid_request", "未知的工作区操作。")


def envelope(payload, root, stdin=False):
    try:
        try:
            if stdin:
                if len(payload) > MAX_REQUEST:
                    raise ValueError()
                body = payload
            else:
                if not isinstance(payload, str) or len(payload) > (MAX_REQUEST * 4 // 3 + 4):
                    raise ValueError()
                body = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            request = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, binascii.Error, RecursionError):
            raise WorkspaceError("invalid_request", "工作区请求格式无效，或超过大小限制。") from None
        return {"ok": True, "data": dispatch(request, root)}
    except WorkspaceError as exc:
        code, message = exc.code, str(exc)
    except FileNotFoundError:
        code, message = "not_found", "工作区或文件不存在。"
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENAMETOOLONG):
            code, message = "invalid_path", "此路径在工作区中不可访问。"
        else:
            code, message = "io_error", "工作区读写失败。"
    except subprocess.TimeoutExpired:
        code, message = "timeout", "Git 操作超时。"
    except ValueError as exc:
        code, message = getattr(exc, "code", "invalid_request"), str(exc)
    except Exception as exc:
        code = getattr(exc, "code", "internal_error")
        message = str(exc) if code != "internal_error" else "云端工作区服务执行失败。"
    return {"ok": False, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    project = Path(os.environ.get("CLOUD_WORKSPACE_ROOT", ROOT)).absolute()
    arg = sys.argv[1] if len(sys.argv) == 2 else None
    stdin = arg == "--stdin"
    payload = sys.stdin.buffer.read(MAX_REQUEST + 1) if stdin else arg
    result = envelope(payload, project, stdin=stdin)
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace")
    if len(output) >= 1024 * 1024:
        output = b'{"ok":false,"error":{"code":"too_large","message":"Response exceeds the transport limit."}}'
    sys.stdout.buffer.write(output + b"\n")
