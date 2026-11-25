"""Exercise read-only paging and editing against real UTF-8 project files."""
from test_browser import BrowserFixture, expect


class FileReaderAcceptance(BrowserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files = {}
        for count, trailing_lf in [(3200, False), (3200, True), (3201, False), (5000, False)]:
            name = f"read-{count}-{'lf' if trailing_lf else 'eof'}.txt"
            lines = [f"行{i:04d} sample" for i in range(1, count)] + [f"文件末行 {count}"]
            lines[1599] = ""  # A real empty line on a page boundary counts toward the total.
            text = "\n".join(lines) + ("\n" if trailing_lf else "")
            (cls.project / name).write_bytes(text.encode("utf-8"))
            cls.files[name] = lines

    def file(self, name):
        return self.page.locator(f'[data-file="{name}"]')

    def open_file(self, name):
        page = self.page
        with page.expect_response(lambda res: res.url.endswith("/api/rpc") and
                                  res.request.post_data_json.get("path") == name and
                                  res.request.post_data_json["op"] == "file") as read:
            self.file(name).click()
        data = read.value.json()
        self.assertTrue(data["ok"], data)
        expect(page.locator("#fileTitle")).to_have_text(name)
        return data["data"]

    def test_all_physical_lines_are_readable_without_file_requests_or_drafts(self):
        page = self.page
        calls = []
        page.on("request", lambda req: calls.append(req.post_data_json) if req.url.endswith("/api/rpc") else None)
        rows = page.locator("#preview .code-row")
        select = page.locator("#preview .file-page")
        for name, lines in self.files.items():
            with self.subTest(name=name):
                data = self.open_file(name)
                self.assertEqual(data["content"].splitlines(), lines)
                expect(select).to_have_value("1")
                requests_before = len(calls)
                next_line = 1
                for option in select.locator("option").all():
                    select.select_option(option.get_attribute("value"))
                    rendered = rows.evaluate_all("""items => items.map(row => [
                        Number(row.querySelector('.line-no').textContent), row.querySelector('.line-text').textContent
                    ])""")
                    self.assertGreater(len(rendered), 0)
                    self.assertLessEqual(len(rendered), 800)
                    expected = lines[next_line - 1:next_line - 1 + len(rendered)]
                    self.assertEqual(rendered, [[i, text] for i, text in enumerate(expected, next_line)])
                    next_line += len(rendered)
                self.assertEqual(next_line, len(lines) + 1)
                expect(rows.last.locator(".line-text")).to_have_text(lines[-1])
                expect(page.locator(".file-next")).to_be_disabled()
                select.focus()
                select.press("Home")
                expect(rows.first.locator(".line-no")).to_have_text("1")
                expect(page.locator(".file-prev")).to_be_disabled()
                expect(page.locator("#fileNotice")).not_to_contain_text("限制显示长度")
                expect(page.locator("#editFile")).to_be_visible()
                expect(page.locator("#fileEditArea")).to_be_hidden()
                self.assertEqual(len(calls), requests_before)
        self.open_file("README.md")
        expect(page.locator("#fileDrafts")).to_be_hidden()

    def test_exact_128_kib_mixed_endings_on_mobile_keep_the_full_editable_file(self):
        page = self.page
        page.set_viewport_size({"width": 320, "height": 740})
        limit = 128 * 1024
        path = self.project / "完整 混合换行.txt"
        endings = (b"\r\n", b"\n", b"\r")
        lines = [f"第{i:04d}行".encode() for i in range(1, 5000)]
        last = "末行标记-A".encode()
        breaks = [endings[i % len(endings)] for i in range(len(lines))]
        remaining = limit - sum(map(len, lines)) - sum(map(len, breaks)) - len(last)
        padding, extra = divmod(remaining, len(lines))
        raw = b"".join(line + b"." * (padding + (i < extra)) + breaks[i]
                       for i, line in enumerate(lines)) + last
        self.assertEqual(len(raw), limit)
        path.write_bytes(raw)
        too_large = self.project / "over-limit.txt"
        too_large.write_bytes(raw + b"!")
        self.addCleanup(too_large.unlink)
        self.addCleanup(path.unlink)
        page.reload(wait_until="domcontentloaded")
        data = self.open_file(path.name)
        self.assertEqual(data["content"].encode(), raw)
        select = page.locator("#preview .file-page")
        controls = page.locator(".file-pages :is(button,select)")
        expect(controls).to_have_count(3)
        self.assertTrue(controls.evaluate_all("""items => items.every(item => {
            const box = item.getBoundingClientRect();
            return box.left >= 0 && box.right <= innerWidth && box.width >= 44 && box.height >= 44;
        })"""))
        page.locator(".file-next").focus()
        page.keyboard.press("Enter")
        expect(select).to_have_value("2")
        select.focus()
        select.press("End")
        row = page.locator("#preview .code-row").last
        row.scroll_into_view_if_needed()
        expect(row).to_be_in_viewport()
        expect(row.locator(".line-no")).to_have_text("5000")
        expect(row.locator(".line-text")).to_have_text(last.decode())
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))

        page.locator("#editFile").click()
        editor = page.locator("#fileEditArea")
        self.assertEqual(editor.input_value(), raw.decode().replace("\r\n", "\n").replace("\r", "\n"))
        editor.press("Control+End")
        editor.press("Backspace")
        editor.press("B")
        page.locator("#saveFile").click()
        expect(page.locator("#editState")).to_contain_text("已保存")
        self.assertEqual(path.read_bytes(), raw[:-1] + b"B")

        self.file(too_large.name).click()
        expect(page.locator("#fileNotice")).to_contain_text("超过 128 KiB")
        expect(page.locator("#preview .file-pages")).to_have_count(0)
        expect(page.locator("#editFile")).to_be_hidden()

    def pending_validation(self, name):
        page = self.page
        held = []

        def conditional(request):
            if not request.url.endswith("/api/rpc"):
                return False
            query = request.post_data_json
            return query.get("op") == "file" and query.get("path") == name and "ifRevision" in query

        def hold_file(route):
            if conditional(route.request):
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", hold_file)
        with page.expect_request(conditional):
            self.file(name).click()
        expect(page.locator("#fileSync")).to_contain_text("正在校验")

        def release():
            self.assertEqual(len(held), 1)
            with page.expect_response(lambda res: conditional(res.request)) as response:
                held.pop().continue_()
            expect(page.locator("#fileSync")).to_be_empty()
            page.unroute("**/api/rpc", hold_file)
            return response.value.json()["data"]
        return release

    def test_unchanged_revalidation_keeps_the_readers_page_selection_and_position(self):
        page = self.page
        name = "read-5000-eof.txt"
        original = self.open_file(name)
        self.open_file("README.md")
        release = self.pending_validation(name)
        select = page.locator("#preview .file-page")
        select.select_option("4")
        text = page.locator("#preview").get_by_text("行2750 sample", exact=True)
        text.scroll_into_view_if_needed()
        text.dblclick()
        snapshot = """() => {
            const sel = getSelection(), range = sel.getRangeAt(0);
            return {text: sel.toString(), line: sel.anchorNode.parentElement.closest('.code-row').innerText,
                    start: sel.anchorOffset, end: sel.focusOffset, top: range.getBoundingClientRect().top};
        }"""
        before = page.evaluate(snapshot)
        self.assertTrue(before["text"])
        expect(text).to_be_in_viewport()
        confirmation = release()
        self.assertTrue(confirmation["unchanged"])
        self.assertEqual(confirmation["revision"], original["revision"])
        expect(select).to_have_value("4")
        self.assertEqual(page.evaluate(snapshot), before)
        expect(text).to_be_in_viewport()

    def test_selection_survives_edits_on_another_page_and_clears_when_rewritten(self):
        page = self.page
        name = "read-5000-eof.txt"
        path = self.project / name
        content = self.open_file(name)["content"]
        self.addCleanup(path.write_text, content, encoding="utf-8")
        self.open_file("README.md")
        release = self.pending_validation(name)
        select = page.locator("#preview .file-page")
        select.select_option("4")
        line = page.locator("#preview").get_by_text("行2750 sample", exact=True)
        line.scroll_into_view_if_needed()
        line.dblclick()
        selected = page.evaluate("getSelection().toString()")
        self.assertTrue(selected)
        node = line.element_handle()
        scroll = page.locator("#preview").evaluate("el => el.scrollTop")
        content = content.replace("文件末行 5000", "远端更新后的末行")
        path.write_text(content, encoding="utf-8")
        self.assertIn("远端更新后的末行", release()["content"])
        self.assertTrue(node.evaluate("el => el.isConnected && el.contains(getSelection().anchorNode)"))
        self.assertEqual(page.evaluate("getSelection().toString()"), selected)
        self.assertEqual(page.locator("#preview").evaluate("el => el.scrollTop"), scroll)
        expect(select).to_have_value("4")
        select.select_option("7")
        expect(page.locator("#preview .line-text").last).to_have_text("远端更新后的末行")

        release = self.pending_validation(name)
        select.select_option("4")
        line.scroll_into_view_if_needed()
        line.dblclick()
        self.assertTrue(page.evaluate("getSelection().toString()"))
        path.write_text(content.replace("行2750 sample", "选中行已更新"), encoding="utf-8")
        release()
        expect(page.locator("#preview").get_by_text("选中行已更新", exact=True)).to_be_visible()
        self.assertEqual(page.evaluate("getSelection().toString()"), "")
        expect(select).to_have_value("4")

    def test_focused_pager_moves_to_preview_when_text_is_replaced_with_an_image(self):
        page = self.page
        name = "read-3200-eof.txt"
        path = self.project / name
        self.addCleanup(path.write_bytes, path.read_bytes())
        self.open_file(name)
        self.open_file("README.md")
        release = self.pending_validation(name)
        select = page.locator("#preview .file-page")
        select.select_option("3")
        select.focus()
        path.write_bytes((self.project / "examples" / "pixel.png").read_bytes())
        self.assertEqual(release()["kind"], "image")
        expect(page.locator("#preview img")).to_be_visible()
        expect(select).to_have_count(0)
        expect(page.locator("#preview")).to_be_focused()
