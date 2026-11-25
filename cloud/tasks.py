"""Detached, replayable tasks for a single-user Linux workspace."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

if __package__:
    from . import codex_tools, proposals, workspace
else:
    import codex_tools
    import proposals
    import workspace


_PAGE_BYTES = 256 * 1024
_CHUNK_CHARS = 8192
_TASK_LIMIT = 80
_FIRST_PAGE = object()
_TURN_TABLES = {"codex": "codex_turn", "claude": "claude_turn"}
_CLAUDE_TOOLS = {
    "Read": "Claude 正在使用 Read 读取文件。",
    "Glob": "Claude 正在使用 Glob 查找文件。",
    "Grep": "Claude 正在使用 Grep 搜索文本。",
}
# Old databases can contain individual messages larger than a transport response.
# Reserve a separate, JavaScript-safe cursor range for their virtual chunks. A
# pre-upgrade integer still skips exactly the records that client already read.
_LEGACY_BASE = 1 << 40
_LEGACY_STRIDE = 1 << 20


def capabilities(root: Path) -> dict:
    result = {}
    for provider, binary in (("check", "git"), ("codex", "codex"), ("claude", "claude"),
                             ("proposal", "codex")):
        available = shutil.which(binary) is not None
        if not available:
            detail = f"请在云服务器安装 {binary} 并确认已加入 PATH。"
        elif provider == "check":
            detail = "用 Git 检查项目，无需模型账户。"
        elif provider == "proposal":
            detail = "用 Codex 为单个文本文件生成提案，审核后应用。"
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
            CREATE INDEX IF NOT EXISTS task_unfinished ON task(status)
                WHERE status IN ('queued', 'running', 'cancelling');
            CREATE TABLE IF NOT EXISTS codex_turn (
                taskId TEXT PRIMARY KEY, parentTaskId TEXT UNIQUE, threadId TEXT
            );
            CREATE TABLE IF NOT EXISTS claude_turn (
                taskId TEXT PRIMARY KEY, parentTaskId TEXT UNIQUE, threadId TEXT
            );
            CREATE TABLE IF NOT EXISTS proposal (
                taskId TEXT PRIMARY KEY, path TEXT NOT NULL, revision TEXT NOT NULL,
                original TEXT NOT NULL, candidate TEXT, decision TEXT NOT NULL DEFAULT 'generating',
                applyIntent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS task_control (
                taskId TEXT PRIMARY KEY, success INTEGER, message TEXT
            );
            CREATE VIEW IF NOT EXISTS task_history AS
                SELECT t.*, t.rowid AS taskRowId, turn.parentTaskId, child.taskId AS nextTaskId,
                       (t.status = 'succeeded' AND turn.threadId IS NOT NULL
                        AND child.taskId IS NULL) AS canContinue
                FROM task t LEFT JOIN codex_turn turn ON turn.taskId = t.id
                            LEFT JOIN codex_turn child ON child.parentTaskId = t.id;
            CREATE VIEW IF NOT EXISTS controlled_task_history AS
                SELECT h.*, (c.taskId IS NOT NULL AND h.status IN ('queued','running')) AS canCancel
                FROM task_history h LEFT JOIN task_control c ON c.taskId=h.id;
            -- Keep both original views Codex-only: older immutable workers use them
            -- to validate a Codex parent without checking the parent's provider.
            CREATE VIEW IF NOT EXISTS model_task_history AS
                SELECT h.id, h.requestId, h.provider, h.prompt, h.status, h.createdAt, h.finishedAt,
                       h.pid, h.taskRowId, h.canCancel,
                       CASE WHEN h.provider='claude' THEN turn.parentTaskId ELSE h.parentTaskId END AS parentTaskId,
                       CASE WHEN h.provider='claude' THEN child.taskId ELSE h.nextTaskId END AS nextTaskId,
                       CASE WHEN h.provider='claude'
                            THEN (h.status='succeeded' AND turn.threadId IS NOT NULL AND child.taskId IS NULL)
                            ELSE h.canContinue END AS canContinue
                FROM controlled_task_history h LEFT JOIN claude_turn turn ON turn.taskId=h.id
                                               LEFT JOIN claude_turn child ON child.parentTaskId=h.id;
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
    result = {key: row[key] for key in (
        "id", "requestId", "provider", "prompt", "status", "createdAt", "finishedAt",
        "parentTaskId", "nextTaskId"
    )}
    result["canContinue"] = bool(row["canContinue"])
    result["canCancel"] = bool(row["canCancel"])
    return result


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
    for row in db.execute("""
        SELECT id, pid, processKey, status FROM task WHERE status IN ('queued', 'running', 'cancelling')
    """).fetchall():
        if not row["processKey"] or _process_key(row["pid"]) != row["processKey"]:
            message = ("云端监督进程已退出，无法确认所有子进程已经停止。"
                       if row["status"] == "cancelling" else
                       "云端任务进程已退出。已保留输出，可创建新任务重试。")
            _finish(db, row["id"], False, message)


def start_task(root: Path, request_id: str, provider: str, prompt: str,
               parent_task_id: str | None = None, path: str | None = None,
               revision: str | None = None) -> dict:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("requestId 长度应为 1 至 128 个字符。")
    if provider not in ("codex", "claude", "check", "proposal"):
        raise ValueError("不支持的任务类型。")
    if not isinstance(prompt, str) or len(prompt) > 32000 or (provider != "check" and not prompt.strip()):
        raise ValueError("请输入任务内容，最多 32000 个字符。")
    if parent_task_id is not None and (provider not in _TURN_TABLES or not isinstance(parent_task_id, str)
                                       or not 1 <= len(parent_task_id) <= 128):
        raise ValueError("追问需要指定当前工作区的模型任务。")
    if provider != "proposal" and (path is not None or revision is not None):
        raise ValueError("仅文件提案接受目标文件和版本。")

    root = Path(root).resolve(strict=True)
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        existing = db.execute("SELECT * FROM model_task_history WHERE requestId=?", (request_id,)).fetchone()
        if existing:
            if (existing["provider"] != provider or existing["prompt"] != prompt
                    or existing["parentTaskId"] != parent_task_id):
                raise ValueError("此 requestId 已用于其他任务内容。")
            if provider == "proposal":
                target = db.execute("SELECT path, revision FROM proposal WHERE taskId=?",
                                    (existing["id"],)).fetchone()
                if not target or tuple(target) != (path, revision):
                    raise ValueError("此 requestId 已用于其他文件或版本。")
            return _public(existing)
        if parent_task_id is not None:
            parent = db.execute("SELECT provider, canContinue, nextTaskId FROM model_task_history WHERE id=?",
                                (parent_task_id,)).fetchone()
            if parent and parent["provider"] != provider:
                raise ValueError("追问必须使用与上一轮相同的模型。")
            if parent and parent["nextTaskId"]:
                raise ValueError("这一轮已经有追问，请刷新并查看下一轮。")
            if not parent or not parent["canContinue"]:
                raise ValueError("这一轮没有可继续的模型会话，请新建任务。")
        task_id = uuid.uuid4().hex
        db.execute("""
            INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
            VALUES (?, ?, ?, ?, 'queued', ?)
        """, (task_id, request_id, provider, prompt, _now()))
        db.execute("INSERT INTO task_control (taskId) VALUES (?)", (task_id,))
        if provider in _TURN_TABLES:
            db.execute(f"INSERT INTO {_TURN_TABLES[provider]} (taskId, parentTaskId) VALUES (?, ?)",
                       (task_id, parent_task_id))
        elif provider == "proposal":
            proposals.create(db, root, task_id, path, revision)
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
        return _public(db.execute("SELECT * FROM model_task_history WHERE id=?", (task_id,)).fetchone())


def cancel_task(root: Path, task_id: str) -> dict:
    if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
        raise ValueError("请选择需要取消的任务。")
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        row = db.execute("SELECT * FROM model_task_history WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise workspace.WorkspaceError("not_found", "找不到该任务。")
        if row["status"] not in ("queued", "running"):
            return _public(row)
        if not row["canCancel"]:
            raise workspace.WorkspaceError("cancel_unsupported", "当前执行器无法安全取消这项任务。")
        db.execute("UPDATE task SET status='cancelling' WHERE id=?", (task_id,))
        _append(db, task_id, "status", "已请求取消，正在停止云端任务。")
        return _public(db.execute("SELECT * FROM model_task_history WHERE id=?", (task_id,)).fetchone())


def _summary_rows(db, limit, where="", values=()):
    return db.execute(f"""
        SELECT id, requestId, provider, substr(prompt, 1, 160) AS prompt, status,
               createdAt, finishedAt, parentTaskId, nextTaskId, canContinue, canCancel
        FROM model_task_history {where} ORDER BY createdAt DESC, taskRowId DESC LIMIT ?
    """, (*values, limit)).fetchall()


def task_page(root: Path, before_task_id=_FIRST_PAGE) -> dict:
    if before_task_id is not _FIRST_PAGE and (
        not isinstance(before_task_id, str) or not 1 <= len(before_task_id) <= 128
        or not before_task_id.isprintable() or not before_task_id.strip()
    ):
        raise workspace.WorkspaceError("invalid_cursor", "beforeTaskId 应为上一页返回的任务 ID。")
    limit = _TASK_LIMIT + 1
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        if before_task_id is _FIRST_PAGE:
            rows = _summary_rows(db, limit)
        else:
            anchor = db.execute("SELECT createdAt, rowid FROM task WHERE id=?", (before_task_id,)).fetchone()
            if not anchor:
                raise workspace.WorkspaceError("cursor_not_found", "当前工作区找不到分页起点，请刷新任务历史。")
            # The createdAt index also stores rowid. Two disjoint seeks use both keys
            # even on SQLite versions that only seek the first column of a tuple inequality.
            rows = _summary_rows(db, limit, "WHERE createdAt=? AND taskRowId<?", tuple(anchor))
            if len(rows) < limit:
                rows.extend(_summary_rows(db, limit - len(rows), "WHERE createdAt<?", (anchor["createdAt"],)))
    has_more = len(rows) > _TASK_LIMIT
    items = [_public(row) for row in rows[:_TASK_LIMIT]]
    return {"tasks": items, "nextBeforeTaskId": items[-1]["id"] if has_more else None}


def list_tasks(root: Path) -> list[dict]:
    return task_page(root)["tasks"]


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
        row = db.execute("SELECT * FROM model_task_history WHERE id=?", (task_id,)).fetchone()
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


def get_proposal(root: Path, task_id: str) -> dict:
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _recover(db)
        return proposals.inspect(db, root, task_id)


def decide_proposal(root: Path, task_id: str, decision: str) -> dict:
    with _database(root) as db:
        return proposals.decide(db, root, task_id, decision)


def _git_check(root, emit):
    emit("正在统计 Git 跟踪的项目文件。")
    git = ["git", "--no-optional-locks"]
    with subprocess.Popen(git + ["ls-files", "--cached", "-z"], cwd=root, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as listing:
        count = sum(chunk.count(b"\0") for chunk in codex_tools.output_chunks(listing))
        if listing.wait():
            return False, "无法读取 Git 项目，请检查远端工作区和 Git 权限。"
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
    # A transport retry can follow a more specific authentication/quota failure.
    lower = message.lower()
    if provider == "claude" and "no conversation found with session id" in lower:
        return 4, "云端 Claude 未找到这段会话的历史，请新建任务。"
    if "no rollout found for thread" in lower:
        return 4, "云端 Codex 未找到这段会话的历史，请新建任务。"
    if any(term in lower for term in ("rate_limit", "rate limit", "quota", "429", "usage limit", "out of credits")):
        return 3, "云端模型账户达到额度或速率限制，请检查账户后重试。"
    if any(term in lower for term in ("auth", "401", "403", "login", "log in", "not logged", "api key", "api_key", "unauthorized")):
        return 3, f"云端 {provider} 认证失败，请在服务器检查 CLI 登录或密钥配置。"
    if any(term in lower for term in ("timed out", "timeout", "network", "connect", "resolve host")):
        return 1, "云端 CLI 连接模型服务失败，请检查服务器网络后重试。"
    if any(term in lower for term in ("unknown option", "unexpected argument", "unrecognized",
                                      "unknown flag", "unknown feature")):
        if provider == "claude" and "--safe-mode" in lower:
            return 4, "当前 Claude Code 不支持 --safe-mode，请升级到支持该选项的版本后重试。"
        return 2, f"云端 {provider} 版本不支持所需选项，请更新 CLI 后重试。"
    return 0, ""


def _agent(root, provider, prompt, emit, resume_thread):
    if provider == "codex":
        try:
            tool_args = codex_tools.restricted_tool_args(root)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, RecursionError):
            return False, "无法确认此工作区的 Codex 外部工具已关闭。请检查 CLI 版本和 MCP 配置后重试。", None
        command = ["codex", "-a", "never", "exec", "--sandbox", "read-only", "--json", *tool_args]
        if resume_thread:
            command.extend(["resume", resume_thread])
        command.append("-")
    else:
        claude_session = resume_thread or str(uuid.uuid4())
        tools = ",".join(_CLAUDE_TOOLS)
        command = ["claude", "-p", "--safe-mode", "--verbose", "--output-format", "stream-json",
                   "--include-partial-messages", "--resume" if resume_thread else "--session-id", claude_session,
                   "--permission-mode", "dontAsk", "--tools", tools,
                   "--allowedTools", tools, "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}']
    hint = (0, "")
    completed = False
    thread_id = None
    claude_initialized = False
    session_mismatch = False
    streamed_text_blocks = set()
    reported_tools = set()
    saw_text = False

    def tool_progress(block):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return
        name, call_id = block.get("name"), block.get("id")
        if (isinstance(name, str) and name in _CLAUDE_TOOLS and isinstance(call_id, str)
                and call_id and call_id not in reported_tools):
            reported_tools.add(call_id)
            emit(_CLAUDE_TOOLS[name], "status")

    # Read the prompt on stdin, so it cannot appear in a process listing.
    with tempfile.TemporaryFile() as source:
        source.write((prompt + "\n").encode("utf-8"))
        source.seek(0)
        proc = subprocess.Popen(command, cwd=root, stdin=source, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    with proc:
        for line in codex_tools.output_lines(proc):
            try:
                event = json.loads(line)
            except ValueError:
                hint = max(hint, _error_hint(line, provider))
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in ("error", "turn.failed") or event.get("error"):
                hint = max(hint, _error_hint(line, provider))
            if provider == "claude":
                # Failed resumes can report an unrelated ID in their diagnostic result.
                if kind == "result" and (event.get("subtype") != "success" or event.get("is_error")):
                    completed = False
                    hint = max(hint, _error_hint(line, provider))
                    continue
                reported = event.get("session_id")
                if reported is not None and reported != claude_session:
                    session_mismatch = True
                if session_mismatch:
                    continue
                if kind == "system" and event.get("subtype") == "init":
                    claude_initialized = reported == claude_session
                if resume_thread and not claude_initialized and kind != "result":
                    continue
            if provider == "codex":
                item = event.get("item") or {}
                if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                    try:
                        thread_id = str(uuid.UUID(event["thread_id"]))
                    except ValueError:
                        thread_id = None
                elif kind == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                    emit(item["text"])
                elif kind == "item.started" and item.get("type") == "command_execution":
                    emit("Codex 正在运行只读命令。")
                elif kind == "turn.completed":
                    completed = True
            elif kind == "stream_event":
                stream = event.get("event") or {}
                if not isinstance(stream, dict) or event.get("error"):
                    continue
                if stream.get("type") == "content_block_start":
                    tool_progress(stream.get("content_block"))
                delta = stream.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    emit(delta["text"], "delta")
                    index = stream.get("index")
                    streamed_text_blocks.add(index if type(index) is int else None)
                    saw_text = True
            elif kind == "assistant":
                blocks = (event.get("message") or {}).get("content") or []
                if not event.get("error"):
                    for index, block in enumerate(blocks):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tool_progress(block)
                        elif block.get("type") == "text" and block.get("text"):
                            # Some CLI versions emit just the completed block in this
                            # message, although its streamed index was relative to the full turn.
                            if (index in streamed_text_blocks or None in streamed_text_blocks
                                    or (len(blocks) == 1 and streamed_text_blocks)):
                                continue
                            emit(block["text"])
                            saw_text = True
                streamed_text_blocks.clear()
            elif kind == "result":
                completed = True
                if claude_initialized and event.get("session_id") == claude_session:
                    thread_id = claude_session
                if not saw_text and (not resume_thread or thread_id) and isinstance(event.get("result"), str):
                    emit(event["result"])
        success = proc.wait() == 0 and completed
    if session_mismatch:
        return False, "Claude 回报的会话标识与请求不符，请新建任务。", None
    if success and resume_thread and thread_id != resume_thread:
        name = "Codex" if provider == "codex" else "Claude"
        return False, f"{name} 未确认已恢复指定会话，请新建任务。", None
    if success:
        if provider == "claude" and thread_id is None:
            return True, "模型任务已完成，Claude 未确认可恢复的会话。", None
        return True, "模型任务已完成。", thread_id
    return False, hint[1] or f"云端 {provider} 未正常完成，请在服务器检查 CLI 版本、登录和模型配置。", None


def _execute(root, task_id, supervised):
    with _database(root) as db:
        row = db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        owner = os.getppid() if supervised else os.getpid()
        if not row or row["status"] != "running" or row["pid"] != owner:
            return

        def emit(text, kind="output"):
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT status FROM task WHERE id=?", (task_id,)).fetchone()[0] == "running":
                    _append(db, task_id, kind, text)

        thread_id = None
        provider = row["provider"]
        try:
            if provider == "check":
                success, message = _git_check(root, emit)
            elif provider == "proposal":
                success, message = proposals.generate(db, task_id, row["prompt"], _error_hint)
            else:
                turns = _TURN_TABLES[provider]
                parent = db.execute(f"""
                    SELECT parent.threadId FROM {turns} turn
                    LEFT JOIN {turns} parent ON parent.taskId = turn.parentTaskId
                    WHERE turn.taskId = ? AND turn.parentTaskId IS NOT NULL
                """, (task_id,)).fetchone()
                # A broken parent link must never turn a follow-up into a fresh conversation.
                resume_thread = str(uuid.UUID(parent["threadId"])) if parent else None
                success, message, thread_id = _agent(root, provider, row["prompt"], emit, resume_thread)
        except FileNotFoundError:
            binary = {"check": "git", "proposal": "codex"}.get(provider, provider)
            success, message = False, f"云端缺少 {binary}，请先安装并加入 PATH。"
        except Exception:
            success, message = False, "任务执行中断，请查看远端 CLI 安装和项目权限。"
        with db:
            # Proposal generation may already hold a write transaction with a staged candidate.
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT status FROM task WHERE id=?", (task_id,)).fetchone()[0] != "running":
                db.rollback()
                return
            if thread_id:
                db.execute(f"UPDATE {_TURN_TABLES[provider]} SET threadId=? WHERE taskId=?", (thread_id, task_id))
            if supervised:
                db.execute("UPDATE task_control SET success=?, message=? WHERE taskId=?",
                           (success, message, task_id))
            else:
                _finish(db, task_id, success, message)


def _child_pids():
    owner = os.getpid()
    try:
        return [int(value) for value in Path(f"/proc/self/task/{owner}/children").read_text().split()]
    except FileNotFoundError:
        # Some Linux builds omit CONFIG_PROC_CHILDREN. The kernel's PPID is equivalent
        # here: a direct child stays ours, including as a zombie, until this thread waits.
        children = []
        with os.scandir("/proc") as entries:
            for entry in entries:
                if entry.name.isdecimal():
                    try:
                        fields = (Path(entry.path) / "stat").read_bytes().rsplit(b") ", 1)[1].split()
                        if int(fields[1]) == owner:
                            children.append(int(entry.name))
                    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
                        continue
        return children


def _enable_subreaper():
    # Linux PR_SET_CHILD_SUBREAPER. Only this newly spawned, task-private process is changed.
    try:
        import ctypes
        if (not os.access("/proc", os.R_OK | os.X_OK) or len(os.listdir("/proc/self/task")) != 1
                or not _process_key(os.getpid())):
            return False
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            pass
        else:
            return False  # Never supervise an interpreter which already owns children.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        return ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    except (ImportError, AttributeError, OSError):
        return False


def _stop_children():
    # No other thread reaps children here. A direct child cannot have its PID reused
    # until we wait for it. Reparenting on exit exposes even descendants with new sessions.
    escalate_at = time.monotonic() + 0.35
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        sig = signal.SIGKILL if time.monotonic() >= escalate_at else signal.SIGTERM
        for pid in _child_pids():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.025)


def _cancelled(db, task_id):
    db.execute("UPDATE task SET status='cancelled', finishedAt=? WHERE id=?", (_now(), task_id))
    db.execute("UPDATE proposal SET candidate=NULL, decision='cancelled' WHERE taskId=?", (task_id,))
    _append(db, task_id, "status", "云端任务已取消。")


def _supervise(root, task_id):
    # No SQLite connection is open across fork. The executor keeps the write end until
    # exit; its CLI subprocesses close this non-inheritable descriptor on exec.
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as finished, os.fdopen(write_fd, "wb") as keep_open:
        try:
            child = os.fork()
        except OSError:
            return
        if child == 0:
            finished.close()
            try:
                _execute(root, task_id, True)
            finally:
                os._exit(0)
        keep_open.close()
        try:
            with _database(root) as db:
                poll = select.poll()
                poll.register(finished, select.POLLIN)
                while db.execute("SELECT status FROM task WHERE id=?", (task_id,)).fetchone()[0] == "running":
                    if poll.poll(120):
                        break
        finally:
            _stop_children()


def _worker(root, task_id):
    supervised = _enable_subreaper()
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status, pid, canCancel FROM model_task_history WHERE id=?",
                         (task_id,)).fetchone()
        if not row or row["pid"] != os.getpid():
            return
        if row["status"] == "cancelling":
            _cancelled(db, task_id)
            return
        if row["status"] != "queued":
            return
        supervised = supervised and bool(row["canCancel"])
        if not supervised:
            db.execute("DELETE FROM task_control WHERE taskId=?", (task_id,))
        db.execute("UPDATE task SET status='running' WHERE id=?", (task_id,))
        _append(db, task_id, "status", "任务正在云端运行。")
    if not supervised:
        _execute(root, task_id, False)
        return

    # RPC processes never send signals to a PID obtained from the database.
    _supervise(root, task_id)
    with _database(root) as db, db:
        db.execute("BEGIN IMMEDIATE")
        result = db.execute("""
            SELECT t.status, c.success, c.message FROM task t
            JOIN task_control c ON c.taskId=t.id WHERE t.id=?
        """, (task_id,)).fetchone()
        if result["status"] == "cancelling":
            _cancelled(db, task_id)
        elif result["status"] == "running":
            _finish(db, task_id, bool(result["success"]),
                    result["message"] or "云端执行进程已中断，已保留任务输出。")


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), sys.argv[2])
