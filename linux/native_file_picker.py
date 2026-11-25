"""Choose a cloud text file using the distribution's native Zenity list dialog."""
import html
import subprocess
import unicodedata

from cloud.workspace import MAX_TEXT

_SIZE_LABEL = f"{MAX_TEXT // 1024} KiB"
# Zenity 4 on Ubuntu pauses 10 ms per stdin cell. Keep its native list responsive.
_PAGE_ROWS = 40


def _display(text):
    """Zenity reads one cell per line. Keep filename controls visible and unambiguous."""
    escaped = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    parts = []
    for ch in text:
        if ch in escaped:
            parts.append(escaped[ch])
        elif unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            parts.append(f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
        else:
            parts.append(ch)
    value = "".join(parts)
    # Older Zenity strips trailing ASCII whitespace from stdin cells.
    return value.rstrip(" ") + "\\x20" * (len(value) - len(value.rstrip(" ")))


def _dialog(options, cells=""):
    try:
        result = subprocess.run(
            ["zenity", "--no-markup", "--title=选择云端文件", *options],
            input=cells, capture_output=True, encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("打开文件选择器需要本机安装 Zenity。") from exc
    if result.returncode == 0:
        return result.stdout.rstrip("\r\n")
    if result.returncode == 1 and "cannot open display" not in result.stderr.lower():
        return None
    raise RuntimeError("无法打开文件选择器。请从本机图形桌面重试。")


def choose_file(rpc, machine, project) -> str | None:
    """Return an unchanged project-relative path, or None when the user cancels."""
    directory = ""
    cursor, offset = None, 0
    history = []
    note = ""
    last_request = None
    while True:
        request = {"op": "tree", "path": directory}
        if cursor is not None:
            request["cursor"] = cursor
        try:
            if request != last_request:
                page = rpc(machine, project, request)
                last_request = request
        except Exception as exc:
            if cursor is None or getattr(exc, "code", None) != "invalid_cursor":
                raise
            cursor, offset = None, 0
            history.clear()
            last_request = None
            note = "目录已经变化，已返回本目录首页。"
            continue

        rows = []
        if directory:
            rows.append(("parent", "上一级", "导航", ""))
        if history:
            rows.append(("first", "返回本目录首页", "导航", ""))
        if len(history) > 1:
            rows.append(("previous", "上一页", "导航", ""))
        entries = page["entries"]
        # A refreshed directory can be shorter when navigating back across RPC pages.
        offset = min(offset, max(0, (len(entries) - 1) // _PAGE_ROWS) * _PAGE_ROWS)
        for item in entries[offset:offset + _PAGE_ROWS]:
            name = item["name"]
            if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("云端返回了无效的目录项。")
            path = directory + "/" + name if directory else name
            if item["type"] == "dir":
                rows.append(("dir", _display(name), "文件夹", path))
            elif item["type"] == "file":
                if item.get("size", 0) > MAX_TEXT:
                    rows.append(("large", _display(name), f"超过 {_SIZE_LABEL}", path))
                elif item.get("binary") is True:
                    rows.append(("binary", _display(name), "二进制文件", path))
                else:
                    rows.append(("file", _display(name), "文件", path))

        next_cursor = page.get("nextCursor")
        more_local = offset + _PAGE_ROWS < len(entries)
        if more_local or next_cursor:
            rows.append(("next", "下一页", "导航", ""))
        elif page.get("truncated"):
            note = "当前连接只返回了部分目录内容，无法继续翻页。"
        if not entries and not note:
            note = "此目录没有可选择的文件。"
        if not rows:
            _dialog(["--info", "--text=" + note])
            return None

        location = project.rstrip("/") + "/" + directory if directory else project
        # Zenity 4 uses g_strcompress and Pango markup for list headings, even with --no-markup.
        heading = html.escape(_display(location).replace("\\", "\\\\"), quote=False)
        text = "目录  " + heading + f"\n可打开不超过 {_SIZE_LABEL} 的 UTF-8 文本文件。"
        if history or more_local or next_cursor:
            text += f"\n第 {len(history) + 1} 页"
        if note:
            text += "\n" + note
        # Only opaque local row numbers are returned. Data never enters Zenity option parsing.
        cells = "".join(f"{idx}\n{label}\n{kind}\n" for idx, (_, label, kind, _) in enumerate(rows))
        choice = _dialog([
            "--list", "--width=660", "--height=480", "--text=" + text,
            "--column=编号", "--column=名称", "--column=类型",
            "--hide-column=1", "--print-column=1", "--ok-label=选择", "--cancel-label=取消",
        ], cells)
        if choice is None:
            return None
        if not choice:
            note = "请选择一行。"
            continue
        if not choice.isascii() or not choice.isdecimal() or len(choice) > 6 or int(choice) >= len(rows):
            raise RuntimeError("文件选择器返回了无效的选择。")
        action, _, _, path = rows[int(choice)]
        note = ""
        if action == "file":
            return path
        if action in {"dir", "parent", "first"}:
            if action == "dir":
                directory = path
            elif action == "parent":
                directory = directory.rpartition("/")[0]
            cursor, offset = None, 0
            history.clear()
        elif action == "previous":
            cursor, offset = history.pop()
        elif action == "next":
            history.append((cursor, offset))
            cursor, offset = (cursor, offset + _PAGE_ROWS) if more_local else (next_cursor, 0)
        else:
            note = f"所选文件超过 {_SIZE_LABEL}。" if action == "large" else "所选文件为二进制文件。"
