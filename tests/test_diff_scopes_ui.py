"""Verify that the Git panel reflects both sides of the real index."""
import json
import subprocess

from test_browser import BrowserFixture, expect


class DiffScopeAcceptance(BrowserFixture):
    @classmethod
    def git(cls, *args):
        return subprocess.run(["git", *args], cwd=cls.project, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cancel = cls.project / "改回原文.txt"
        partial = cls.project / "部分暂存.txt"
        cancel.write_text("抵消的原文 A\n", encoding="utf-8")
        partial.write_text("部分暂存的原文\n", encoding="utf-8")
        cls.git("add", "--", cancel.name, partial.name)
        cls.git("commit", "-qm", "Add index comparison fixtures")
        cancel.write_text("抵消的索引 B\n", encoding="utf-8")
        partial.write_text("部分暂存的索引\n", encoding="utf-8")
        cls.git("add", "--", cancel.name, partial.name)
        cancel.write_text("抵消的原文 A\n", encoding="utf-8")
        partial.write_text("部分暂存的工作区\n", encoding="utf-8")
        (cls.project / "未跟踪 文件.txt").write_text("仅在工作区的新文件\n", encoding="utf-8")

    def group(self, scope):
        return self.page.locator(f'#preview [data-diff-scope="{scope}"]')

    def test_opposite_patches_partial_staging_and_untracked_file(self):
        self.assertEqual(self.git("diff", "HEAD", "--", "改回原文.txt"), "")
        self.page.locator("#showDiff").click()
        staged, work = self.group("staged"), self.group("unstaged")
        for group, label, lines in [
            (staged, "已暂存", ["-抵消的原文 A", "+抵消的索引 B", "-部分暂存的原文", "+部分暂存的索引"]),
            (work, "未暂存", ["-抵消的索引 B", "+抵消的原文 A", "-部分暂存的索引", "+部分暂存的工作区",
                          "+仅在工作区的新文件"]),
        ]:
            expect(group.locator("summary")).to_contain_text(label)
            for line in lines:
                expect(group.get_by_text(line, exact=True)).to_be_visible()
        expect(staged).not_to_contain_text("未跟踪 文件.txt")
        expect(staged).not_to_contain_text("部分暂存的工作区")
        expect(work).not_to_contain_text("部分暂存的原文")

    def test_large_patch_in_either_scope_preserves_the_other_scope(self):
        big = self.project / "large-patch.txt"
        # Larger than the 64 KiB diff cap, still within the 128 KiB text preview limit.
        big.write_text(("large diff " + "x" * 180 + "\n") * 440, encoding="utf-8")
        self.addCleanup(big.unlink)
        self.git("add", "--", big.name)
        self.addCleanup(self.git, "rm", "--cached", "--ignore-unmatch", "--", big.name)
        diff = self.page.locator("#showDiff")
        staged, work = self.group("staged"), self.group("unstaged")
        diff.click()
        expect(staged.locator(".diff-notice")).to_contain_text("仅显示开头部分")
        expect(work.get_by_text("+仅在工作区的新文件", exact=True)).to_be_visible()
        expect(work).not_to_contain_text("仅显示开头部分")

        self.git("restore", "--staged", "--", big.name)
        diff.click()
        expect(work.locator(".diff-notice")).to_contain_text("仅显示开头部分")
        expect(staged.get_by_text("+部分暂存的索引", exact=True)).to_be_visible()
        expect(staged).not_to_contain_text("仅显示开头部分")

    def test_old_service_that_ignores_scope_displays_one_combined_patch(self):
        page = self.page
        probes = []

        def ignore_unknown_scope(route):
            payload = route.request.post_data_json
            if payload["op"] == "status":
                reply = route.fetch().json()
                reply["data"].pop("diffScopes", None)
                route.fulfill(json=reply)
                return
            if payload["op"] != "diff":
                route.continue_()
                return
            probes.append(payload.get("scope"))
            # Run the existing unscoped Git RPC and return its authentic legacy shape.
            response = route.fetch(post_data=json.dumps({"op": "diff"}))
            self.assertNotIn("scope", response.json()["data"])
            route.fulfill(response=response)

        page.route("**/api/rpc", ignore_unknown_scope)
        page.reload()
        expect(page.locator("#runCheck")).to_be_enabled()
        page.locator("#showDiff").click()
        combined = self.group("combined")
        expect(combined.locator("summary")).to_contain_text("合并差异")
        for line in ("-部分暂存的原文", "+部分暂存的工作区", "+仅在工作区的新文件"):
            expect(combined.get_by_text(line, exact=True)).to_be_visible()
        expect(combined).not_to_contain_text("抵消的索引 B")
        expect(combined).not_to_contain_text("部分暂存的索引")
        expect(page.locator("#preview [data-diff-scope]")).to_have_count(1)
        self.assertEqual(probes, ["staged"])

    def test_unstaged_patch_appears_while_staged_is_pending_and_late_result_keeps_draft(self):
        page = self.page
        readme, editor = page.locator('[data-file="README.md"]'), page.locator("#fileEditArea")
        readme.click()
        page.locator("#editFile").click()
        draft = editor.input_value() + "跨视图保留的草稿\n"
        editor.fill(draft)
        held = []

        def is_staged(request):
            return request.url.endswith("/api/rpc") and request.post_data_json.get("scope") == "staged"

        def hold_staged(route):
            if is_staged(route.request):
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", hold_staged)
        with page.expect_request(is_staged):
            page.locator("#showDiff").click()
        expect(self.group("unstaged").get_by_text("+仅在工作区的新文件", exact=True)).to_be_visible()
        readme.click()
        expect(editor).to_have_value(draft)
        self.assertEqual(len(held), 1)
        with page.expect_response(lambda res: is_staged(res.request)) as late:
            held.pop().continue_()
        self.assertIn("+抵消的索引 B", late.value.json()["data"]["diff"])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(page.locator("#fileTitle")).to_have_text("README.md")
        expect(page.locator("#preview [data-diff-scope]")).to_have_count(0)
        expect(editor).to_be_visible()
        expect(editor).to_have_value(draft)
        expect(page.locator("#editState")).to_contain_text("未保存")

    def test_long_complete_patches_page_independently_with_keyboard_on_mobile(self):
        page = self.page
        page.set_viewport_size({"width": 320, "height": 740})
        markers = {"staged": "暂存补丁末行", "unstaged": "未暂存补丁末行"}
        for scope, marker in markers.items():
            path = self.project / f"{scope}-pages.txt"
            lines = [f"{scope[0]}{i:04d}" for i in range(1, 3600)] + [marker]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.addCleanup(path.unlink)
            if scope == "staged":
                self.git("add", "--", path.name)
                self.addCleanup(self.git, "rm", "--cached", "--ignore-unmatch", "--", path.name)

        def diff_response(scope):
            return lambda res: res.url.endswith("/api/rpc") and res.request.post_data_json == {
                "op": "diff", "scope": scope,
            }

        with page.expect_response(diff_response("staged")) as staged_result, \
             page.expect_response(diff_response("unstaged")) as work_result:
            page.locator("#showDiff").click()
        patch_lines = {}
        for scope, result in [("staged", staged_result), ("unstaged", work_result)]:
            data = result.value.json()["data"]
            self.assertFalse(data["truncated"])
            self.assertLess(len(data["diff"].encode("utf-8")), 64 * 1024)
            patch_lines[scope] = data["diff"].splitlines()
            self.assertGreater(len(patch_lines[scope]), 3200)
            expect(self.group(scope).locator(".diff-page")).to_have_value("1")
            expect(self.group(scope)).not_to_contain_text("仅显示开头部分")
            controls = self.group(scope).locator(".diff-pages :is(button,select)")
            self.assertTrue(controls.evaluate_all("""items => items.every(item => {
                const box = item.getBoundingClientRect();
                return box.left >= 0 && box.right <= innerWidth && box.width >= 44 && box.height >= 44;
            })"""))

        requests = []
        page.on("request", lambda req: requests.append(req.post_data_json) if req.url.endswith("/api/rpc") else None)
        staged, work = self.group("staged"), self.group("unstaged")

        def numbers(group):
            return [int(text) for text in group.locator(".diff-line .line-no").all_text_contents()]

        first_page = numbers(staged)
        self.assertEqual(first_page, list(range(1, len(first_page) + 1)))
        other_page = numbers(work)
        next_button = staged.locator(".diff-next")
        next_button.focus()
        next_button.press("Enter")
        expect(staged.locator(".diff-page")).to_have_value("2")
        second_page = numbers(staged)
        self.assertEqual(second_page, list(range(first_page[-1] + 1, first_page[-1] + len(second_page) + 1)))
        self.assertEqual(numbers(work), other_page)

        for scope, group, other in [("staged", staged, work), ("unstaged", work, staged)]:
            other_value = other.locator(".diff-page").input_value()
            other_numbers = numbers(other)
            select = group.locator(".diff-page")
            select.focus()
            select.press("End")
            expect(select).to_have_value(str(select.locator("option").count()))
            marker = "+" + markers[scope]
            row = group.locator(".diff-line").filter(has=page.get_by_text(marker, exact=True))
            row.scroll_into_view_if_needed()
            expect(row).to_be_in_viewport()
            expect(row.locator(".line-no")).to_have_text(str(patch_lines[scope].index(marker) + 1))
            last_page = numbers(group)
            self.assertEqual(last_page, list(range(last_page[0], len(patch_lines[scope]) + 1)))
            expect(other.locator(".diff-page")).to_have_value(other_value)
            self.assertEqual(numbers(other), other_numbers)

        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.assertEqual(requests, [])
