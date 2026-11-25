"""One-request, JSON-only Linux workspace API for the authenticated local bridge."""

import base64
import binascii
from contextlib import contextmanager
import difflib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import secrets
import stat
import subprocess
import sys
import tempfile


ROOT = Path("/data/cloud-workspace/project")
PRIVATE = {".git", ".cloudwork"}
MAX_ENTRIES = 400
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
    fd = os.open(root, DIRECTORY)
    try:
        for index, name in enumerate(parts):
            flags = DIRECTORY if directory or index < len(parts) - 1 else NO_LINK | os.O_NONBLOCK
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def tree(root, path):
    entries = []
    with open_path(root, relative_parts(path), directory=True) as fd, os.scandir(fd) as listing:
        for entry in listing:
            if entry.name in PRIVATE or entry.name.startswith(TEMP_PREFIX):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                kind = "dir"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                continue
            entries.append({
                "name": entry.name, "path": f"{path}/{entry.name}" if path else entry.name,
                "type": kind, "size": info.st_size if kind == "file" else 0,
            })
            if len(entries) > MAX_ENTRIES:
                break
    truncated = len(entries) > MAX_ENTRIES
    entries = sorted(entries[:MAX_ENTRIES], key=lambda item: (item["type"] != "dir", item["name"].casefold()))
    return {"path": path, "entries": entries, "truncated": truncated}


def read_bytes(root, path, limit):
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


def git(root, *arguments, limit=MAX_DIFF, check=True, overrides=()):
    # A file size limit bounds even a diff of a huge tracked file. A file descriptor
    # avoids buffering the child process's complete output in this service.
    def limit_output():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))

    env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
           "GIT_DIR": str(root / ".git"), "GIT_WORK_TREE": str(root)}
    with tempfile.TemporaryFile() as output:
        command = ["git", "--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                   "-c", "core.preloadIndex=false", "-c", "core.quotePath=false", *overrides, *arguments]
        result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=12, preexec_fn=limit_output)
        output.seek(0)
        data = output.read(limit + 1)
    truncated = len(data) > limit
    if check and result.returncode and not truncated:
        raise WorkspaceError("git_error", "Git 无法完成工作区仓库操作。")
    return data[:limit], truncated, result.returncode


def has_repo(root):
    try:
        return stat.S_ISDIR((root / ".git").lstat().st_mode)
    except FileNotFoundError:
        return False


def status(root):
    import tasks

    branch = ""
    if has_repo(root):
        raw, _, code = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if code:
            raw, _, _ = git(root, "rev-parse", "--short", "HEAD", check=False)
        branch = raw.decode("utf-8", "replace").strip()
    return {"project": root.name, "branch": branch, "providers": tasks.capabilities(root)}


def disabled_filters(root):
    # A repository's clean/process filter is an external command even when
    # --no-ext-diff is used. Ask Git for its names and override each before diffing.
    raw, truncated, code = git(root, "config", "--null", "--name-only", "--get-regexp",
                               r"^filter\..*\.(clean|process|required)$", check=False)
    if truncated or code not in (0, 1):
        raise WorkspaceError("git_error", "无法检查仓库的 Git 过滤器设置。")
    overrides = []
    for key in raw.rstrip(b"\x00").split(b"\x00"):
        if key:
            name = os.fsdecode(key)
            overrides.extend(["-c", name + ("=false" if name.endswith(".required") else "=")])
    return overrides


def new_file_patch(root, path):
    try:
        content = read_bytes(root, path, MAX_TEXT)
        if b"\x00" in content:
            raise UnicodeError()
        lines = content.decode("utf-8").splitlines(keepends=True)
    except (WorkspaceError, UnicodeError, OSError):
        return f"Untracked file: {path} (preview unavailable)\n"
    patch = difflib.unified_diff([], lines, "/dev/null", f"b/{path}")
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n" + "".join(patch).rstrip("\n") + "\n"


def diff(root):
    if not has_repo(root):
        return {"diff": "", "truncated": False}
    base, _, code = git(root, "rev-parse", "--verify", "HEAD", check=False)
    if code:
        base, _, _ = git(root, "hash-object", "-t", "tree", "--stdin")
    raw, truncated, _ = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                            "--ignore-submodules=all", base.decode("ascii").strip(), "--", *VISIBLE_PATHS,
                            overrides=disabled_filters(root))
    if truncated:
        return {"diff": raw.decode("utf-8", "replace"), "truncated": True}
    paths, names_cut, _ = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *VISIBLE_PATHS)
    result = bytearray(raw)
    for name in paths.split(b"\x00")[:-1]:
        path = os.fsdecode(name)
        patch = new_file_patch(root, path).encode("utf-8", "replace")
        remaining = MAX_DIFF - len(result)
        result.extend(patch[:remaining])
        if len(patch) > remaining:
            truncated = True
            break
    return {"diff": result.decode("utf-8", "replace"), "truncated": truncated or names_cut}


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
        return status(root)
    if op == "tree":
        return tree(root, request.get("path", ""))
    if op == "file":
        return file_preview(root, request.get("path"), request.get("ifRevision"))
    if op == "save":
        return save(root, request.get("path"), request.get("content"), request.get("revision"))
    if op == "diff":
        return diff(root)
    import tasks
    if op == "tasks":
        return {"tasks": tasks.list_tasks(root)}
    if op == "start":
        return tasks.start_task(root, request.get("requestId"), request.get("provider"),
                                request.get("prompt"), request.get("parentTaskId"),
                                request.get("path"), request.get("revision"))
    if op == "events":
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
