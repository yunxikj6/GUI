"""Single-file Codex proposals. Only an explicit apply calls the workspace file writer."""
from contextlib import contextmanager
import difflib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

if __package__:
    from . import workspace
else:
    import workspace


LIMIT = 24 * 1024
_TIMEOUT = 300
_INVENTORY_TIMEOUT = 15
_INVENTORY_LIMIT = 256 * 1024
_NO_EXTENSIONS = ["--disable", "apps", "--disable", "plugins", "--disable", "hooks"]
_OUTPUT_LIMIT = 6 * LIMIT + 1024  # JSON can escape each input byte as six ASCII bytes.
_SCHEMA = {"type": "object", "properties": {"content": {"type": "string"}},
           "additionalProperties": False, "required": ["content"]}


def create(db, root, task_id, path, revision):
    if not workspace.relative_parts(path) or not isinstance(revision, str) or len(revision) != 64:
        raise ValueError("生成提案前请先打开已保存的文本文件。")
    source = workspace.file_preview(root, path)
    if source["kind"] != "text":
        raise ValueError("提案只支持 UTF-8 文本文件。")
    if source["size"] > LIMIT:
        raise ValueError("提案文件不能超过 24 KiB。")
    if source["revision"] != revision:
        raise workspace.WorkspaceError("conflict", "文件已改变，请重新读取后生成提案。")
    db.execute("""
        INSERT INTO proposal (taskId, path, revision, original) VALUES (?, ?, ?, ?)
    """, (task_id, path, revision, source["content"]))


@contextmanager
def _process(command, directory, timeout, stdin, stderr, env=None):
    # Runs in the detached Linux worker's main thread. Bound both stdout reads and wait().
    def expired(*_):
        raise TimeoutError
    with subprocess.Popen(command, cwd=directory, stdin=stdin, stdout=subprocess.PIPE,
                          stderr=stderr, env=env, start_new_session=True) as proc:
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            yield proc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def _enabled_servers(directory, env, overrides):
    command = ["codex", "mcp", "list", "--json", *_NO_EXTENSIONS, *overrides]
    with _process(command, directory, _INVENTORY_TIMEOUT, subprocess.DEVNULL, subprocess.DEVNULL, env) as proc:
        # Server descriptors can contain private arguments or headers. Never log or store them.
        raw = proc.stdout.read(_INVENTORY_LIMIT + 1)
        if len(raw) > _INVENTORY_LIMIT or proc.wait():
            raise ValueError()
        servers = json.loads(raw.decode("utf-8"))
    if not isinstance(servers, list) or len(servers) > 256:
        raise ValueError()
    enabled = set()
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError()
        name = server.get("name")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_:@/.-]{1,256}", name)
                or type(server.get("enabled")) is not bool):
            raise ValueError()
        if server["enabled"]:
            enabled.add(name)
    return enabled


def _disable_system_mcp(directory):
    # exec ignores user config but still loads system MCP definitions. Inventory an
    # empty user config with the same cwd and extension flags. Real exec retains auth.
    with tempfile.TemporaryDirectory(prefix="mcp-inventory-", dir=directory) as empty_home:
        env = dict(os.environ, CODEX_HOME=empty_home)
        names = _enabled_servers(directory, env, [])
        if not names:
            return []
        entries = ",".join(f'{json.dumps(name)}={{enabled=false}}' for name in sorted(names))
        overrides = ["-c", "mcp_servers={" + entries + "}"]
        if _enabled_servers(directory, env, overrides):
            raise ValueError()
        return overrides


def _run(command, directory, prompt, error_hint):
    hint = (0, "")
    completed = False
    with tempfile.TemporaryFile() as source:
        source.write(prompt.encode("utf-8"))
        source.seek(0)
        try:
            with _process(command, directory, _TIMEOUT, source, subprocess.STDOUT) as proc:
                # Diagnostics can contain file contents or account details. Never replay them.
                for block in iter(lambda: proc.stdout.readline(_OUTPUT_LIMIT + 1), b""):
                    line = block.decode("utf-8", "replace")
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        hint = max(hint, error_hint(line, "codex"))
                        continue
                    if isinstance(event, dict):
                        if event.get("type") == "turn.completed":
                            completed = True
                        elif event.get("type") in ("error", "turn.failed"):
                            hint = max(hint, error_hint(line, "codex"))
                if proc.wait() or not completed:
                    return hint[1] or "Codex 未能生成提案，请检查 CLI 版本和账户配置。"
        except TimeoutError:
            return hint[1] or "Codex 生成提案超过 5 分钟，请检查云端连接后重试。"
    return None


def generate(db, task_id, instruction, error_hint):
    row = db.execute("SELECT path, original FROM proposal WHERE taskId=?", (task_id,)).fetchone()
    prompt = ("根据用户要求编辑下方 JSON 中的单个文件，只在 content 字段返回完整替换文本。"
              "仅使用提供的内容，不调用工具。保留未涉及的内容、行尾和原有格式。\n"
              + json.dumps({"instruction": instruction, "path": row["path"], "original": row["original"]},
                           ensure_ascii=False))
    with tempfile.TemporaryDirectory(prefix="cloudwork-proposal-") as temporary:
        directory = Path(temporary)
        try:
            mcp_overrides = _disable_system_mcp(directory)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, RecursionError):
            return False, "无法确认此主机已为提案关闭外部工具。请检查 Codex CLI 和系统 MCP 配置。"
        schema = directory / "schema.json"
        output = directory / "result.json"
        schema.write_text(json.dumps(_SCHEMA), encoding="utf-8")
        command = ["codex", "-a", "never", "exec", "--sandbox", "read-only", "--ephemeral",
                   "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                   "--disable", "shell_tool", "--disable", "view_image", "--disable", "multi_agent",
                   "--disable", "goals", *_NO_EXTENSIONS, *mcp_overrides,
                   "-c", 'web_search="disabled"', "-c", "tools.experimental_request_user_input.enabled=false",
                   "--output-schema", str(schema), "-o", str(output), "--json", "-"]
        error = _run(command, directory, prompt, error_hint)
        if error:
            return False, error
        try:
            with output.open("rb") as stream:
                raw = stream.read(_OUTPUT_LIMIT + 1)
            if len(raw) > _OUTPUT_LIMIT:
                raise ValueError()
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict) or set(result) != {"content"}:
                raise ValueError()
            content = result["content"]
            if not isinstance(content, str) or "\x00" in content or len(content.encode("utf-8")) > LIMIT:
                raise ValueError()
        except (OSError, ValueError, RecursionError):
            return False, "Codex 未返回有效的单文件提案，候选文本须为 24 KiB 以内的 UTF-8 文本。"
    if content == row["original"]:
        return False, "候选文本与原文件相同，没有可审核的改动。"
    db.execute("UPDATE proposal SET candidate=?, decision='pending' WHERE taskId=?", (content, task_id))
    return True, "提案已生成，等待你查看差异并决定是否应用。"


def _row(db, task_id):
    row = db.execute("""
        SELECT p.*, t.status FROM proposal p JOIN task t ON t.id=p.taskId WHERE p.taskId=?
    """, (task_id,)).fetchone()
    if not row:
        raise workspace.WorkspaceError("not_found", "找不到这份文件提案。")
    return row


def _diff(row):
    if row["candidate"] is None:
        return ""
    # Split only at LF, preserving CRs and final-newline changes exactly in the patch.
    def lines(value):
        parts = value.split("\n")
        return [line + "\n" for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    patch = difflib.unified_diff(lines(row["original"]), lines(row["candidate"]),
                                 "a/" + row["path"], "b/" + row["path"])
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in patch)


def _public(row):
    return {"taskId": row["taskId"], "path": row["path"], "revision": row["revision"],
            "decision": "failed" if row["status"] == "failed" else row["decision"], "diff": _diff(row)}


def _matches_file(root, path, content):
    try:
        return workspace.read_bytes(root, path, LIMIT) == content.encode("utf-8")
    except (OSError, workspace.WorkspaceError):
        return False


def _recover_apply(db, root, task_id):
    row = _row(db, task_id)
    if row["decision"] == "pending" and row["applyIntent"]:
        # A process can exit after the atomic file replacement and before SQLite commit.
        if _matches_file(root, row["path"], row["candidate"]):
            db.execute("UPDATE proposal SET decision='applied', applyIntent=0 WHERE taskId=?", (task_id,))
            row = _row(db, task_id)
    return row


def inspect(db, root, task_id):
    return _public(_recover_apply(db, root, task_id))


def _terminal(db, row, action):
    if row["decision"] == ("applied" if action == "apply" else "rejected"):
        return _public(row)
    if row["decision"] != "pending":
        # Keep a recovered acknowledgement even if this caller requested the opposite action.
        db.commit()
        raise workspace.WorkspaceError("proposal_decided", "此提案当前不能执行这项操作，请刷新查看。")
    return None


def decide(db, root, task_id, action):
    if action not in ("apply", "reject"):
        raise ValueError("提案操作应为 apply 或 reject。")
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = _recover_apply(db, root, task_id)
        terminal = _terminal(db, row, action)
        if terminal:
            return terminal
        if action == "reject":
            db.execute("UPDATE proposal SET decision='rejected', applyIntent=0 WHERE taskId=?", (task_id,))
            return _public(_row(db, task_id))
        # Only accept a new apply intent against the exact reviewed version.
        if not _matches_file(root, row["path"], row["original"]):
            raise workspace.WorkspaceError("conflict", "无法确认云端文件仍为原版本。提案仍可查看，请重新生成或拒绝。")
        db.execute("UPDATE proposal SET applyIntent=1 WHERE taskId=?", (task_id,))

    with db:
        db.execute("BEGIN IMMEDIATE")
        row = _recover_apply(db, root, task_id)
        terminal = _terminal(db, row, action)
        if terminal:
            return terminal
        workspace.save(root, row["path"], row["candidate"], row["revision"])
        db.execute("UPDATE proposal SET decision='applied', applyIntent=0 WHERE taskId=?", (task_id,))
        return _public(_row(db, task_id))
