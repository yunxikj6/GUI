"""Read durable task output across bounded display windows in Chromium."""
from contextlib import closing
import json
import sqlite3
import sys
from urllib.request import Request, urlopen

from test_browser import BrowserFixture, expect


class TaskOutputAcceptance(BrowserFixture):
    rows = [f"检查记录 {number:04d}" for number in range(1, 527)]
    pieces = [f"[{n:03d}]" + "🌱" * 32 + "x" * 4096 + f"[/{n:03d}]\n" for n in range(96)]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        initialize = Request(cls.origin + "/api/rpc", data=b'{"op":"tasks"}', headers={"Content-Type": "application/json"})
        with urlopen(initialize, timeout=5) as response:
            if not json.load(response)["ok"]:
                raise RuntimeError("Could not initialize output fixture")
        database, = (cls.project.parent / "state").rglob("tasks.sqlite")
        with closing(sqlite3.connect(database)) as db, db:
            for task, kind, entries in (("streamed", "delta", cls.pieces), ("rows", "output", cls.rows),
                                        ("short", "output", ["另一任务的独立记录"])):
                db.execute("""INSERT INTO task (id, requestId, provider, prompt, status, createdAt)
                              VALUES (?, ?, 'check', ?, 'succeeded', '2025-05-04T12:00:00Z')""", (task, task, task))
                db.executemany("INSERT INTO event (taskId, seq, kind, text) VALUES (?, ?, ?, ?)",
                               ((task, n, kind, text) for n, text in enumerate(entries, 1)))

        # A real detached worker records CLI events. The test controls when its final output arrives.
        cls.advance = cls.codex_binary.with_name("advance-output")
        cls.release = cls.codex_binary.with_name("finish-output")
        cls.codex_binary.write_text(f"#!{sys.executable}\n" + """
import json, sys, time
from pathlib import Path
if sys.argv[1:3] == ["mcp", "list"]:
    print("[]")
    sys.exit(0)
def say(text):
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}), flush=True)
for index in range(526):
    say(f"运行输出 {index:04d}")
for name, text in (("advance-output", "阅读旧页期间新增的结果"), ("finish-output", "长任务的最终结果")):
    gate = Path(__file__).with_name(name)
    deadline = time.monotonic() + 45
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    if not gate.exists():
        sys.exit(1)
    say(text)
print(json.dumps({"type": "turn.completed"}))
""", encoding="utf-8")

    def select(self, task, marker):
        self.page.locator("#taskHistory").select_option(task)
        expect(self.page.locator("#eventLog")).to_contain_text(marker, timeout=12000)

    def test_narrow_output_paging_controls(self):
        page = self.page
        for width in (320, 360):
            with self.subTest(width=width):
                page.set_viewport_size({"width":width, "height":720})
                page.locator("#mobileTasks").click()
                self.select("rows", self.rows[-1])
                page.get_by_role("button", name="较早输出").click()
                expect(page.locator("#outputPosition")).to_have_text("历史输出")
                for name in ("较早输出", "较新输出", "回到最新输出"):
                    button = page.get_by_role("button", name=name)
                    expect(button).to_be_visible()
                    rect = button.bounding_box()
                    self.assertGreaterEqual(rect["width"], 44)
                    self.assertGreaterEqual(rect["height"], 44)
                    self.assertGreaterEqual(rect["x"], 0)
                    self.assertLessEqual(rect["x"] + rect["width"], width)
                log = page.locator("#eventLog")
                self.assertTrue(log.evaluate("node => node.clientHeight > 0 && node.scrollHeight > node.clientHeight"))
                log.hover()
                log.evaluate("""node => {
                    delete node.dataset.fixtureScrolled;
                    node.addEventListener('scroll', () => {
                        node.dataset.fixtureScrolled = String(node.scrollTop > 0);
                    }, {once:true});
                }""")
                page.mouse.wheel(0, 180)
                expect(log).to_have_attribute("data-fixture-scrolled", "true")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                page.get_by_role("button", name="回到最新输出").click()
                expect(log.locator(".event").last).to_have_text(self.rows[-1])

        # The mobile file pane is shown first after a reload. A tail received
        # while the task pane is hidden can scroll only after that pane is opened.
        page.locator("#mobileFiles").click()
        page.reload()
        expect(page.locator("#eventLog")).to_contain_text(self.rows[-1])
        page.locator("#mobileTasks").click()
        page.wait_for_function("""() => {
            const log = document.getElementById('eventLog');
            return log.clientHeight > 0 && log.scrollHeight - log.scrollTop - log.clientHeight < 2;
        }""")

    def test_read_every_row_in_both_directions_and_restore_reading_page(self):
        page, log = self.page, self.page.locator("#eventLog .event")
        self.select("rows", self.rows[-1])
        end = len(self.rows)
        while True:
            visible = log.all_text_contents()
            start = end - len(visible)
            self.assertTrue(0 <= start < end)
            self.assertEqual(visible, self.rows[start:end])
            if start == 0:
                break
            page.locator("#olderOutput").click()
            expect(log.last).to_have_text(self.rows[start - 1])
            end = start
        expect(page.locator("#olderOutput")).to_be_disabled()
        self.assertLess(end, len(self.rows))
        page.reload(wait_until="domcontentloaded")
        expect(log.first).to_have_text(self.rows[0])
        self.assertEqual(log.all_text_contents(), self.rows[:end])

        while end < len(self.rows):
            page.locator("#newerOutput").click()
            expect(log.first).to_have_text(self.rows[end])
            visible = log.all_text_contents()
            self.assertGreater(len(visible), 0)
            self.assertEqual(visible, self.rows[end:end + len(visible)])
            end += len(visible)
        page.locator("#olderOutput").click()
        expect(page.locator("#latestOutput")).to_be_enabled()
        page.locator("#latestOutput").click()
        expect(log.last).to_have_text(self.rows[-1])

    def test_merged_deltas_reconstruct_original_text_with_bounded_dom(self):
        page = self.page
        log = page.locator("#eventLog")
        final_marker = f"[/{len(self.pieces) - 1:03d}]"
        self.select("streamed", final_marker)
        end = len(self.pieces)
        merged = False
        while end:
            text = log.text_content()
            first = int(text[1:4])
            self.assertTrue(0 <= first < end)
            self.assertEqual(text, "".join(self.pieces[first:end]))
            sizes = log.locator(".event").evaluate_all("nodes => nodes.map(node => node.textContent.length)")
            self.assertLessEqual(sum(sizes), 180000)
            self.assertLessEqual(max(sizes), 30000)
            merged = merged or len(sizes) < end - first
            end = first
            if end:
                page.locator("#olderOutput").click()
                expect(log).to_contain_text(f"[/{end - 1:03d}]")
        self.assertTrue(merged)
        page.locator("#latestOutput").click()
        expect(log).to_contain_text(final_marker)

    def start_running_output(self):
        page, log = self.page, self.page.locator("#eventLog")
        for gate in (self.advance, self.release):
            gate.unlink(missing_ok=True)
            self.addCleanup(gate.touch)
        page.locator("#provider").select_option("codex")
        page.locator("#prompt").fill("记录运行输出")
        with page.expect_response(lambda r: (r.request.post_data_json or {}).get("op") == "start") as accepted:
            page.locator("#runAgent").click()
        expect(page.locator("#taskHistory")).to_have_value(accepted.value.json()["data"]["id"])
        expect(page.locator("#taskStatus")).to_have_text("运行中")
        expect(log).to_contain_text("运行输出 0525", timeout=18000)
        return log

    @staticmethod
    def background_append(response):
        if (response.request.post_data_json or {}).get("op") != "events" or not response.ok:
            return False
        return any(e["text"] == "阅读旧页期间新增的结果" for e in response.json()["data"]["events"])

    def test_archived_running_output_keeps_selection_and_scroll_when_task_finishes(self):
        page, log = self.page, self.start_running_output()
        page.locator("#olderOutput").click()
        expect(page.locator("#outputPosition")).to_contain_text("历史")
        visible = log.locator(".event").all_text_contents()
        offset = log.evaluate("""node => {
            const event = node.children[Math.floor(node.children.length / 2)];
            event.scrollIntoView({block:'center'});
            const range = document.createRange();
            range.selectNodeContents(event);
            getSelection().removeAllRanges();
            getSelection().addRange(range);
            return [node.scrollTop, getSelection().toString()];
        }""")
        self.assertGreater(offset[0], 0)
        self.assertTrue(offset[1])
        def assert_reading_position():
            self.assertEqual(log.locator(".event").all_text_contents(), visible)
            self.assertEqual(log.evaluate("node => [node.scrollTop, getSelection().toString()]"), offset)

        with page.expect_response(self.background_append, timeout=12000):
            self.advance.touch()
        expect(page.locator("#taskStatus")).to_have_text("运行中")
        assert_reading_position()
        self.release.touch()
        expect(page.locator("#taskStatus")).to_have_text("已完成", timeout=12000)
        assert_reading_position()
        page.locator("#latestOutput").click()
        expect(log).to_contain_text("阅读旧页期间新增的结果")
        expect(log).to_contain_text("长任务的最终结果")

    def test_scrolling_live_tail_keeps_selected_text_in_place_during_rollover(self):
        page, log = self.page, self.start_running_output()
        expect(log.locator(".event")).to_have_count(160)
        # Scroll up within the latest window, without touching any paging control.
        log.hover()
        log.evaluate("""node => node.addEventListener('scrollend', () => {
            node.dataset.fixtureScrollEnded = 'yes';
        }, {once:true})""")
        page.mouse.wheel(0, -900)
        expect(log).to_have_attribute("data-fixture-scroll-ended", "yes")
        self.assertGreater(log.evaluate("node => node.scrollHeight - node.scrollTop - node.clientHeight"), 150)
        sentence = log.evaluate("""log => {
            const bounds = log.getBoundingClientRect();
            return Array.from(log.children).find(el => {
                const box = el.getBoundingClientRect();
                return box.top >= bounds.top + 12 && box.bottom < bounds.bottom;
            }).textContent;
        }""")
        log.get_by_text(sentence, exact=True).click(click_count=3, position={"x": 8, "y": 10})

        def visible_selection():
            return log.evaluate("""log => {
                const selection = getSelection();
                if (!selection.rangeCount || !log.contains(selection.anchorNode)) return ['', null];
                return [selection.toString(), selection.getRangeAt(0).getBoundingClientRect().top - log.getBoundingClientRect().top];
            }""")

        chosen, initial_top = visible_selection()
        self.assertEqual(chosen.strip(), sentence)

        def assert_visible_selection():
            text, top = visible_selection()
            self.assertEqual(text, chosen)
            self.assertAlmostEqual(top, initial_top, delta=2)

        with page.expect_response(self.background_append, timeout=12000):
            self.advance.touch()
        expect(page.locator("#latestOutput")).to_be_visible()
        assert_visible_selection()
        self.release.touch()
        expect(page.locator("#taskStatus")).to_have_text("已完成", timeout=12000)
        assert_visible_selection()
        page.locator("#latestOutput").click()
        expect(log).to_contain_text("长任务的最终结果")

    def test_retry_uses_same_boundary_and_late_reply_cannot_replace_another_task(self):
        page, log = self.page, self.page.locator("#eventLog .event")
        self.select("rows", self.rows[-1])
        before = log.all_text_contents()
        requests, held = [], []

        def intercept(route):
            payload = route.request.post_data_json or {}
            if payload.get("op") == "events" and "before" in payload:
                requests.append(payload)
                if len(requests) == 1:
                    route.abort("internetdisconnected")
                    return
                if len(requests) == 3:
                    held.append(route)
                    return
            route.continue_()

        page.route("**/api/rpc", intercept)
        page.locator("#olderOutput").click()
        expect(page.locator("#outputNotice")).to_contain_text("连接")
        self.assertEqual(log.all_text_contents(), before)
        page.locator("#retryOutput").click()
        expect(log.last).to_have_text(self.rows[self.rows.index(before[0]) - 1])
        self.assertEqual(requests[0], requests[1])
        with page.expect_request(lambda req: "before" in (req.post_data_json or {})):
            page.locator("#olderOutput").click()
        self.select("short", "另一任务的独立记录")
        with page.expect_response(lambda res: "before" in (res.request.post_data_json or {})) as stale:
            self.assertEqual(len(held), 1)
            held.pop().continue_()
        self.assertTrue(stale.value.json()["ok"])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(log).to_have_text(["另一任务的独立记录"])
        expect(page.locator("#taskHistory")).to_have_value("short")

    def test_original_events_response_still_reaches_end(self):
        page, requests = self.page, []

        def original_events(route):
            payload = route.request.post_data_json or {}
            if payload.get("op") == "events" and payload.get("taskId") == "rows":
                requests.append(payload)
                query = {"op": "events", "taskId": "rows", "after": payload.get("after", 0)}
                envelope = route.fetch(post_data=json.dumps(query)).json()
                envelope["data"].pop("hasEarlier", None)
                route.fulfill(json=envelope)
            else:
                route.continue_()

        page.route("**/api/rpc", original_events)
        self.select("rows", self.rows[-1])
        log = page.locator("#eventLog .event")
        expect(page.locator("#outputPosition")).to_have_text("兼容模式")
        self.assertEqual(log.all_text_contents(), self.rows[-log.count():])
        self.assertLessEqual(log.count(), 400)
        self.assertTrue(any(request.get("tail") for request in requests))
        self.assertFalse(any("before" in request for request in requests))

    def test_late_running_tail_does_not_hide_the_final_polled_output(self):
        page, log = self.page, self.start_running_output()
        page.locator("#olderOutput").click()
        expect(page.locator("#outputPosition")).to_have_text("历史输出")
        held = []

        def hold_latest(route):
            query = route.request.post_data_json or {}
            if query.get("op") == "events" and query.get("tail") and not held:
                # Capture the actual running snapshot before the CLI can emit the final result.
                held.append((route, route.fetch()))
                self.advance.touch()
                self.release.touch()
            else:
                route.continue_()

        page.route("**/api/rpc", hold_latest)
        page.locator("#latestOutput").click()
        expect(page.locator("#taskStatus")).to_have_text("已完成", timeout=12000)
        self.assertEqual(len(held), 1)
        route, response = held[0]
        snapshot = response.json()["data"]
        self.assertEqual(snapshot["task"]["status"], "running")
        self.assertNotIn("长任务的最终结果", [event["text"] for event in snapshot["events"]])
        route.fulfill(response=response)
        expect(page.locator("#outputPosition")).to_have_text("最新输出")
        expect(log).to_contain_text("长任务的最终结果", timeout=7000)
        expect(page.locator("#taskStatus")).to_have_text("已完成")
