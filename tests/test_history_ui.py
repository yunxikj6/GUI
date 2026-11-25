"""Browse archived task history through the launcher's actual RPC and Chromium UI."""
from contextlib import closing
import json
import sqlite3
from urllib.request import Request, urlopen
import uuid

from test_browser import BrowserFixture, expect


class TaskHistoryAcceptance(BrowserFixture):
    count = 163
    stamp = "2019-05-18T12:00:00.000+00:00"

    @staticmethod
    def record(index):
        return f"archive-{index:03d}"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Let the real worker create its current schema in this fixture's temporary project.
        request = Request(cls.origin + "/api/rpc", data=b'{"op":"tasks"}', headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            if not json.load(response)["ok"]:
                raise RuntimeError("Could not initialize the isolated task database")
        cls.database, = (cls.project.parent / "state").rglob("tasks.sqlite")
        cls.snapshot = sqlite3.connect(":memory:")
        cls.addClassCleanup(cls.snapshot.close)
        cls.thread = str(uuid.uuid4())
        with closing(sqlite3.connect(cls.database)) as db, db:
            for index in range(1, cls.count + 1):
                ident = cls.record(index)
                db.execute("""INSERT INTO task (id, requestId, provider, prompt, status, createdAt, finishedAt)
                              VALUES (?, ?, ?, ?, 'succeeded', ?, ?)""",
                           (ident, ident, "codex" if index == 1 else "check", f"历史任务 {index}", cls.stamp, cls.stamp))
                db.execute("INSERT INTO event (taskId, seq, kind, text) VALUES (?, 1, 'output', ?)",
                           (ident, f"第 {index} 份检查结果"))
            db.execute("INSERT INTO codex_turn (taskId, threadId) VALUES (?, ?)", (cls.record(1), cls.thread))
            db.commit()
            db.backup(cls.snapshot)

    def setUp(self):
        with closing(sqlite3.connect(self.database)) as db:
            self.snapshot.backup(db)
        self.codex_binary.with_name("turn-" + self.thread).write_text("历史任务 1", encoding="utf-8")
        super().setUp()

    def test_browse_tied_records_and_restore_old_turn_across_pages(self):
        page = self.page
        history, position = page.locator("#taskHistory"), page.locator("#historyPage")
        newest, oldest = self.record(self.count), self.record(1)
        expect(history).to_have_value(newest)
        seen = []
        for number in (1, 2, 3):
            if number > 1:
                page.locator("#olderTasks").click()
            expect(position).to_have_text(f"第 {number} 页")
            expect(history).to_have_value(newest)
            expect(page.locator("#eventLog")).to_have_text(f"第 {self.count} 份检查结果")
            ids = history.locator("option").evaluate_all("options => options.map(option => option.value)")
            if number > 1:
                ids.remove(newest)  # It stays selectable while browsing pages that do not contain it.
            self.assertLessEqual(len(ids), 80)
            seen.extend(ids)
        self.assertEqual(seen, [self.record(i) for i in range(self.count, 0, -1)])

        history.select_option(oldest)
        expect(page.locator("#eventLog")).to_have_text("第 1 份检查结果")
        year, month, day = page.evaluate("""ts => {
            const d = new Date(ts); return [d.getFullYear(), d.getMonth()+1, d.getDate()];
        }""", self.stamp)
        label = history.locator("option:checked").inner_text()
        self.assertIn(str(year), label)
        self.assertRegex(label, rf"(?<!\d)0?{month}\D+0?{day}(?!\d)")
        page.locator("#followUp summary").click()
        draft = "解释第一份检查结果"
        page.locator("#replyPrompt").fill(draft)
        page.locator("#newerTasks").click()
        expect(position).to_have_text("第 2 页")
        expect(history).to_have_value(oldest)
        expect(page.locator("#replyPrompt")).to_have_value(draft)
        page.reload(wait_until="domcontentloaded")
        expect(position).to_have_text("第 2 页")
        expect(history).to_have_value(oldest)
        expect(page.locator("#eventLog")).to_have_text("第 1 份检查结果")
        expect(page.locator("#replyPrompt")).to_have_value(draft)

        with page.expect_request(lambda req: (req.post_data_json or {}).get("op") == "start") as submitted:
            page.locator("#sendReply").click()
        payload = submitted.value.post_data_json
        self.assertEqual((payload["provider"], payload["parentTaskId"], payload["prompt"]), ("codex", oldest, draft))
        expect(page.locator("#eventLog")).to_contain_text("历史任务 1 → " + draft)
        expect(page.locator("#taskStatus")).to_have_text("已完成")
        page.locator("#previousTurn").click()
        expect(history).to_have_value(oldest)
        expect(page.locator("#eventLog")).to_have_text("第 1 份检查结果")

    def test_failed_page_keeps_visible_history_and_retries_the_same_cursor(self):
        page = self.page
        page.set_viewport_size({"width": 320, "height": 720})
        page.locator("#mobileTasks").click()
        history, position = page.locator("#taskHistory"), page.locator("#historyPage")
        expect(history).to_have_value(self.record(self.count))
        page.locator("#olderTasks").click()
        expect(position).to_have_text("第 2 页")
        chosen = self.record(40)
        history.select_option(chosen)
        expect(page.locator("#eventLog")).to_have_text("第 40 份检查结果")
        before = history.locator("option").all_text_contents()
        requests = []

        def drop_first(route):
            payload = route.request.post_data_json or {}
            if payload.get("op") == "tasks":
                requests.append(payload)
                if len(requests) == 1:
                    route.abort("internetdisconnected")
                    return
            route.continue_()

        page.route("**/api/rpc", drop_first)
        page.locator("#olderTasks").click()
        expect(page.locator("#historyNotice")).to_contain_text("连接")
        expect(position).to_have_text("第 2 页")
        expect(history).to_have_value(chosen)
        self.assertEqual(history.locator("option").all_text_contents(), before)
        expect(page.locator("#eventLog")).to_have_text("第 40 份检查结果")
        page.locator("#olderTasks").click()
        expect(position).to_have_text("第 3 页")
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        for button in page.locator("#historyNav button").all():
            box = button.bounding_box()
            self.assertGreaterEqual(min(box["width"], box["height"]), 44)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]["beforeTaskId"], self.record(4))
        expect(history).to_have_value(chosen)
        expect(history.locator(f'option[value="{self.record(1)}"]')).to_have_count(1)
        page.locator("#latestTasks").click()
        expect(position).to_have_text("第 1 页")
        expect(history).to_have_value(chosen)
        expect(history.locator(f'option[value="{self.record(self.count)}"]')).to_have_count(1)

    def test_deleted_anchor_and_legacy_server_return_to_newest_without_losing_draft(self):
        page = self.page
        history, position = page.locator("#taskHistory"), page.locator("#historyPage")
        oldest = self.record(1)
        newest_option = history.locator(f'option[value="{self.record(self.count)}"]')
        for number in (2, 3):
            page.locator("#olderTasks").click()
            expect(position).to_have_text(f"第 {number} 页")
        history.select_option(oldest)
        page.locator("#followUp summary").click()
        draft = "保留这个归档任务的追问"
        page.locator("#replyPrompt").fill(draft)

        def expect_newest_with_draft():
            expect(newest_option).to_have_count(1)
            expect(position).to_have_text("第 1 页")
            expect(history).to_have_value(oldest)
            expect(page.locator("#eventLog")).to_have_text("第 1 份检查结果")
            expect(page.locator("#replyPrompt")).to_have_value(draft)

        # The selected turn survives deletion of the record used to locate its page.
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM task WHERE id=?", (self.record(4),))
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#historyNotice")).to_contain_text("已返回最近记录")
        expect_newest_with_draft()

        page.locator("#olderTasks").click()
        expect(position).to_have_text("第 2 页")
        anchors = []

        def legacy_server(route):
            payload = route.request.post_data_json or {}
            if payload.get("op") != "tasks":
                route.continue_()
                return
            anchors.append(payload.get("beforeTaskId"))
            # Old launchers ignore an unknown cursor and return only the recent summaries.
            envelope = route.fetch(post_data=json.dumps({"op": "tasks"})).json()
            del envelope["data"]["nextBeforeTaskId"]
            route.fulfill(json=envelope)

        page.route("**/api/rpc", legacy_server)
        for expected_anchors in ([self.record(84)], [self.record(84), None]):
            page.reload(wait_until="domcontentloaded")
            expect_newest_with_draft()
            expect(page.locator("#historyNav")).to_be_hidden()
            self.assertEqual(anchors, expected_anchors)

    def test_refreshing_current_page_supersedes_in_flight_older_page(self):
        page, held = self.page, []
        history = page.locator("#taskHistory")
        selected = self.record(self.count)
        expect(history).to_have_value(selected)

        def older_request(request):
            return (request.post_data_json or {}).get("beforeTaskId") == self.record(84)

        def hold_older(route):
            if older_request(route.request):
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", hold_older)
        with page.expect_request(older_request):
            page.locator("#olderTasks").click()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                          VALUES ('new-head','new-head','check','','succeeded',?)""", (self.stamp,))
        page.locator("#reloadTasks").click()
        head = history.locator('option[value="new-head"]')
        expect(head).to_have_count(1)
        self.assertEqual(len(held), 1)
        with page.expect_response(lambda response: older_request(response.request)) as returned:
            held.pop().continue_()
        self.assertTrue(returned.value.json()["ok"])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(page.locator("#historyPage")).to_have_text("第 1 页")
        expect(head).to_have_count(1)
        expect(history).to_have_value(selected)
        expect(page.locator("#eventLog")).to_have_text(f"第 {self.count} 份检查结果")

    def test_paging_before_start_receipt_keeps_current_task(self):
        page = self.page
        history, position = page.locator("#taskHistory"), page.locator("#historyPage")
        original = self.record(self.count)
        expect(history).to_have_value(original)
        page.locator("#prompt").fill("浏览历史时保留这份草稿")
        held = []

        def hold_start(route):
            if (route.request.post_data_json or {}).get("op") == "start":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", hold_start)
        with page.expect_request(lambda req: (req.post_data_json or {}).get("op") == "start"):
            page.locator("#runCheck").click()
        request = held.pop()
        receipt = request.fetch()
        created = receipt.json()["data"]["id"]
        page.locator("#olderTasks").click()
        expect(position).to_have_text("第 2 页")
        expect(history).to_have_value(original)
        request.fulfill(response=receipt)
        expect(page.locator("#startNotice")).to_be_empty()
        expect(history).to_have_value(original)
        expect(page.locator("#eventLog")).to_have_text(f"第 {self.count} 份检查结果")
        expect(page.locator("#prompt")).to_have_value("浏览历史时保留这份草稿")
        page.locator("#newerTasks").click()
        expect(position).to_have_text("第 1 页")
        history.select_option(created)
        expect(page.locator("#eventLog")).to_contain_text("项目检查通过。")
