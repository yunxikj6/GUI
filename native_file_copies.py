"""Private, editable Linux working copies of individual cloud workspace text files."""

import ctypes
import fcntl
import hashlib
import os
from contextlib import closing, contextmanager, suppress
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat

from cloud.workspace import MAX_TEXT, WorkspaceError, relative_parts
from instabox import BridgeError


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _text(data):
    if len(data) > MAX_TEXT:
        raise BridgeError("too_large", "文本文件超过 128 KiB 上限。")
    try:
        if b"\0" in data:
            raise UnicodeError()
        return data.decode("utf-8")
    except UnicodeError:
        raise BridgeError("unsupported_file", "工作副本仅支持 UTF-8 文本。") from None


def _binding(machine, project, path):
    if not isinstance(machine, str) or not re.fullmatch(r"m_[A-Za-z0-9_]+", machine):
        raise BridgeError("invalid_machine", "请输入有效的 InstaBox 机器标识。")
    if not isinstance(project, str) or "\0" in project:
        raise BridgeError("invalid_project", "请输入云项目的绝对路径。")
    directory = PurePosixPath(project)
    if directory.anchor != "/" or directory == directory.parent or ".." in directory.parts:
        raise BridgeError("invalid_project", "请输入云项目的绝对路径。")
    try:
        if not relative_parts(path):
            raise WorkspaceError("invalid_path", "请选择云项目中的文件。")
        # The local registry and GTK file manager both require Unicode paths.
        path.encode("utf-8")
        project.encode("utf-8")
    except WorkspaceError as exc:
        raise BridgeError(exc.code, str(exc)) from None
    except UnicodeError:
        raise BridgeError("invalid_path", "工作副本路径需要使用 UTF-8 编码。") from None
    return machine, str(directory), path


def _is_private_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("unsafe_local", "工作副本目录必须由当前用户所有且仅该用户可访问。")


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _local_errors():
    try:
        yield
    except FileNotFoundError:
        raise BridgeError("local_missing", "本地工作副本或目录已移动或删除。") from None
    except (OSError, sqlite3.Error):
        raise BridgeError("local_io", "无法安全访问工作副本。请检查权限、链接和磁盘空间。") from None


class Copies:
    def __init__(self, base, rpc):
        self.base = Path(base).absolute()
        self.rpc = rpc
        with _local_errors():
            if self.base.resolve() != self.base:
                raise BridgeError("unsafe_local", "工作副本根目录不能包含符号链接。")
            self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.base.lstat()
            self._base_identity = info.st_dev, info.st_ino
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS copies(
                        local TEXT PRIMARY KEY, machine TEXT NOT NULL, project TEXT NOT NULL,
                        path TEXT NOT NULL, revision TEXT NOT NULL, pending TEXT, initial BLOB);
                    CREATE INDEX IF NOT EXISTS cloud_file ON copies(machine,project,path,revision);
                """)

    @contextmanager
    def _directory(self, parts, create=False):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        fd = os.open(self.base, flags)
        try:
            root = os.fstat(fd)
            if (root.st_dev, root.st_ino) != self._base_identity:
                raise BridgeError("unsafe_local", "工作副本根目录已被替换，请重新检查所选目录。")
            _is_private_directory(fd)
            for part in parts:
                if part in ("", ".", "..") or "/" in part:
                    raise BridgeError("unsafe_local", "工作副本目录路径无效。")
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                _is_private_directory(fd)
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _db(self):
        with self._directory([".registry"], create=True) as directory:
            name = "copies.sqlite3"
            fd = os.open(name, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            os.close(fd)
            for suffix in ("", "-journal", "-wal", "-shm"):
                with suppress(FileNotFoundError):
                    info = os.stat(name + suffix, dir_fd=directory, follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                            info.st_uid != os.getuid() or info.st_mode & 0o077):
                        raise BridgeError("unsafe_local", "本地工作副本登记库的权限或类型无效。")
            with closing(sqlite3.connect(f"/proc/self/fd/{directory}/{name}", timeout=30)) as db, db:
                db.row_factory = sqlite3.Row
                yield db

    @contextmanager
    def _lock(self, binding):
        name = digest("\0".join(binding).encode())
        with self._directory([".registry", "locks", name], create=True) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield

    def _get(self, local):
        with self._db() as db:
            row = db.execute("SELECT * FROM copies WHERE local=?", (local,)).fetchone()
        if row is None:
            raise BridgeError("unregistered_file", "此文件尚未登记为云项目工作副本。")
        return row

    def _row_binding(self, row):
        binding = _binding(row["machine"], row["project"], row["path"])
        folder = self._project_folder(binding)
        parts = Path(row["local"]).parts
        cloud_parts = Path(binding[2]).parts
        primary = parts == ("projects", folder, *cloud_parts)
        fork = (len(parts) >= 4 and parts[:2] == ("revisions", folder)
                and re.fullmatch(r"[0-9a-f]{24}", parts[2]) and parts[3:] == cloud_parts)
        if not (primary or fork):
            raise BridgeError("unsafe_local", "此工作副本的登记路径与云项目不一致。")
        return binding

    def _registered(self, path):
        supplied = Path(path).absolute()
        if ".." in supplied.parts or not supplied.is_relative_to(self.base):
            raise BridgeError("unregistered_file", "请选择已登记的本地工作副本。")
        row = self._get(str(supplied.relative_to(self.base)))
        return row["local"], self._row_binding(row)

    @contextmanager
    def _open_copy(self, local):
        path = Path(local)
        with self._directory(path.parts[:-1]) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as file:
                before = os.fstat(file.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
                    raise BridgeError("unsafe_local", "工作副本必须是当前用户独立拥有的普通文件。")
                yield file, directory, before

    def _snapshot(self, local):
        with self._open_copy(local) as (file, directory, before):
            data = file.read(MAX_TEXT + 1)
            current = os.stat(Path(local).name, dir_fd=directory, follow_symlinks=False)
            if _stamp(before) != _stamp(os.fstat(file.fileno())) or _stamp(before) != _stamp(current):
                raise BridgeError("local_changed", "编辑器正在保存文件，请在保存完成后重试。")
            _text(data)
            return data

    def _install_new(self, local, data):
        """Atomically expose a complete Linux file only at a vacant destination."""
        with self._directory(local.parts[:-1], create=True) as directory:
            temporary = ".cloudwork-save-" + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                # RENAME_NOREPLACE also protects a file created by an editor
                # after the existence check and before this installation.
                rename = ctypes.CDLL(None, use_errno=True).renameat2
                if rename(directory, os.fsencode(temporary), directory, os.fsencode(local.name), 1) != 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)

    def _materialize(self, row):
        """Finish an interrupted checkout without replacing any existing file."""
        local = Path(row["local"])
        data = row["initial"]
        if data is not None:
            try:
                self._install_new(local, data)
            except FileExistsError:
                if self._snapshot(str(local)) != data:
                    raise BridgeError("local_conflict", "本机已有同名文件，工作副本未覆盖。") from None
            with self._db() as db:
                db.execute("UPDATE copies SET initial=NULL WHERE local=?", (str(local),))
        return self.base / local

    def _create(self, binding, local, data):
        target = Path(local)
        with self._directory(target.parts[:-1], create=True) as fd:
            try:
                os.stat(target.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise BridgeError("local_conflict", "本机已有未登记的同名文件。请移动该文件后重试。")
        with self._db() as db:
            db.execute("INSERT INTO copies(local,machine,project,path,revision,initial) VALUES(?,?,?,?,?,?)",
                       (local, *binding, digest(data), data))
        return self._materialize(self._get(local))

    @staticmethod
    def _project_folder(binding):
        machine, project, _ = binding
        label = Path(project).name[:30]
        return f"{label}-{digest((machine + chr(0) + project).encode())[:20]}"

    def _remote(self, binding):
        machine, project, path = binding
        reply = self.rpc(machine, project, {"op": "file", "path": path})
        if not isinstance(reply, dict) or reply.get("path") != path:
            raise BridgeError("invalid_response", "云端未返回所选文件。")
        if reply.get("kind") == "image":
            raise BridgeError("unsupported_file", "工作副本仅支持 UTF-8 文本。")
        try:
            if reply.get("kind") != "text" or not isinstance(reply.get("content"), str):
                raise ValueError()
            data = reply["content"].encode()
            _text(data)
            if reply.get("size") != len(data) or reply.get("revision") != digest(data):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_response", "云端返回的文本内容与其版本不一致。") from None

    def _restore_missing(self, local, binding):
        data = self._remote(binding)
        try:
            self._install_new(Path(local), data)
        except FileExistsError:
            raise BridgeError("local_changed", "此路径重新出现了本地文件，已保留其内容。请重新打开。") from None
        self._confirm(local, digest(data))

    def checkout(self, machine, project, path):
        binding = _binding(machine, project, path)
        local = str(Path("projects", self._project_folder(binding), path))
        with _local_errors(), self._lock(binding):
            with self._db() as db:
                row = db.execute("SELECT * FROM copies WHERE local=?", (local,)).fetchone()
            if row is None:
                return self._create(binding, local, self._remote(binding))
            if self._row_binding(row) != binding:
                raise BridgeError("unsafe_local", "此工作副本的云项目登记不一致。")
            self._materialize(row)
            try:
                # Native editors may exceed the cloud text limit while drafting.
                # Reopening that draft offline must allow the user to correct it.
                with self._open_copy(local):
                    pass
            except FileNotFoundError:
                self._restore_missing(local, binding)
            return self.base / local

    def _state(self, local, status=None):
        row = self._get(local)
        local_rev = digest(self._snapshot(local))
        dirty = local_rev != row["revision"]
        return {**{key: row[key] for key in ("machine", "project", "path", "revision")},
                "local": str(self.base / local), "localRevision": local_rev, "dirty": dirty,
                "status": status or ("modified" if dirty else "clean")}

    def inspect(self, path):
        with _local_errors():
            local, binding = self._registered(path)
            with self._lock(binding):
                self._materialize(self._get(local))
                return self._state(local)

    def _confirm(self, local, revision):
        with self._db() as db:
            db.execute("UPDATE copies SET revision=?,pending=NULL WHERE local=?", (revision, local))

    def publish(self, path):
        with _local_errors():
            local, binding = self._registered(path)
            with self._lock(binding):
                row = self._get(local)
                self._materialize(row)
                content = self._snapshot(local)
                target = digest(content)
                base = row["revision"]
                # Compare the actual cloud file, including an earlier save with
                # an unknown outcome. A later save still requires server-side CAS.
                remote = digest(self._remote(binding))
                if remote == target:
                    self._confirm(local, remote)
                    status = "unchanged" if base == target and not row["pending"] else "already_published"
                    return self._state(local, status)
                if remote == row["pending"]:
                    self._confirm(local, remote)
                    base = remote
                if remote != base:
                    raise BridgeError("conflict", "云端已变化，请刷新取得新副本，并保留本地稿进行合并。")
                with self._db() as db:
                    db.execute("UPDATE copies SET pending=? WHERE local=?", (target, local))
                response = self.rpc(*binding[:2], {"op": "save", "path": binding[2],
                                    "revision": base, "content": _text(content)})
                if (not isinstance(response, dict) or response.get("path") != binding[2] or
                        response.get("kind") != "text" or response.get("size") != len(content) or
                        response.get("revision") != target):
                    raise BridgeError("invalid_response", "云端保存的回执无法确认，请重试发布以核验结果。")
                self._confirm(local, target)
                return self._state(local, "published")

    def refresh(self, path):
        with _local_errors():
            local, binding = self._registered(path)
            with self._lock(binding):
                row = self._get(local)
                self._materialize(row)
                try:
                    self._snapshot(local)
                except FileNotFoundError:
                    self._restore_missing(local, binding)
                    return self._state(local, "restored")
                data = self._remote(binding)
                remote = digest(data)
                state = self._state(local)
                if remote in (state["localRevision"], row["pending"]):
                    self._confirm(local, remote)
                    state = self._state(local)
                if remote == state["revision"]:
                    state["status"] = "local_changes" if state["dirty"] else "current"
                    return state
                with self._db() as db:
                    existing = db.execute("""SELECT * FROM copies WHERE machine=? AND project=?
                                          AND path=? AND revision=? ORDER BY local LIMIT 1""",
                                          (*binding, remote)).fetchone()
                if existing:
                    self._row_binding(existing)
                    copy = self._materialize(existing)
                    try:
                        fork_state = self._state(existing["local"])
                    except FileNotFoundError:
                        # The registered fork is also recoverable after deletion.
                        with suppress(FileExistsError):
                            self._install_new(Path(existing["local"]), data)
                        fork_state = self._state(existing["local"])
                else:
                    fork = Path("revisions", self._project_folder(binding), secrets.token_hex(12), binding[2])
                    copy = self._create(binding, str(fork), data)
                    fork_state = self._state(str(fork))
                state = self._state(local, "remote_copy")
                return {**state, "remoteCopy": str(copy), "remoteRevision": remote,
                        "remoteCopyDirty": fork_state["dirty"], "remoteCopyLocalRevision": fork_state["localRevision"]}
