from test_browser import BrowserFixture, expect


class FileDraftRecovery(BrowserFixture):
    def edit_real_file(self, name, raw):
        path = self.project / name
        path.write_bytes(raw)
        self.addCleanup(path.unlink, missing_ok=True)
        self.page.locator("#reloadTree").click()
        self.page.locator("#tree").get_by_text(name, exact=True).click()
        self.page.locator("#editFile").click()
        return path

    def test_mixed_line_endings_preserve_bytes_caret_and_composition(self):
        page = self.page
        original = "甲\r\n乙\n丙\r丁\r\n尾"
        path = self.edit_real_file("mixed.txt", original.encode())
        editor = page.locator("#fileEditArea")
        editor.evaluate("area => area.setSelectionRange(1,2)")
        editor.press("Backspace")
        editor.press("Control+z")
        expect(page.locator("#saveFile")).to_be_disabled()
        editor.evaluate("area => area.setSelectionRange(1,1)")
        ime = self.context.new_cdp_session(page)
        ime.send("Input.imeSetComposition", {"text": "zh", "selectionStart": 2, "selectionEnd": 2})
        ime.send("Input.insertText", {"text": "中"})
        self.assertEqual(editor.evaluate("area => area.selectionStart"), 2)
        self.assertTrue(editor.input_value().startswith("甲中\n乙"))
        for label in ("乙", "丙"):
            editor.evaluate("""(area, text) => {
                const caret = area.value.indexOf(text) + text.length;
                area.setSelectionRange(caret,caret);
            }""", label)
            editor.press("Enter")
        editor.press("Control+s")
        expect(editor).to_be_hidden()
        self.assertEqual(path.read_bytes(), "甲中\r\n乙\n\n丙\r\r丁\r\n尾".encode())

    def test_deletion_between_cr_and_lf_keeps_two_visible_breaks(self):
        path = self.edit_real_file("adjacent.txt", b"a\rb\nc\r\n")
        editor = self.page.locator("#fileEditArea")
        editor.evaluate("area => area.setSelectionRange(2,3)")
        editor.press("Backspace")
        expect(editor).to_have_value("a\n\nc\n")
        editor.press("Control+s")
        expect(editor).to_be_hidden()
        self.assertEqual(path.read_bytes(), b"a\r\r\nc\r\n")

    def test_disconnected_reload_recovers_multiple_files_and_detects_conflict(self):
        page = self.page
        editor = page.locator("#fileEditArea")
        page.locator("#tree").get_by_text("examples", exact=True).click()
        page.locator("#tree").get_by_text("notes.txt", exact=True).click()
        page.locator("#editFile").click()
        notes = editor.input_value() + "笔记本地编辑\n"
        editor.fill(notes)
        page.locator("#tree").get_by_text("README.md", exact=True).click()
        page.locator("#editFile").click()
        readme = editor.input_value() + "自述文件本地编辑\n"
        editor.fill(readme)

        # The launcher answers immediately while cloud status is still in flight.
        held = []

        def disconnected(route):
            if route.request.post_data_json.get("op") == "status":
                held.append(route)
            else:
                route.abort("internetdisconnected")

        page.route("**/api/rpc", disconnected)
        page.reload(wait_until="domcontentloaded")
        expect(editor).to_have_value(readme)
        expect(page.locator("#saveFile")).to_be_disabled()
        self.assertEqual(len(held), 1)
        held.pop().abort("internetdisconnected")
        expect(page.locator("#connectionLabel")).to_have_text("连接中断")
        editor.fill(readme + "断线后再写一行\n")
        page.locator("#fileDrafts").select_option("examples/notes.txt")
        expect(editor).to_have_value(notes)

        endpoint = self.origin + "/api/rpc"
        original = self.context.request.post(endpoint, data={"op": "file", "path": "examples/notes.txt"}).json()["data"]
        other_edit = original["content"] + "另一位编辑者的文字\n"
        result = self.context.request.post(endpoint, data={
            "op": "save", "path": original["path"], "revision": original["revision"], "content": other_edit,
        }).json()
        self.assertTrue(result["ok"], result)
        page.unroute("**/api/rpc")
        page.locator("#refresh").click()
        expect(page.locator("#editState")).to_contain_text("云端版本已变化")
        expect(editor).to_have_value(notes)
        expect(page.locator("#saveFile")).to_be_disabled()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#cancelEdit").click()
        expect(page.locator("#preview")).to_contain_text("另一位编辑者的文字")
        page.locator("#fileDrafts").select_option("README.md")
        expect(editor).to_have_value(readme + "断线后再写一行\n")
        page.locator("#saveFile").click()
        expect(page.locator("#preview")).to_contain_text("断线后再写一行")
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#preview")).to_contain_text("断线后再写一行")
        expect(editor).to_be_hidden()
        expect(page.locator("#fileDrafts")).to_be_hidden()

    def test_tabs_and_workspaces_with_same_name_and_path_have_separate_drafts(self):
        page = self.page
        page.locator("#editFile").click()
        editor = page.locator("#fileEditArea")
        draft = editor.input_value() + "此标签页此项目的草稿"
        editor.fill(draft)
        other_tab = self.context.new_page()
        other_tab.goto(self.origin)
        expect(other_tab.locator("#editFile")).to_be_visible()
        expect(other_tab.locator("#fileEditArea")).to_be_hidden()
        other_tab.close()

        # A disconnected launcher at this origin now serves a different project
        # with the same name. It must never show the previous project's drafts.
        def other_identity(route):
            body = route.fetch().json()
            body["data"]["workspaceId"] = "other-workspace"
            route.fulfill(json=body)

        page.route("**/api/rpc", lambda route: route.abort("internetdisconnected"))
        page.route("**/api/workspace", other_identity)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#topProject")).to_have_text("project")
        expect(editor).to_be_hidden()
        expect(page.locator("#fileDrafts")).to_be_hidden()
        page.unroute("**/api/rpc")
        page.unroute("**/api/workspace")
        page.locator("#refresh").click()
        expect(editor).to_have_value(draft)

    def test_storage_failure_leaves_editor_usable_and_explains_reload_risk(self):
        page = self.page
        page.add_init_script("""{
            const set = Storage.prototype.setItem;
            Storage.prototype.setItem = function (key, value) {
                if (this === sessionStorage) throw new DOMException('Full', 'QuotaExceededError');
                return set.call(this, key, value);
            };
        }""")
        page.reload(wait_until="domcontentloaded")
        page.locator("#editFile").click()
        editor = page.locator("#fileEditArea")
        draft = editor.input_value() + "内存中的编辑仍可保存\n"
        editor.fill(draft)
        expect(page.locator("#editState")).to_contain_text("无法备份最新草稿")
        page.route("**/api/rpc", lambda route: route.abort("internetdisconnected"))
        page.route("**/api/workspace", lambda route: route.fulfill(json={
            "ok": True, "data": {"mode": "local", "workspaceId": "different-workspace", "project": "不同项目"},
        }))
        page.locator("#refresh").click()
        expect(page.locator("#topProject")).to_have_text("不同项目")
        expect(editor).to_be_hidden()
        page.unroute("**/api/rpc")
        page.unroute("**/api/workspace")
        page.locator("#refresh").click()
        expect(editor).to_have_value(draft)
        page.locator("#saveFile").click()
        expect(page.locator("#preview")).to_contain_text("内存中的编辑仍可保存")

    def test_lost_save_ack_recovers_committed_revision_and_subsequent_typing(self):
        page = self.page
        editor = page.locator("#fileEditArea")
        path = self.edit_real_file("windows.txt", b"one\r\ntwo\r\n")
        first = editor.input_value() + "已提交的第一份修改\n"
        editor.fill(first)

        def lose_ack(route):
            if route.request.post_data_json.get("op") == "save":
                self.assertTrue(route.fetch().json()["ok"])
                route.abort("failed")
            else:
                route.continue_()

        page.route("**/api/rpc", lose_ack)
        page.locator("#saveFile").click()
        expect(page.locator("#editState")).to_contain_text("连接中断")
        self.assertEqual(path.read_bytes(), first.replace("\n", "\r\n").encode())
        second = first + "随后继续输入的文字\n"
        editor.fill(second)
        page.unroute("**/api/rpc")
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#editState")).to_contain_text("上次保存已确认")
        expect(editor).to_have_value(second)
        page.locator("#saveFile").click()
        expect(page.locator("#preview")).to_contain_text("随后继续输入的文字")
        self.assertEqual(path.read_bytes(), second.replace("\n", "\r\n").encode())
