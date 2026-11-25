"""Detached, replayable tasks for a single-user Linux workspace."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid


_PAGE_BYTES = 256 * 1024
_CHUNK_CHARS = 8192
_TASK_LIMIT = 80
# Old databases can contain individual messages larger than a transport response.
# Reserve a separate, JavaScript-safe cursor range for their virtual chunks. A
# pre-upgrade integer still skips exactly the records that client already read.
_LEGACY_BASE = 1 << 40
_LEGACY_STRIDE = 1 << 20


def capabilities(root: Path) -> dict:
    result = {}
    for provider, binary in (("check", "git"), ("codex", "codex"), ("claude", "claude")):
        available = shutil.which(binary) is not None
        if not available:
            detail = f"请在云服务器安装 {binary} 并确认已加入 PATH。"
        elif provider == "check":
            detail = "用 Git 检查项目，无需模型账户。"
        else:
            detail = f"已发现 {binary}，任务使用云服务器已有的登录配置。"
        result[provider] = {"available": available, "detail": detail}
    return result


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def _database(root):
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    key = sha256(os.fsencode(root.resolve(strict=True))).hexdigest()
    directory = state / "cloudwork" / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(directory / "tasks.sqlite", timeout=20)) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS task (
                id TEXT PRIMARY KEY, requestId TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
                prompt TEXT NOT NULL, status TEXT NOT NULL, createdAt TEXT NOT NULL,
                finishedAt TEXT, pid INTEGER, processKey TEXT
            );
            CREATE TABLE IF NOT EXISTS event (
                taskId TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                text TEXT NOT NULL, PRIMARY KEY (taskId, seq)
            );
            CREATE INDEX IF NOT EXISTS task_created ON task(createdAt);
            CREATE INDEX IF NOT EXISTS task_active ON task(status) WHERE status IN ('queued', 'running');
        """)
        yield db


def _process_key(pid):
    # Linux reuses PIDs. Boot ID and process start ticks identify this specific worker.
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
        if stat[0] in (b"Z", b"X"):
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + stat[19].decode("ascii")
    except (OSError, IndexError, ValueError):
        return None


def _public(row):
    return {key: row[key] for key in (
        "id", "requestId", "provider", "prompt", "status", "createdAt", "finishedAt"
    )}


def _chunks(text):
    start = 0
    while len(text) - start > _CHUNK_CHARS:
        end = start + _CHUNK_CHARS
        newline = text.rfind("\n", start, end)
        if newline >= start:
            end = newline + 1
        elif text[end - 1:end + 1] == "\r\n":
            end -= 1
        yield text[start:end]
        start = end
    yield text[start:]


def _append(db, task_id, kind, text):
    if kind == "output" and len(text) > _CHUNK_CHARS:
        kind = "delta"
    db.executemany("""
        INSERT INTO event (taskId, seq, kind, text)
        SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ? FROM event WHERE taskId = ?
    """, ((task_id, kind, chunk, task_id) for chunk in _chunks(text)))


def _finish(db, task_id, success, message):
    status = "succeeded" if success else "failed"
    _append(db, task_id, "status" if success else "error", message)
    db.execute("UPDATE task SET status=?, finishedAt=? WHERE id=?", (status, _now(), task_id))


def _recover(db):
    for row in db.execute("SELECT id, pid, processKey FROM task WHERE status IN ('queued', 'running')").fetchall():
        if not row["processKey"] or _process_key(row["pid"]) != row["processKey"]:
            _finish(db, row["id"], False, "云端任务进程已退出。已保留输出，可创建新任务重试。")


def start_task(root: Path, request_id: str, provider: str, prompt: str) -> dict:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("requestId 长度应为 1 至 128 个字符。")
    if provider not in ("codex", "claude", "check"):
        raise ValueError("不支持的任务类型。")
    if not isinstance(prompt, str) or len(prompt) > 32000 or (provider != "check" and not prompt.strip()):
        raise ValueError("请输入任务内容，最多 32000 个字符。")

    root = Path(root).resolve(strict=True)
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        existing = db.execute("SELECT * FROM task WHERE requestId=?", (request_id,)).fetchone()
        if existing:
            if existing["provider"] != provider or existing["prompt"] != prompt:
                raise ValueError("此 requestId 已用于其他任务内容。")
            return _public(existing)
        task_id = uuid.uuid4().hex
        db.execute("""
            INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
            VALUES (?, ?, ?, ?, 'queued', ?)
        """, (task_id, request_id, provider, prompt, _now()))
        _append(db, task_id, "status", "任务已排队。")
        try:
            # The child also takes BEGIN IMMEDIATE before reading its record.
            # This prevents a committed queue entry with no associated process.
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(root), task_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=root, close_fds=True, start_new_session=True,
            )
        except OSError:
            _finish(db, task_id, False, "无法启动云端 Python 任务进程。")
        else:
            db.execute("UPDATE task SET pid=?, processKey=? WHERE id=?",
                       (worker.pid, _process_key(worker.pid), task_id))
            threading.Thread(target=worker.wait, daemon=True).start()
        return _public(db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone())


def list_tasks(root: Path) -> list[dict]:
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        return [dict(row) for row in db.execute("""
            SELECT id, requestId, provider, substr(prompt, 1, 160) AS prompt,
                   status, createdAt, finishedAt
            FROM task ORDER BY createdAt DESC, rowid DESC LIMIT ?
        """, (_TASK_LIMIT,))]


def _wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _replay(db, task_id, after):
    packed = after >= _LEGACY_BASE
    start = (after - _LEGACY_BASE) // _LEGACY_STRIDE - 1 if packed else after
    for row in db.execute("SELECT seq, kind, text FROM event WHERE taskId=? AND seq>? ORDER BY seq",
                          (task_id, start)):
        oversized = len(row["text"]) > _CHUNK_CHARS
        packed = packed or oversized
        kind = "delta" if oversized and row["kind"] == "output" else row["kind"]
        for index, chunk in enumerate(_chunks(row["text"])):
            seq = _LEGACY_BASE + row["seq"] * _LEGACY_STRIDE + index if packed else row["seq"]
            if seq > after:
                yield {"seq": seq, "kind": kind, "text": chunk}


def events(root: Path, task_id: str, after: int) -> dict:
    if type(after) is not int or not 0 <= after < (1 << 53):
        raise ValueError("事件游标应为非负整数。")
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        row = db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("找不到该任务。")
        result = {"task": _public(row), "events": [], "cursor": after, "hasMore": False}
        remaining = _PAGE_BYTES - _wire_size(result) - 32  # Allow for digits in the next cursor.
        for item in _replay(db, task_id, after):
            size = _wire_size(item) + 1
            if size > remaining:
                result["hasMore"] = True
                break
            result["events"].append(item)
            result["cursor"] = item["seq"]
            remaining -= size
        return result


def _git_check(root, emit):
    emit("正在统计 Git 跟踪的项目文件。")
    git = ["git", "--no-optional-locks"]
    listed = subprocess.run(git + ["ls-files", "--cached", "-z"], cwd=root, capture_output=True)
    if listed.returncode:
        return False, "无法读取 Git 项目，请检查远端工作区和 Git 权限。"
    count = listed.stdout.count(b"\0")
    emit(f"Git 索引包含 {count} 个文件条目。")
    clean = True
    for name, args in (("工作区", []), ("暂存区", ["--cached"])):
        emit(f"正在检查{name}改动中的空白错误与冲突标记。")
        checked = subprocess.run(git + ["diff", "--check", "--no-ext-diff", "--no-textconv",
                                        "--ignore-submodules=all", *args, "--"], cwd=root,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if checked.returncode:
            clean = False
            emit(f"{name}检查未通过，可在云端执行 git diff {'--cached ' if args else ''}--check 查看详情。")
        else:
            emit(f"{name}检查通过。")
    return (True, "项目检查通过。") if clean else (False, "Git 检查发现问题，请查看上方结果。")


def _error_hint(message, provider):
    # CLI diagnostics can include request headers, paths and provider response bodies.
    # Keep only known error categories in the replay log.
    lower = message.lower()
    if any(term in lower for term in ("rate_limit", "rate limit", "quota", "429", "usage limit", "out of credits")):
        return "云端模型账户达到额度或速率限制，请检查账户后重试。"
    if any(term in lower for term in ("auth", "401", "403", "login", "log in", "not logged", "api key", "api_key", "unauthorized")):
        return f"云端 {provider} 认证失败，请在服务器检查 CLI 登录或密钥配置。"
    if any(term in lower for term in ("timed out", "timeout", "network", "connect", "resolve host")):
        return "云端 CLI 连接模型服务失败，请检查服务器网络后重试。"
    if any(term in lower for term in ("unknown option", "unexpected argument", "unrecognized", "unknown flag")):
        return f"云端 {provider} 版本不支持所需选项，请更新 CLI 后重试。"
    return None


def _agent(root, provider, prompt, emit):
    if provider == "codex":
        command = ["codex", "-a", "never", "exec", "--sandbox", "read-only", "--ephemeral", "--json", "-"]
    else:
        command = ["claude", "-p", "--verbose", "--output-format", "stream-json",
                   "--include-partial-messages", "--no-session-persistence",
                   "--permission-mode", "dontAsk", "--tools", "Read,Glob,Grep",
                   "--allowedTools", "Read,Glob,Grep", "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}']
    hint = None
    completed = False
    saw_partial = False
    saw_text = False
    # Read the prompt on stdin, so it cannot appear in a process listing.
    with tempfile.TemporaryFile() as source:
        source.write((prompt + "\n").encode("utf-8"))
        source.seek(0)
        proc = subprocess.Popen(command, cwd=root, stdin=source, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    with proc:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                hint = _error_hint(line, provider) or hint
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in ("error", "turn.failed") or event.get("error"):
                hint = _error_hint(line, provider) or hint
            if provider == "codex":
                item = event.get("item") or {}
                if kind == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                    emit(item["text"])
                elif kind == "item.started" and item.get("type") == "command_execution":
                    emit("Codex 正在运行只读命令。")
                elif kind == "turn.completed":
                    completed = True
            elif kind == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    emit(delta["text"], "delta")
                    saw_partial = saw_text = True
            elif kind == "assistant":
                if not saw_partial and not event.get("error"):
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text"):
                            emit(block["text"])
                            saw_text = True
                saw_partial = False
            elif kind == "result":
                completed = event.get("subtype") == "success" and not event.get("is_error")
                if not completed:
                    hint = _error_hint(line, provider) or hint
                elif not saw_text and isinstance(event.get("result"), str):
                    emit(event["result"])
        success = proc.wait() == 0 and completed
    if success:
        return True, "模型任务已完成。"
    return False, hint or f"云端 {provider} 未正常完成，请在服务器检查 CLI 版本、登录和模型配置。"


def _worker(root, task_id):
    with _database(root) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] != "queued" or row["pid"] != os.getpid():
                return
            db.execute("UPDATE task SET status='running' WHERE id=?", (task_id,))
            _append(db, task_id, "status", "任务正在云端运行。")

        def emit(text, kind="output"):
            with db:
                _append(db, task_id, kind, text)

        provider = row["provider"]
        try:
            if provider == "check":
                success, message = _git_check(root, emit)
            else:
                success, message = _agent(root, provider, row["prompt"], emit)
        except FileNotFoundError:
            success, message = False, f"云端缺少 {'git' if provider == 'check' else provider}，请先安装并加入 PATH。"
        except Exception:
            success, message = False, "任务执行中断，请查看远端 CLI 安装和项目权限。"
        with db:
            _finish(db, task_id, success, message)


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), sys.argv[2])
