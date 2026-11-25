"""Exercise native-provider turn selection and receipt recovery in the browser."""
from contextlib import closing
from hashlib import sha256
import json
import os
import sqlite3
import sys
import uuid

from test_browser import BrowserFixture, expect


class ClaudeConversationAcceptance(BrowserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        claude = cls.codex_binary.with_name("claude")
        claude.write_text(f"#!{sys.executable}\n" + """
import json, sys, time, uuid
from pathlib import Path
args = sys.argv[1:]
assert "--no-session-persistence" not in args
assert args[args.index("--permission-mode") + 1] == "dontAsk"
assert set(args[args.index("--tools") + 1].split(",")) == {"Read", "Glob", "Grep"}
option = "--resume" if "--resume" in args else "--session-id"
session = str(uuid.UUID(args[args.index(option) + 1]))
path = Path(__file__).parent / ("claude-turns-" + session)
turns = json.loads(path.read_text()) if option == "--resume" else []
turns.append(sys.stdin.read().strip())
path.write_text(json.dumps(turns))
answer = " → ".join(turns)
print(json.dumps({"type":"system", "subtype":"init", "session_id":session}))
tool = {"type":"tool_use", "id":"toolu_" + uuid.uuid4().hex, "name":"Read"}
print(json.dumps({"type":"stream_event", "session_id":session, "event":{
    "type":"content_block_start", "index":0, "content_block":{**tool, "input":{}},
}}), flush=True)
if option == "--session-id":
    until = time.monotonic() + 15
    while not (path.parent / "finish-claude-read").exists() and time.monotonic() < until:
        time.sleep(.05)
print(json.dumps({"type":"assistant", "session_id":session, "message":{"content":[
    {**tool, "input":{"file_path":"/workspace/private-ui-fixture-path"}},
]}}))
print(json.dumps({"type":"assistant", "session_id":session,
                  "message":{"content":[{"type":"text", "text":answer}]}}))
print(json.dumps({"type":"result", "subtype":"success", "is_error":False,
                  "session_id":session, "result":answer}))
""", encoding="utf-8")
        claude.chmod(0o700)

        # Open records from before Claude persistence through the current launcher's own DB migration.
        directory = cls.project.parent / "state" / "cloudwork" / sha256(os.fsencode(cls.project.resolve())).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        thread = str(uuid.uuid4())
        with closing(sqlite3.connect(directory / "tasks.sqlite")) as db, db:
            db.executescript("""
                CREATE TABLE task (id TEXT PRIMARY KEY, requestId TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
                    prompt TEXT NOT NULL, status TEXT NOT NULL, createdAt TEXT NOT NULL,
                    finishedAt TEXT, pid INTEGER, processKey TEXT);
                CREATE TABLE codex_turn (taskId TEXT PRIMARY KEY, parentTaskId TEXT UNIQUE, threadId TEXT);
                CREATE VIEW task_history AS
                    SELECT t.*, t.rowid AS taskRowId, turn.parentTaskId, child.taskId AS nextTaskId,
                           (t.status='succeeded' AND turn.threadId IS NOT NULL AND child.taskId IS NULL) AS canContinue
                    FROM task t LEFT JOIN codex_turn turn ON turn.taskId=t.id
                                LEFT JOIN codex_turn child ON child.parentTaskId=t.id;
            """)
            for task in ("codex-with-thread", "codex-without-thread"):
                db.execute("""INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                    VALUES (?, ?, 'codex', '旧对话', 'succeeded', '2025-01-01T00:00:00Z')""", (task, task))
            db.execute("INSERT INTO codex_turn (taskId, threadId) VALUES ('codex-with-thread', ?)", (thread,))
        cls.codex_binary.with_name("turn-" + thread).write_text("旧对话", encoding="utf-8")

    def test_two_tabs_submit_same_parent_and_winner_recovers_its_receipt(self):
        page = self.page
        page.locator("#provider").select_option("claude")
        page.locator("#prompt").fill("审阅入口")
        finish_read = self.codex_binary.with_name("finish-claude-read")
        self.addCleanup(finish_read.touch)
        page.locator("#runAgent").click()
        progress = page.locator("#eventLog .event").filter(has_text="Read")
        read_status = ["Claude 正在使用 Read 读取文件。"]
        expect(progress).to_have_text(read_status)
        expect(progress).to_have_attribute("data-kind", "status")
        expect(page.locator("#taskStatus")).to_have_text("运行中")
        finish_read.touch()
        expect(page.locator("#eventLog")).to_contain_text("审阅入口")
        expect(progress).to_have_text(read_status)
        page.locator("#followUp summary").click()
        parent = page.locator("#taskHistory").input_value()
        history = self.context.request.post(self.origin + "/api/rpc", data={
            "op":"events", "taskId":parent, "after":0,
        }).json()["data"]
        self.assertNotIn("private-ui-fixture-path", json.dumps(history))
        page.locator("#replyPrompt").fill("说明输入格式")

        other = self.context.new_page()
        other.goto(self.origin, wait_until="domcontentloaded")
        expect(other.locator("#taskHistory")).to_have_value(parent)
        other.locator("#followUp summary").click()
        other.locator("#replyPrompt").fill("另一页的追问")
        page.locator("#provider").select_option("codex")
        page.locator("#prompt").fill("另一项 Codex 任务的草稿")
        held = []
        def hold_follow_up(route):
            if (route.request.post_data_json or {}).get("parentTaskId") == parent:
                held.append(route)
            else:
                route.continue_()
        for tab in (page, other):
            tab.route("**/api/rpc", hold_follow_up)
            with tab.expect_request(lambda req: (req.post_data_json or {}).get("parentTaskId") == parent):
                tab.locator("#replyPrompt").press("Control+Enter")
        first, stale = held
        request = first.request.post_data_json
        self.assertEqual(request["provider"], "claude")
        self.assertEqual(request["prompt"], "说明输入格式")
        accepted = first.fetch().json()
        self.assertTrue(accepted["ok"], accepted)
        first.abort("failed")
        response = stale.fetch()
        self.assertFalse(response.json()["ok"])
        stale.fulfill(response=response)
        expect(other.locator("#nextTurn")).to_be_visible()
        expect(other.locator("#replyPrompt")).to_have_value("另一页的追问")
        expect(other.locator("#sendReply")).to_be_disabled()
        expect(page.locator("#retryStart")).to_be_visible()
        page.unroute("**/api/rpc", hold_follow_up)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#pendingMessage")).to_contain_text("说明输入格式")
        expect(page.locator("#taskHistory")).to_have_value(parent)
        expect(progress).to_have_text(read_status)
        expect(page.locator("#eventLog")).not_to_contain_text("private-ui-fixture-path")
        with page.expect_response(lambda res: (res.request.post_data_json or {}).get("op") == "start") as retried:
            page.locator("#retryStart").click()
        self.assertEqual(retried.value.request.post_data_json, request)
        child = accepted["data"]["id"]
        self.assertEqual(retried.value.json()["data"]["id"], child)
        expect(page.locator("#eventProvider")).to_have_text("Claude Code")
        expect(page.locator("#eventLog")).to_contain_text("审阅入口 → 说明输入格式")
        expect(progress).to_have_text(read_status)
        expect(page.locator("#prompt")).to_have_value("另一项 Codex 任务的草稿")
        other.locator("#nextTurn").click()
        expect(other.locator("#taskHistory")).to_have_value(child)
        expect(other.locator("#eventLog")).to_contain_text("审阅入口 → 说明输入格式")
        transcripts = list(self.codex_binary.parent.glob("claude-turns-*"))
        self.assertEqual([json.loads(path.read_text()) for path in transcripts], [["审阅入口", "说明输入格式"]])

    def test_persisted_codex_turns_keep_their_original_provider(self):
        page = self.page
        history = page.locator("#taskHistory")
        page.locator("#provider").select_option("claude")
        history.select_option("codex-without-thread")
        expect(page.locator("#taskRequestText")).to_have_text("旧对话")
        expect(page.locator("#followUp")).to_be_hidden()
        history.select_option("codex-with-thread")
        page.locator("#followUp summary").click()
        page.locator("#replyPrompt").fill("继续原任务")
        page.locator("#sendReply").click()
        expect(page.locator("#eventProvider")).to_have_text("Codex")
        expect(page.locator("#eventLog")).to_contain_text("旧对话 → 继续原任务")
