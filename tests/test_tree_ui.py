"""Exercise paginated root and nested file trees through Chromium and the real worker."""
import json
from pathlib import Path
import tempfile

from test_browser import BrowserFixture, expect


class FileTreeAcceptance(BrowserFixture):
    parent = "资料"
    nested = "资料/数据集"
    root_files = [f"root-{n:03d}.txt" for n in range(413)]
    child_files = [f"sample-{n:03d}.txt" for n in range(805)]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = cls.project / cls.nested
        folder.mkdir(parents=True)
        for directory, names in ((cls.project, cls.root_files), (folder, cls.child_files)):
            # Creation order deliberately differs from the visible sort order.
            for name in reversed(names):
                (directory / name).write_text(f"已打开 {name}\n", encoding="utf-8")

    def folder(self, path):
        return self.page.locator(f'#tree [data-folder="{path}"]')

    def file(self, path):
        return self.page.locator(f'#tree [data-file="{path}"]')

    def more(self, path=""):
        return self.page.locator(f'button[data-tree-more="{path}"]')

    def notice(self, path=""):
        return self.page.locator(f'[data-tree-notice="{path}"]')

    def open_nested(self):
        self.folder(self.parent).click()
        self.folder(self.nested).click()
        expect(self.more(self.nested)).to_have_text("加载更多")

    def entries(self, path):
        return self.page.locator("#tree [data-file], #tree [data-folder]").evaluate_all("""(buttons, path) =>
            buttons.map(btn => btn.dataset.file ?? btn.dataset.folder)
                   .filter(entry => entry.split('/').slice(0,-1).join('/') === path)
                   .map(entry => entry.split('/').at(-1))""", path)

    def test_append_root_and_nested_pages_without_losing_siblings_or_edits(self):
        page = self.page
        self.folder("examples").click()
        self.file("examples/notes.txt").click()
        page.locator("#editFile").click()
        editor = page.locator("#fileEditArea")
        draft = editor.input_value() + "分页时保留的编辑"
        editor.fill(draft)
        self.open_nested()
        self.assertEqual(len(self.entries("")), 400)
        self.assertEqual(self.entries(self.nested), self.child_files[:400])

        self.more().click()
        expect(self.file(self.root_files[-1])).to_have_count(1)
        self.assertEqual(self.entries(""), ["examples", self.parent, "README.md", *self.root_files])
        for size in (800, len(self.child_files)):
            self.more(self.nested).click()
            expect(self.file(f"{self.nested}/{self.child_files[size - 1]}")).to_have_count(1)
            self.assertEqual(self.entries(self.nested), self.child_files[:size])
        expect(self.more(self.nested)).to_be_hidden()
        expect(self.folder("examples")).to_have_attribute("aria-expanded", "true")
        expect(self.folder(self.nested)).to_have_attribute("aria-expanded", "true")
        expect(editor).to_have_value(draft)
        for directory, name in (("", self.root_files[-1]), (self.nested + "/", self.child_files[-1])):
            self.file(directory + name).click()
            expect(page.locator("#preview")).to_contain_text("已打开 " + name)
        self.file("examples/notes.txt").click()
        expect(editor).to_have_value(draft)

    def test_nested_retry_and_collapse_discard_a_late_page(self):
        page, requests, held = self.page, [], []
        self.open_nested()

        def later(request):
            data = request.post_data_json or {}
            return data.get("op") == "tree" and data.get("path") == self.nested and "cursor" in data

        def intercept(route):
            if later(route.request):
                requests.append(route.request.post_data_json)
                if len(requests) == 1:
                    self.assertTrue(route.fetch().json()["ok"])  # Drop only the response to a successful listing.
                    route.abort("failed")
                    return
                if len(requests) == 3:
                    held.append(route)
                    return
            route.continue_()

        page.route("**/api/rpc", intercept)
        more = self.more(self.nested)
        more.click()
        expect(more).to_have_text("重试读取")
        self.assertEqual(self.entries(self.nested), self.child_files[:400])
        more.click()
        expect(self.file(f"{self.nested}/{self.child_files[799]}")).to_have_count(1)
        self.assertEqual(requests[0], requests[1])
        with page.expect_request(later):
            more.click()
        self.folder(self.nested).click()
        expect(self.folder(self.nested)).to_have_attribute("aria-expanded", "false")
        with page.expect_response(lambda response: later(response.request)) as late:
            self.assertEqual(len(held), 1)
            held.pop().continue_()
        self.assertTrue(late.value.json()["ok"])
        self.folder(self.nested).click()
        expect(more).to_have_text("加载更多")
        self.assertEqual(self.entries(self.nested), self.child_files[:800])
        more.click()
        expect(self.file(f"{self.nested}/{self.child_files[-1]}")).to_have_count(1)
        self.assertEqual(requests[2], requests[3])
        self.assertEqual(self.entries(self.nested), self.child_files)

    def test_refresh_supersedes_root_append_and_replaced_directory_recovers_locally(self):
        page, held = self.page, []
        self.file("README.md").click()
        page.locator("#editFile").click()
        editor = page.locator("#fileEditArea")
        draft = editor.input_value() + "保留草稿和目录状态"
        editor.fill(draft)

        def hold_root(route):
            data = route.request.post_data_json or {}
            if data.get("op") == "tree" and not data.get("path") and "cursor" in data:
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", hold_root)
        with page.expect_request(lambda req: "cursor" in (req.post_data_json or {})):
            self.more().click()
        marker = self.project / "000-refresh-marker.txt"
        marker.touch()
        self.addCleanup(marker.unlink, missing_ok=True)
        page.locator("#reloadTree").click()
        expect(self.file(marker.name)).to_have_count(1)
        with page.expect_response(lambda response: "cursor" in (response.request.post_data_json or {})) as late:
            self.assertEqual(len(held), 1)
            held.pop().continue_()
        self.assertTrue(late.value.json()["ok"])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        self.assertEqual(len(self.entries("")), 400)
        expect(self.file(self.root_files[-1])).to_have_count(0)
        expect(editor).to_have_value(draft)
        page.unroute("**/api/rpc", hold_root)

        self.folder("examples").click()
        self.open_nested()
        original = self.project / self.nested
        with tempfile.TemporaryDirectory(dir=self.project.parent) as temporary:
            previous = Path(temporary) / "previous"
            original.rename(previous)
            original.mkdir()
            replacement = original / "replacement.txt"
            try:
                replacement.write_text("新目录中的真实文件\n", encoding="utf-8")
                self.more(self.nested).click()
                expect(self.more(self.nested)).to_have_text("刷新目录")
                self.more(self.nested).click()
                new_file = self.file(f"{self.nested}/{replacement.name}")
                expect(new_file).to_have_count(1)
                self.assertEqual(self.entries(self.nested), [replacement.name])
                expect(self.file("examples/notes.txt")).to_be_visible()
                expect(editor).to_have_value(draft)
                new_file.click()
                expect(page.locator("#preview")).to_contain_text("新目录中的真实文件")
            finally:
                replacement.unlink(missing_ok=True)
                original.rmdir()
                previous.rename(original)

    def test_narrow_screen_and_legacy_truncation_have_usable_entries(self):
        page = self.page
        page.set_viewport_size({"width": 320, "height": 740})
        self.more().click()
        self.file(self.root_files[-1]).click()
        expect(page.locator("#preview")).to_contain_text("已打开 " + self.root_files[-1])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        requests = []

        def legacy(route):
            data = route.request.post_data_json or {}
            if data.get("op") != "tree":
                route.continue_()
                return
            requests.append(data)
            upstream = route.fetch(post_data=json.dumps({"op": "tree", "path": data.get("path", "")})).json()
            upstream["data"].pop("nextCursor", None)
            route.fulfill(json=upstream)

        page.route("**/api/rpc", legacy)
        page.reload(wait_until="domcontentloaded")
        expect(self.notice()).to_contain_text("400")
        expect(self.more()).to_be_hidden()
        self.folder(self.parent).click()
        self.folder(self.nested).click()
        expect(self.notice(self.nested)).to_contain_text("400")
        expect(self.more(self.nested)).to_be_hidden()
        self.file(f"{self.nested}/{self.child_files[0]}").click()
        expect(page.locator("#preview")).to_contain_text("已打开 " + self.child_files[0])
        self.assertEqual([query.get("path", "") for query in requests], ["", self.parent, self.nested])
        self.assertFalse(any("cursor" in query for query in requests))
