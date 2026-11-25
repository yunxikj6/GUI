"""Read a known Git patch during a connection failure, always within its original workspace."""
import subprocess
import threading

import launcher
from test_browser import BrowserFixture, README_MARKER, expect


class DiffCacheAcceptance(BrowserFixture):
    def setUp(self):
        self.index = self.project / "index-sample.txt"
        self.loose = self.project / "loose-sample.txt"
        self.stage("索引初版\n")
        self.loose.write_text("工作区初版\n", encoding="utf-8")
        super().setUp()

    def stage(self, text):
        self.index.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "--", self.index.name], cwd=self.project, check=True, capture_output=True)

    def layer(self, scope):
        return self.page.locator(f'#preview [data-diff-scope="{scope}"]')

    def test_cold_failure_then_warm_offline_read_and_independent_retries(self):
        page = self.page
        readme = page.locator('[data-file="README.md"]')
        readme.click()
        page.locator("#editFile").click()
        editor = page.locator("#fileEditArea")
        draft = editor.input_value() + "尚未保存的审阅笔记\n"
        editor.fill(draft)
        blocked = {"staged", "unstaged"}
        calls = []

        def connection(route):
            query = route.request.post_data_json
            if query["op"] == "diff":
                calls.append(query["scope"])
                if query["scope"] in blocked:
                    route.abort("internetdisconnected")
                    return
            route.continue_()

        page.route("**/api/rpc", connection)
        diff = page.locator("#showDiff")
        diff.click()
        initial = {"staged": "+索引初版", "unstaged": "+工作区初版"}
        for scope in initial:
            layer = self.layer(scope)
            expect(layer.get_by_role("button", name="重试读取", exact=False)).to_be_visible()
            expect(layer.locator(".diff-lines .diff-line")).to_have_count(0)
            expect(layer).to_contain_text("连接")
            expect(layer).not_to_contain_text("缓存未校验")
        expect(page.locator("#fileNotice")).not_to_contain_text("没有 Git 差异")

        blocked.clear()
        for scope, old in initial.items():
            layer = self.layer(scope)
            layer.get_by_role("button", name="重试读取", exact=False).click()
            expect(layer).to_contain_text(old)
        readme.click()
        expect(editor).to_have_value(draft)
        self.stage("索引新版\n")
        self.loose.write_text("工作区新版\n", encoding="utf-8")
        blocked.update(initial)
        diff.click()
        for scope, old in initial.items():
            expect(self.layer(scope)).to_contain_text("缓存未校验")
            expect(self.layer(scope)).to_contain_text(old)

        for scope, new in [("staged", "+索引新版"), ("unstaged", "+工作区新版")]:
            calls.clear()
            blocked.remove(scope)
            layer = self.layer(scope)
            layer.get_by_role("button", name="重试读取", exact=False).click()
            expect(layer).to_contain_text(new)
            expect(layer).not_to_contain_text(initial[scope])
            expect(layer).not_to_contain_text("缓存未校验")
            self.assertEqual(calls, [scope])
            if scope == "staged":
                expect(self.layer("unstaged")).to_contain_text(initial["unstaged"])
                expect(self.layer("unstaged")).to_contain_text("缓存未校验")
        readme.click()
        expect(editor).to_have_value(draft)
        editor.press("Control+End")
        editor.press("X")
        self.assertEqual(editor.input_value(), draft + "X")
        self.assertNotIn("审阅笔记", (self.project / "README.md").read_text(encoding="utf-8"))

    def test_same_staged_response_preserves_paged_text_selection_and_scroll(self):
        page = self.page
        self.stage("".join(f"review line {i:04d}\n" for i in range(1800)))
        staged = self.layer("staged")
        page.locator("#showDiff").click()
        expect(staged.get_by_text("+review line 0000", exact=True)).to_be_visible()
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")
        page.locator('[data-file="README.md"]').click()
        expect(page.locator("#fileTitle")).to_have_text("README.md")
        held = []

        def gate(route):
            if route.request.post_data_json.get("scope") == "staged":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", gate)
        page.locator("#showDiff").click()
        expect(staged).to_contain_text("正在校验")
        select = staged.locator(".diff-page")
        select.select_option("2")
        text = staged.get_by_text("+review line 1250", exact=True)
        text.scroll_into_view_if_needed()
        text.dblclick()
        node = text.element_handle()
        selection = """node => {
            const s = getSelection();
            return [s.toString(),node.contains(s.anchorNode),node.contains(s.focusNode),
                    s.anchorOffset,s.focusOffset,node.closest('.diff-lines').scrollTop];
        }"""
        before = page.evaluate(selection, node)
        self.assertTrue(before[0].strip())
        self.assertEqual(before[1:3], [True, True])
        self.assertGreater(before[-1], 0)
        self.assertEqual(len(held), 1)
        with page.expect_response(lambda res: res.url.endswith("/api/rpc")
                                  and res.request.post_data_json.get("scope") == "staged") as confirmed:
            held.pop().continue_()
        self.assertIn("+review line 1250", confirmed.value.json()["data"]["diff"])
        expect(staged).not_to_contain_text("正在校验")
        expect(staged).not_to_contain_text("缓存未校验")
        expect(select).to_have_value("2")
        self.assertEqual(page.evaluate(selection, node), before)
        expect(text).to_be_in_viewport()

    def test_same_origin_workspace_switch_rejects_cached_patches_and_late_reply(self):
        first = launcher.LocalWorkspace(self.project)
        second = launcher.LocalWorkspace(self.project.parent / "next" / self.project.name)
        for workspace in (first, second):
            workspace.env["XDG_STATE_HOME"] = str(self.project.parent / "state")
        (second.root / "other-workspace.txt").write_text("第二个项目的差异\n", encoding="utf-8")
        server = launcher.LocalServer(("127.0.0.1", 0), launcher.Handler)
        self.addCleanup(server.server_close)
        server.workspace = first
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        page = self.page
        page.goto(f"http://127.0.0.1:{server.server_port}")
        diff, preview = page.locator("#showDiff"), page.locator("#preview")
        diff.click()
        expect(self.layer("staged")).to_contain_text("+索引初版")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")
        page.locator('[data-file="README.md"]').click()
        expect(page.locator("#fileTitle")).to_have_text("README.md")
        held = {}

        def gate(route):
            if route.request.post_data_json["op"] == "diff" and server.workspace is first:
                held[route.request.post_data_json["scope"]] = route
            else:
                route.continue_()

        page.route("**/api/rpc", gate)
        def scope_request(scope):
            return lambda req: req.url.endswith("/api/rpc") and req.post_data_json.get("scope") == scope

        with page.expect_request(scope_request("staged")), page.expect_request(scope_request("unstaged")):
            diff.click()
        expect(self.layer("staged")).to_contain_text("+索引初版")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")
        self.assertEqual(set(held), {"staged", "unstaged"})
        old = held["staged"].fetch()
        self.assertIn("+索引初版", old.json()["data"]["diff"])
        server.workspace = second
        with page.expect_response(lambda res: res.url.endswith("/api/rpc") and res.status == 409) as denied:
            held["unstaged"].continue_()
        self.assertEqual(denied.value.json()["error"]["code"], "workspace_changed")
        expect(page.locator("#connectionLabel")).to_contain_text("项目已切换")
        expect(preview).not_to_contain_text("索引初版")
        expect(preview).not_to_contain_text("工作区初版")

        page.locator("#refresh").click()
        expect(page.locator("#tree")).to_contain_text("other-workspace.txt")
        diff.click()
        expect(self.layer("unstaged")).to_contain_text("+第二个项目的差异")
        expect(self.layer("staged")).to_contain_text("没有已暂存的改动")
        with page.expect_response(lambda res: res.url.endswith("/api/rpc")
                                  and res.request.post_data_json.get("scope") == "staged") as late:
            held["staged"].fulfill(response=old)
        self.assertIn("+索引初版", late.value.json()["data"]["diff"])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(preview).not_to_contain_text("索引初版")
        expect(preview).not_to_contain_text("工作区初版")
        expect(self.layer("unstaged")).to_contain_text("+第二个项目的差异")

    @staticmethod
    def diff_scope(request, scope):
        return request.url.endswith("/api/rpc") and request.post_data_json.get("scope") == scope

    def hold_diff_then_open_readme(self):
        page, held = self.page, {}
        self.diff_connection = "hold"

        def gate(route):
            query = route.request.post_data_json
            if query["op"] == "diff" and self.diff_connection == "hold":
                held[query["scope"]] = route
            elif query["op"] == "diff" and self.diff_connection == "offline":
                route.abort("internetdisconnected")
            else:
                route.continue_()

        def file_response(response):
            return response.url.endswith("/api/rpc") and response.request.post_data_json.get("op") == "file"

        page.route("**/api/rpc", gate)
        with (page.expect_request(lambda req: self.diff_scope(req, "staged")),
              page.expect_request(lambda req: self.diff_scope(req, "unstaged"))):
            page.locator("#showDiff").click()
        with page.expect_response(file_response):
            page.locator('[data-file="README.md"]').click()
        expect(page.locator("#preview")).to_contain_text(README_MARKER)
        return held

    def test_offscreen_identity_rejection_clears_history_only_for_that_workspace_visit(self):
        first = launcher.LocalWorkspace(self.project)
        second = launcher.LocalWorkspace(self.project.parent / "offscreen" / self.project.name)
        for workspace in (first, second):
            workspace.env["XDG_STATE_HOME"] = str(self.project.parent / "state")
        (second.root / "README.md").write_text("另一个项目的文件\n", encoding="utf-8")
        server = launcher.LocalServer(("127.0.0.1", 0), launcher.Handler)
        self.addCleanup(server.server_close)
        server.workspace = first
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        page = self.page
        page.goto(f"http://127.0.0.1:{server.server_port}")
        diff, preview = page.locator("#showDiff"), page.locator("#preview")
        diff.click()
        expect(self.layer("staged")).to_contain_text("+索引初版")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")

        held = self.hold_diff_then_open_readme()

        # Both requests still carry the first project's header, but the user is reading a file.
        server.workspace = second
        rejected = held["unstaged"].fetch()
        self.assertEqual(rejected.status, 409)
        self.assertEqual(rejected.json()["error"]["code"], "workspace_changed")
        with page.expect_response(lambda res: self.diff_scope(res.request, "staged") and res.status == 409) as denial:
            held["staged"].continue_()
        self.assertEqual(denial.value.json()["error"]["code"], "workspace_changed")
        self.diff_connection = "offline"
        diff.click()
        expect(self.layer("staged").get_by_role("button", name="重试读取", exact=False)).to_be_visible()
        expect(preview).not_to_contain_text("+索引初版")
        expect(preview).not_to_contain_text("+工作区初版")

        # Return to the first path after confirming B. This new visit has the same
        # workspace ID as the held 409, but its newly verified patches must survive.
        self.diff_connection = "online"
        page.locator("#refresh").click()
        expect(preview).to_contain_text("另一个项目的文件")
        server.workspace = first
        self.stage("重新进入首个项目\n")
        page.locator("#refresh").click()
        expect(preview).to_contain_text(README_MARKER)
        diff.click()
        expect(self.layer("staged")).to_contain_text("+重新进入首个项目")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")
        with page.expect_response(lambda res: self.diff_scope(res.request, "unstaged") and res.status == 409):
            held["unstaged"].fulfill(response=rejected)
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(self.layer("staged")).to_contain_text("+重新进入首个项目")
        expect(page.locator("#connectionLabel")).to_have_text("本机模式")

    def test_offscreen_no_repository_expires_only_patches_older_than_its_request(self):
        page = self.page
        diff, preview = page.locator("#showDiff"), page.locator("#preview")
        diff.click()
        expect(self.layer("staged")).to_contain_text("+索引初版")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")
        held = self.hold_diff_then_open_readme()

        marker, parked = self.project / ".git", self.project.parent / "offscreen-git"
        marker.rename(parked)
        try:
            older = held["unstaged"].fetch()
            self.assertEqual(older.json()["data"]["gitState"], "none")
            with page.expect_response(lambda res: self.diff_scope(res.request, "staged")) as current:
                held["staged"].continue_()
            self.assertEqual(current.value.json()["data"]["gitState"], "none")
            expect(preview).to_contain_text(README_MARKER)
            self.diff_connection = "offline"
            diff.click()
            expect(self.layer("staged").get_by_role("button", name="重试读取", exact=False)).to_be_visible()
            expect(preview).not_to_contain_text("+索引初版")
            expect(preview).not_to_contain_text("+工作区初版")
        finally:
            parked.rename(marker)

        self.diff_connection = "online"
        self.stage("恢复后确认的索引\n")
        diff.click()
        expect(self.layer("staged")).to_contain_text("+恢复后确认的索引")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")
        with page.expect_response(lambda res: self.diff_scope(res.request, "unstaged")):
            held["unstaged"].fulfill(response=older)
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(self.layer("staged")).to_contain_text("+恢复后确认的索引")
        page.locator('[data-file="README.md"]').click()
        self.diff_connection = "offline"
        diff.click()
        expect(self.layer("staged")).to_contain_text("缓存未校验")
        expect(self.layer("staged")).to_contain_text("+恢复后确认的索引")
        expect(self.layer("unstaged")).to_contain_text("+工作区初版")

    def test_older_staged_absence_preserves_newer_unstaged_retry_in_same_view(self):
        page = self.page
        staged, work = self.layer("staged"), self.layer("unstaged")
        diff = page.locator("#showDiff")
        diff.click()
        expect(staged).to_contain_text("+索引初版")
        expect(work).to_contain_text("+工作区初版")
        branch = page.locator("#branch").inner_text()
        held = []
        block_work = True

        def gate(route):
            if self.diff_scope(route.request, "staged"):
                held.append(route)
            elif block_work and self.diff_scope(route.request, "unstaged"):
                route.abort("internetdisconnected")
            else:
                route.continue_()

        page.route("**/api/rpc", gate)
        with page.expect_request(lambda req: self.diff_scope(req, "staged")):
            diff.click()
        retry = work.get_by_role("button", name="重试读取", exact=False)
        expect(retry).to_be_visible()
        self.assertEqual(len(held), 1)
        route = held.pop()
        marker, parked = self.project / ".git", self.project.parent / "staged-retry-git"
        marker.rename(parked)
        try:
            absence = route.fetch()
            self.assertEqual(absence.json()["data"]["gitState"], "none")
        finally:
            parked.rename(marker)

        self.loose.write_text("修复后重试的工作区\n", encoding="utf-8")
        block_work = False
        retry.click()
        expect(work).to_contain_text("+修复后重试的工作区")
        with page.expect_response(lambda res: self.diff_scope(res.request, "staged")):
            route.fulfill(response=absence)
        expect(staged.get_by_role("button", name="重试读取", exact=False)).to_be_visible()
        expect(staged).to_contain_text("+索引初版")
        expect(work).to_contain_text("+修复后重试的工作区")
        expect(work.locator(".diff-cache-state")).to_be_empty()
        expect(page.locator("#branch")).to_have_text(branch)
        expect(page.locator("#runCheck")).to_be_enabled()
