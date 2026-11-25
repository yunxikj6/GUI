import base64
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


REPO = Path(__file__).resolve().parents[1]
BROWSER = os.environ.get("CLOUD_WORKSPACE_TEST_CHROMIUM") or shutil.which("chromium") or shutil.which("google-chrome")
README_MARKER = "Local render acceptance fixture"
UNTRUSTED_SOURCE = "<img src=x onerror='window.codeExecuted=true'>"
ONE_PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0K8AAAAASUVORK5CYII=")


def make_project(root):
    root.mkdir()
    assets = root / "examples"
    assets.mkdir()
    (root / "README.md").write_text(README_MARKER + "\n", encoding="utf-8")
    (assets / "notes.txt").write_text(UNTRUSTED_SOURCE + "\n", encoding="utf-8")
    (assets / "pixel.png").write_bytes(ONE_PIXEL)
    for args in (["init", "-q"], ["config", "user.name", "Browser Acceptance"],
                 ["config", "user.email", "acceptance@localhost"], ["add", "."],
                 ["commit", "-qm", "Acceptance fixture"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write("Observed working-tree change\n")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop_server(server):
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


@unittest.skipUnless(sync_playwright and BROWSER, "browser acceptance requires Playwright and Chromium")
class BrowserAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        work = tempfile.TemporaryDirectory()
        cls.addClassCleanup(work.cleanup)
        root = Path(work.name)
        project = root / "project"
        make_project(project)
        port = free_port()
        cls.origin = f"http://127.0.0.1:{port}"
        env = {**os.environ, "XDG_STATE_HOME": str(root / "state"), "PYTHONDONTWRITEBYTECODE": "1"}
        # Keep the real Git check in flight long enough to refresh between acknowledgements.
        binaries = root / "bin"
        binaries.mkdir()
        git = binaries / "git"
        git.write_text(
            f"#!{sys.executable}\nimport os, sys, time\n"
            "if 'ls-files' in sys.argv and '--cached' in sys.argv: time.sleep(2)\n"
            f"os.execv({shutil.which('git')!r}, ['git', *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        git.chmod(0o700)
        env["PATH"] = str(binaries) + os.pathsep + env["PATH"]
        log = (root / "launcher.log").open("w+")
        cls.addClassCleanup(log.close)
        server = subprocess.Popen(
            [sys.executable, str(REPO / "launcher.py"), "--local", "--port", str(port),
             "--no-browser", "--project", str(project)],
            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(stop_server, server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and server.poll() is None:
            try:
                with urlopen(cls.origin, timeout=.5) as response:
                    if response.status == 200:
                        break
            except (TimeoutError, URLError):
                time.sleep(.08)
        else:
            log.seek(0)
            raise RuntimeError(f"Local workspace unavailable: {log.read()}")
        pw = sync_playwright().start()
        cls.addClassCleanup(pw.stop)
        cls.browser = pw.chromium.launch(executable_path=BROWSER, headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(8000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.origin, wait_until="domcontentloaded")

    def tearDown(self):
        self.assertEqual([], self.errors)

    def test_files_diff_and_image(self):
        page = self.page
        tree = page.locator("#tree")
        tree.get_by_text("README.md", exact=True).click()
        expect(page.locator("#preview")).to_contain_text(README_MARKER)
        tree.get_by_text("examples", exact=True).click()
        tree.get_by_text("notes.txt", exact=True).click()
        expect(page.locator("#preview")).to_contain_text(UNTRUSTED_SOURCE)
        self.assertIsNone(page.evaluate("window.codeExecuted"))
        tree.get_by_text("pixel.png", exact=True).click()
        image = page.locator("#preview img")
        expect(image).to_be_visible()
        self.assertTrue(image.evaluate("img => img.complete && img.naturalWidth === 1"))
        page.locator("#showDiff").click()
        expect(page.locator("#preview")).to_contain_text("+Observed working-tree change")

    def test_cached_file_visible_offline_and_conflict_preserves_chinese_draft(self):
        page = self.page
        tree = page.locator("#tree")
        readme = tree.get_by_text("README.md", exact=True)
        readme.click()
        expect(page.locator("#preview")).to_contain_text(README_MARKER)
        tree.get_by_text("examples", exact=True).click()
        notes = tree.get_by_text("notes.txt", exact=True)
        notes.click()
        expect(page.locator("#preview")).to_contain_text(UNTRUSTED_SOURCE)
        held = []

        def hold_read(route):
            query = route.request.post_data_json or {}
            if query.get("op") == "file" and query.get("path") == "README.md":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", hold_read)
        readme.click()
        expect(page.locator("#preview")).to_contain_text(README_MARKER)
        expect(page.locator("#fileSync")).to_contain_text("正在校验")
        # A dropped revalidation must keep the cached preview on screen.
        self.assertEqual(len(held), 1)
        held.pop().abort("internetdisconnected")
        expect(page.locator("#fileSync")).to_contain_text("云端未校验")
        expect(page.locator("#preview")).to_contain_text(README_MARKER)

        notes.click()
        expect(page.locator("#preview")).to_contain_text(UNTRUSTED_SOURCE)
        readme.click()
        expect(page.locator("#fileSync")).to_contain_text("正在校验")
        page.locator("#editFile").click()
        draft = page.locator("#fileEditArea").input_value() + "离线输入的中文草稿\n"
        page.locator("#fileEditArea").fill(draft)
        expect(page.locator("#editState")).to_contain_text("未保存")
        # Another editor persists a new version in this isolated test project.
        endpoint = self.origin + "/api/rpc"
        base = self.context.request.post(endpoint, data={
            "op": "file", "path": "README.md"
        }).json()["data"]
        updated = base["content"] + "另一个窗口保存的版本\n"
        result = self.context.request.post(endpoint, data={
            "op": "save", "path": "README.md", "revision": base["revision"], "content": updated
        }).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(held), 1)
        held.pop().continue_()
        expect(page.locator("#editState")).to_contain_text("云端版本已变化")
        expect(page.locator("#fileEditArea")).to_have_value(draft)
        expect(page.locator("#saveFile")).to_be_disabled()
        persisted = self.context.request.post(endpoint, data={
            "op": "file", "path": "README.md"
        }).json()["data"]
        self.assertEqual(persisted["content"], updated)

    def test_mobile_layout_and_draft(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#mobileTasks").click()
        expect(page.locator("#prompt")).to_be_visible()
        page.locator("#prompt").fill("保留未发送的中文草稿")
        page.reload(wait_until="domcontentloaded")
        page.locator("#mobileTasks").click()
        expect(page.locator("#prompt")).to_have_value("保留未发送的中文草稿")
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        page.locator("#mobileFiles").click()
        expect(page.locator("#tree")).to_be_visible()
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))

    def test_lost_ack_retry_refresh_and_resume(self):
        """Drop only the acknowledgement of a committed command and resume the same task."""
        page = self.page
        starts = []
        received = []
        task_ids = set()
        interrupt_events = True

        def route_rpc(route):
            nonlocal interrupt_events
            payload = route.request.post_data_json or {}
            op = payload.get("op")
            if op == "start":
                starts.append(payload.get("requestId"))
                upstream = route.fetch()
                reply = upstream.json()
                if reply.get("ok"):
                    task_ids.add(reply["data"]["id"])
                if len(starts) == 1:
                    route.abort("failed")
                else:
                    route.fulfill(response=upstream)
            elif op == "events" and interrupt_events:
                route.abort("failed")
            else:
                route.continue_()

        def track_events(response):
            if "/api/rpc" not in response.url or not response.ok:
                return
            request = response.request.post_data_json
            if request and request.get("op") == "events":
                body = response.json()
                if body.get("ok") and body["data"]["task"]["id"] in task_ids:
                    received.extend(body["data"]["events"])

        page.route("**/api/rpc", route_rpc)
        page.on("response", track_events)
        page.locator("#runCheck").click()
        expect(page.locator("#retryStart")).to_be_visible(timeout=15000)
        page.locator("#retryStart").click()
        expect(page.locator("#taskHistory")).to_have_value(next(iter(task_ids)), timeout=20000)
        selected = page.locator("#taskHistory").input_value()
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0], starts[1])
        self.assertEqual(task_ids, {selected})
        active = self.context.request.post(self.origin + "/api/rpc", data={
            "op": "events", "taskId": selected, "after": 0
        }).json()["data"]["task"]
        self.assertIn(active["status"], {"queued", "running"}, "Refresh must happen during execution")
        page.reload(wait_until="domcontentloaded")
        interrupt_events = False
        expect(page.locator("#taskHistory")).to_have_value(selected, timeout=20000)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if received and any(e["text"] in page.locator("#eventLog").inner_text()
                                for e in received if e.get("text")):
                break
            page.wait_for_timeout(150)
        self.assertTrue(received, "No task events arrived after reconnect")
        self.assertEqual(len(page.locator(f'#taskHistory option[value="{selected}"]').all()), 1)
        status = self.context.request.post(self.origin + "/api/rpc", data={
            "op": "events", "taskId": selected, "after": 0
        })
        self.assertTrue(status.ok)
        result = status.json()["data"]
        deadline = time.monotonic() + 10
        while result["task"]["status"] in {"queued", "running"} and time.monotonic() < deadline:
            page.wait_for_timeout(150)
            result = self.context.request.post(self.origin + "/api/rpc", data={
                "op": "events", "taskId": selected, "after": 0
            }).json()["data"]
        self.assertEqual(result["task"]["status"], "succeeded")
        content = [event["text"].strip() for event in result["events"]
                   if event["kind"] == "output" and event.get("text", "").strip()]
        self.assertTrue(content, "The project check returned no visible output")
        for snippet in content:
            expect(page.locator("#eventLog")).to_contain_text(snippet, timeout=15000)
        rendered = page.locator("#eventLog .event").all_text_contents()
        self.assertEqual(rendered, [e["text"] for e in result["events"]])


    def test_desktop_route_in_local_mode(self):
        with self.context.expect_page() as new_window:
            self.page.locator('a[href="/desktop/"]').click()
        desktop = new_window.value
        expect(desktop.get_by_role("heading", name="Linux 桌面")).to_be_visible()
        expect(desktop.locator("#status")).to_contain_text("InstaBox", timeout=8000)
        self.assertTrue(self.page.locator("#tree").is_visible())

    def test_edit_save_and_stale_writer(self):
        other = self.browser.new_context()
        self.addCleanup(other.close)
        writer, stale = self.page, other.new_page()
        stale.set_default_timeout(8000)
        stale.goto(self.origin, wait_until="domcontentloaded")
        for tab in (writer, stale):
            tab.locator("#tree").get_by_text("README.md", exact=True).click()
            tab.locator("#editFile").click()
            expect(tab.locator("#fileEditArea")).to_be_visible()
        text = writer.locator("#fileEditArea").input_value()
        first = text + "Saving this revision from window One\n"
        unsaved = text + "Window Two has an unsaved change\n"
        writer.locator("#fileEditArea").fill(first)
        stale.locator("#fileEditArea").fill(unsaved)
        writer.locator("#saveFile").click()
        expect(writer.locator("#preview")).to_contain_text("Saving this revision")
        stale.locator("#saveFile").click()
        expect(stale.locator("#editState")).to_contain_text("云端版本已变化")
        expect(stale.locator("#fileEditArea")).to_have_value(unsaved)
        writer.locator("#showDiff").click()
        expect(writer.locator("#preview")).to_contain_text("+Saving this revision")
        expect(writer.locator("#preview")).not_to_contain_text("Window Two has an unsaved change")
        writer.reload(wait_until="domcontentloaded")
        writer.locator("#tree").get_by_text("README.md", exact=True).click()
        expect(writer.locator("#preview")).to_contain_text("Saving this revision")
        expect(writer.locator("#preview")).not_to_contain_text("Window Two has an unsaved change")


if __name__ == "__main__":
    unittest.main()
