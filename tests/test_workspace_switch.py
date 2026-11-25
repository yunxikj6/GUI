"""A live browser tab must not edit the next project served on its loopback port."""
from pathlib import Path
import tempfile
import threading
import unittest

import launcher

from test_browser import BROWSER, expect, sync_playwright


@unittest.skipUnless(sync_playwright and BROWSER, "browser acceptance requires Playwright and Chromium")
class WorkspaceSwitchTest(unittest.TestCase):
    def test_stale_tab_keeps_its_draft_when_launcher_serves_another_project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        first = launcher.LocalWorkspace(root / "first" / "project")
        second = launcher.LocalWorkspace(root / "second" / "project")
        for workspace in (first, second):
            workspace.env["XDG_STATE_HOME"] = str(root / "state")
        check = first.rpc({"op": "start", "requestId": "first-check", "provider": "check",
                           "prompt": "检查第一个项目的文件"})
        self.assertTrue(check["ok"], check)
        (second.root / "second-project.txt").write_text("second", encoding="utf-8")
        original = (first.root / "README.md").read_text(encoding="utf-8")
        self.assertEqual((second.root / "README.md").read_text(encoding="utf-8"), original)
        draft = original + "保留在第一个项目的未保存草稿\n"

        server = launcher.LocalServer(("127.0.0.1", 0), launcher.Handler)
        self.addCleanup(server.server_close)
        server.workspace = first
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        browser = pw.chromium.launch(executable_path=BROWSER, headless=True)
        self.addCleanup(browser.close)
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        expect(page.locator("#taskHistory")).to_have_value(check["data"]["id"])
        expect(page.locator("#eventLog")).to_contain_text("项目检查通过")
        page.locator("#editFile").click()
        prompt = page.locator("#prompt")
        prompt.fill("第一个项目的助手草稿")
        editor = page.locator("#fileEditArea")
        editor.fill(draft)

        # Equivalent to restarting the launcher on this port. Both files have the same revision.
        server.workspace = second
        with page.expect_response(lambda res: res.url.endswith("/api/rpc")
                                  and res.request.post_data_json.get("op") == "save") as saved:
            page.locator("#saveFile").click()
        self.assertFalse(saved.value.json()["ok"])
        expect(editor).to_have_value(draft)
        self.assertEqual((second.root / "README.md").read_text(encoding="utf-8"), original)
        self.assertEqual((first.root / "README.md").read_text(encoding="utf-8"), original)

        page.route("**/api/rpc", lambda route: route.abort("internetdisconnected"))
        with page.expect_response(lambda res: res.url.endswith("/api/workspace")):
            page.locator("#refresh").click()
        expect(editor).to_be_hidden()
        expect(prompt).to_have_value("")
        expect(page.locator("#eventLog")).to_have_text("")
        expect(page.locator("#taskHistory")).not_to_contain_text("检查第一个项目的文件")
        expect(page.locator("#taskRequest")).to_be_hidden()
        prompt.fill("第二个项目的离线助手草稿")
        expect(page.locator("#runAgent")).to_be_disabled()

        page.unroute("**/api/rpc")
        page.locator("#refresh").click()
        expect(page.locator("#tree")).to_contain_text("second-project.txt")
        expect(prompt).to_have_value("第二个项目的离线助手草稿")
        expect(editor).to_be_hidden()
        expect(page.locator("#preview")).not_to_contain_text("未保存草稿")
        server.workspace = first
        page.locator("#refresh").click()
        expect(editor).to_be_visible()
        expect(editor).to_have_value(draft)
        expect(prompt).to_have_value("第一个项目的助手草稿")
        page.locator("#saveFile").click()
        expect(page.locator("#editState")).to_contain_text("已保存")
        self.assertEqual((first.root / "README.md").read_text(encoding="utf-8"), draft)
        self.assertEqual((second.root / "README.md").read_text(encoding="utf-8"), original)
