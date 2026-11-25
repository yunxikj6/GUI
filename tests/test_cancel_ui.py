"""Drive cancellation through the browser, launcher, SQLite and a running test CLI."""
import sys

from test_browser import BrowserFixture, expect


class CancellationAcceptance(BrowserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.codex_binary.write_text(f"#!{sys.executable}\n" + """
import json, sys, time, uuid
from pathlib import Path
if sys.argv[1:3] == ["mcp", "list"]:
    print("[]")
    sys.exit(0)
prompt = sys.stdin.read().strip()
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type":"thread.started", "thread_id":str(uuid.uuid4())})
emit({"type":"item.completed", "item":{"type":"agent_message", "text":"开始处理 " + prompt}})
if prompt.startswith("等待"):
    gate = Path(__file__).parent / prompt
    until = time.monotonic() + 30
    while not gate.exists() and time.monotonic() < until:
        time.sleep(.05)
emit({"type":"item.completed", "item":{"type":"agent_message", "text":"正常结束 " + prompt}})
emit({"type":"turn.completed"})
""", encoding="utf-8")

    def start(self, prompt):
        page = self.page
        self.addCleanup((self.codex_binary.parent / prompt).touch)
        page.locator("#provider").select_option("codex")
        page.locator("#prompt").fill(prompt)
        page.locator("#runAgent").click()
        expect(page.locator("#eventLog")).to_contain_text("开始处理 " + prompt)
        return page.locator("#taskHistory").input_value()

    def test_lost_receipt_retries_and_refreshes_existing_output(self):
        page = self.page
        task = self.start("等待重试")
        requests = []
        def drop_cancellation(route):
            body = route.request.post_data_json or {}
            if body.get("op") != "cancel":
                route.continue_()
                return
            requests.append(body)
            # First disconnect before delivery, then lose the receipt after the server commits.
            if len(requests) == 2:
                received = route.fetch().json()
                self.assertTrue(received["ok"], received)
            route.abort("failed")

        page.route("**/api/rpc", drop_cancellation)
        cancel = page.get_by_role("button", name="取消任务")
        cancel.click()
        expect(page.locator("#cancelNotice")).to_contain_text("取消结果尚未确认")
        expect(page.locator("#eventLog")).to_contain_text("开始处理 等待重试")
        with page.expect_event("requestfailed", predicate=lambda req: (req.post_data_json or {}).get("op") == "cancel"):
            cancel.evaluate("button => { button.click(); button.click(); }")
        self.assertEqual(requests, [{"op":"cancel", "taskId":task}] * 2)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#taskHistory")).to_have_value(task)
        expect(page.locator("#taskStatus")).to_have_text("已取消", timeout=15000)
        expect(page.locator("#cancelTask")).to_be_hidden()
        response = self.context.request.post(self.origin + "/api/rpc", data={
            "op":"events", "taskId":task, "after":0,
        }).json()["data"]
        self.assertEqual(response["task"]["status"], "cancelled")
        expected = [event["text"] for event in response["events"]]
        expect(page.locator("#eventLog .event")).to_have_text(expected)
        self.assertNotIn("正常结束 等待重试", expected)
        self.start("之后的任务")
        expect(page.locator("#taskStatus")).to_have_text("已完成")

    def test_switch_tasks_before_cancel_response_and_finish_race(self):
        page = self.page
        page.set_viewport_size({"width":390, "height":844})
        page.locator("#mobileTasks").click()
        history = page.locator("#taskHistory")
        log = page.locator("#eventLog")
        first = self.start("等待取消")
        second = self.start("等待完成")
        history.select_option(first)
        expect(log).to_contain_text("开始处理 等待取消")
        held = []
        snapshots = []
        def hold_cancel(route):
            op = (route.request.post_data_json or {}).get("op")
            if op == "cancel":
                held.append(route)
            elif op == "tasks":
                snapshots.append(route)
            else:
                route.continue_()
        page.route("**/api/rpc", hold_cancel)
        button = page.locator("#cancelTask")
        button.evaluate("button => { button.click(); button.click(); }")
        expect(button).to_be_disabled()
        self.assertEqual(len(held), 1)
        history.select_option(second)
        expect(button).to_be_enabled()
        first_route = held.pop()
        first_response = first_route.fetch()
        self.assertTrue(first_response.json()["ok"])
        # Delay the first receipt until another task is selected.
        # The selected task must still accept its own cancellation.
        with page.expect_response(lambda res: res.request.post_data_json and res.request.post_data_json.get("op") == "cancel"):
            first_route.fulfill(response=first_response)
        expect(history).to_have_value(second)
        expect(button).to_be_enabled()
        expect(log).to_contain_text("开始处理 等待完成")
        history.select_option(first)
        expect(page.locator("#taskStatus")).to_have_text("已取消", timeout=15000)

        history.select_option(second)
        button.click()
        expect(button).to_be_disabled()
        self.assertEqual(len(held), 1)
        page.locator("#reloadTasks").click()
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots.pop()
        stale = snapshot.fetch()
        # This worker finishes before the server receives the cancellation.
        (self.codex_binary.parent / "等待完成").touch()
        expect(page.locator("#taskStatus")).to_have_text("已完成")
        with page.expect_response(lambda res: (res.request.post_data_json or {}).get("op") == "tasks"):
            snapshot.fulfill(response=stale)
        expect(button).to_be_hidden()
        last = held.pop()
        result = last.fetch()
        self.assertEqual(result.json()["data"]["status"], "succeeded")
        last.fulfill(response=result)
        expect(page.locator("#taskStatus")).to_have_text("已完成")
        expect(log).to_contain_text("正常结束 等待完成")
        expect(button).to_be_hidden()
