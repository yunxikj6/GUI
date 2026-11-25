"""Connect the current GNOME user's native file actions to an existing cloud project."""
from __future__ import annotations

import os
import re
import subprocess

from instabox import BridgeError, InstaBox, InstaBoxAccount
from linux.native_file_picker import _display
from workspace_mcp import VAULT_SERVICE, system_vault


_VAULT_HELP = "请在当前 Ubuntu 桌面用户会话中解锁登录密钥环（Secret Service），然后重新打开项目设置。"
_MACHINE = re.compile(r"m_[A-Za-z0-9_]+")
_STATES = {"running": "运行中", "stopped": "已停止", "suspended": "已暂停"}


class Zenity:
    def run(self, title, options, *, cells=""):
        try:
            result = subprocess.run(
                ["zenity", "--no-markup", "--title=" + title, *options],
                input=cells, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="strict",
                env={k: v for k, v in os.environ.items() if k != "INSTABOX_API_KEY"},
            )
        except (OSError, UnicodeError):
            raise ValueError("无法打开系统对话框。请从 Ubuntu 图形桌面打开项目设置。") from None
        if result.returncode == 0:
            return result.stdout.removesuffix("\n")
        if result.returncode == 1 and "cannot open display" not in (result.stderr or "").lower():
            return None
        raise ValueError("系统对话框无法完成操作。请重新打开项目设置。")

    def choose(self, title, prompt, columns, rows):
        # All remote strings are plain text stdin cells. Only local row numbers are returned.
        cells = "".join(f"{idx}\n" + "".join(_display(c) + "\n" for c in row)
                        for idx, row in enumerate(rows))
        choice = self.run(title, [
            "--list", "--width=640", "--height=420", "--text=" + prompt, "--column=编号",
            *["--column=" + c for c in columns], "--hide-column=1", "--print-column=1",
            "--ok-label=下一步", "--cancel-label=取消",
        ], cells=cells)
        if choice is None:
            return None
        if not choice.isascii() or not choice.isdecimal() or len(choice) > 5 or int(choice) >= len(rows):
            raise ValueError("请重新打开项目设置并选中一行。")
        return int(choice)

    def entry(self, title, prompt, initial=""):
        return self.run(title, ["--entry", "--text=" + prompt, "--entry-text=" + initial,
                                "--ok-label=下一步", "--cancel-label=取消"])

    def password(self):
        return self.run("InstaBox API 密钥", ["--password", "--ok-label=读取云机", "--cancel-label=取消"])

    def confirm(self, machine, project, action):
        # --question with --no-markup takes plain text; Zenity's message body expands backslashes.
        target = _display(project).replace("\\", "\\\\")
        body = f"云机  {machine}\n项目目录  {target}\n\n{action}"
        return self.run("确认连接云项目", ["--question", "--default-cancel", "--width=540",
                        "--text=" + body, "--ok-label=确认连接", "--cancel-label=取消"]) is not None


def machine_page(key, cursor):
    return InstaBoxAccount(key).machine_page(cursor)


def check_project(key, machine, project):
    data = InstaBox(key, machine, project).file_request({"op": "tree", "path": ""})
    if data.get("path") != "" or not isinstance(data.get("entries"), list):
        raise ValueError("目录读取结果无效。")


def _select_machine(ui, key, list_page):
    cursor = None
    seen = set()
    note = ""
    while True:
        try:
            page = list_page(key, cursor)
            machines = page["machines"]
            if not isinstance(machines, list) or any(
                not isinstance(m, dict) or not isinstance(m.get("id"), str)
                or not _MACHINE.fullmatch(m["id"]) or not isinstance(m.get("state"), str)
                or not isinstance(m.get("name", ""), str) for m in machines
            ):
                raise ValueError()
            following = page.get("nextCursor")
            if following is not None and (not isinstance(following, str) or not following or following in seen):
                raise ValueError()
        except Exception:
            raise ValueError("无法读取云机列表。请检查网络及 API 密钥读取云机的权限，再重新打开设置。") from None
        rows = [(m.get("name") or m["id"], m["id"], _STATES.get(m["state"], m["state"])) for m in machines]
        if following:
            rows.append(("下一页", "", "导航"))
        if cursor is not None:
            rows.append(("返回云机列表首页", "", "导航"))
        if not rows:
            raise ValueError("这个账号的列表中没有可选择的云机。请核对密钥所属账号。")
        choice = ui.choose("选择 InstaBox 云机", note or "选择已运行的云机。", ("名称", "机器 ID", "状态"), rows)
        if choice is None:
            return None
        if cursor is not None and choice == len(rows) - 1:
            cursor = None
            seen.clear()
            note = ""
        elif choice == len(machines):
            cursor = following
            seen.add(cursor)
            note = ""
        elif machines[choice]["state"] == "running":
            return machines[choice]["id"]
        else:
            note = "这台云机尚未运行。请选择运行中的云机，或在 InstaBox 启动后再试。"


def configure_project(validate, integrate, *, current=None, dialogs=None, vault_factory=system_vault,
                      list_page=machine_page, check_tree=check_project) -> bool:
    """Bind one validated project. Return False for cancellation at any dialog."""
    ui = dialogs or Zenity()
    machine_hint, project_hint = current or (None, "")
    saved_label = ("使用这台机器的已存密钥", machine_hint) if machine_hint else ("使用已存密钥", "按机器 ID 查找，缺少时输入")
    fresh = ("输入 API 密钥", "从 InstaBox 读取云机列表")
    options = [saved_label, fresh] if machine_hint else [fresh, saved_label]
    source = ui.choose("连接 InstaBox 项目", "选择如何读取云机列表。", ("凭据来源", "说明"), options)
    if source is None:
        return False
    try:
        vault = vault_factory()
    except Exception:
        raise ValueError(_VAULT_HELP) from None

    def stored(machine):
        try:
            return vault.get_password(VAULT_SERVICE, machine)
        except Exception:
            raise ValueError(_VAULT_HELP) from None

    key = None
    if options[source] == saved_label:
        source_machine = machine_hint or ui.entry("使用已存密钥", "输入曾在 Ubuntu 密钥环登记的机器 ID（m_…）。")
        if source_machine is None:
            return False
        if not _MACHINE.fullmatch(source_machine):
            raise ValueError("请输入已登记的机器 ID（m_…）。")
        key = stored(source_machine)
    if not key:
        key = ui.password()
        if key is None:
            return False
    key = key.strip()
    if not key or not key.isascii() or any(not 33 <= ord(c) <= 126 for c in key):
        raise ValueError("请输入有效的 InstaBox API 密钥。")

    machine = _select_machine(ui, key, list_page)
    if machine is None:
        return False
    project = project_hint if machine == machine_hint and key not in project_hint else ""
    prompt = "输入云机上已有项目的绝对目录，例如 /data/my-project。"
    while True:
        project = ui.entry("输入云项目目录", prompt, project)
        if project is None:
            return False
        # A key pasted into the directory field must never reach argv, settings or a guest.
        if key in project:
            project = ""
            prompt = "请在此处输入项目目录。"
            continue
        try:
            _, project = validate(machine, project)
        except (TypeError, ValueError):
            prompt = "请输入已有项目的绝对目录，例如 /data/my-project。"
            continue
        try:
            check_tree(key, machine, project)
            break
        except BridgeError as exc:
            if exc.code in {"not_found", "invalid_path"}:
                prompt = "该目录不存在或无法读取，请核对云机上的绝对目录。"
                continue
        except Exception:
            pass
        raise ValueError("无法读取项目目录。请检查云机是否运行、项目读取权限及密钥的命令执行权限。") from None

    old = stored(machine)
    if old == key:
        action = "继续使用此机器已保存在 Ubuntu 密钥环（Secret Service）的密钥，并将此目录设为本机项目。"
    elif old:
        action = "将此目录设为本机项目，并用本次密钥替换 Ubuntu 密钥环（Secret Service）中此机器的密钥。"
    else:
        action = "将此目录设为本机项目，并将 API 密钥保存到当前用户的 Ubuntu 密钥环（Secret Service）。"
    if not ui.confirm(machine, project, action):
        return False

    if stored(machine) != old:
        raise ValueError("此机器的密钥已被更改，请重新打开项目设置。")
    if old != key:
        try:
            vault.set_password(VAULT_SERVICE, machine, key)
        except Exception:
            raise ValueError("未能确认此机器的密钥已保存。请解锁或检查 Ubuntu 密钥环，然后重新打开设置。") from None
    try:
        integrate(machine, project)
    except Exception:
        if old != key:
            try:
                if stored(machine) == key:
                    if old:
                        vault.set_password(VAULT_SERVICE, machine, old)
                    else:
                        vault.delete_password(VAULT_SERVICE, machine)
            except Exception:
                raise ValueError("项目登记失败。请在 Ubuntu 密钥环中检查此机器的条目，然后重新打开设置。") from None
        raise ValueError("本机项目登记失败。请检查配置目录及桌面入口的访问权限，然后重新打开设置。") from None
    return True
