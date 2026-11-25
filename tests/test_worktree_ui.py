"""Exercise linked Git worktrees through the local launcher and Chromium."""
import os
import re
import select
import subprocess
import sys

from test_browser import BrowserFixture, REPO, expect, stop_server


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


class WorktreeAcceptance(BrowserFixture):
    branch = "feature/linked-checkout"

    @classmethod
    def launch_project(cls, root):
        server = subprocess.Popen(
            [sys.executable, str(REPO / "launcher.py"), "--local", "--project", str(root),
             "--port", "0", "--no-browser"],
            cwd=REPO, env={**os.environ, "XDG_STATE_HOME": str(cls.project.parent / "state")},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        cls.addClassCleanup(server.stdout.close)
        cls.addClassCleanup(stop_server, server)
        if not select.select([server.stdout], [], [], 15)[0]:
            raise RuntimeError("Local launcher did not announce its bound port")
        banner = server.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:\d+/", banner)
        if not match:
            raise RuntimeError(f"Local launcher startup failed: {banner}")
        return match.group()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.main_origin = cls.origin
        cls.main_branch = git(cls.project, "symbolic-ref", "--short", "HEAD")
        cls.linked = cls.project.parent / "关联 工作树"
        git(cls.project, "worktree", "add", "-q", "-b", cls.branch, str(cls.linked))
        # The same tracked paths have independent index and working tree contents.
        for root, label in [(cls.project, "主工作树"), (cls.linked, "关联工作树")]:
            with (root / "examples/notes.txt").open("a", encoding="utf-8") as stream:
                stream.write(f"{label}已暂存\n")
            git(root, "add", "--", "examples/notes.txt")
            with (root / "README.md").open("a", encoding="utf-8") as stream:
                stream.write(f"{label}未暂存\n")
            (root / "新增 文件.txt").write_text(f"{label}未跟踪\n", encoding="utf-8")
        cls.origin = cls.launch_project(cls.linked)

        # Its parent is a real repository. Git must stay scoped to the selected root.
        cls.plain = cls.project / "普通 目录"
        cls.plain.mkdir()
        (cls.plain / "记录.txt").write_text("普通目录原文\n", encoding="utf-8")
        cls.plain_origin = cls.launch_project(cls.plain)

    def test_main_and_linked_branches_and_diff_use_the_selected_checkout(self):
        page = self.page
        for origin, branch, label, other in [
            (self.origin, self.branch, "关联工作树", "主工作树"),
            (self.main_origin, self.main_branch, "主工作树", "关联工作树"),
        ]:
            with self.subTest(branch=branch):
                page.goto(origin)
                expect(page.locator("#branch")).to_have_text(branch)
                expect(page.locator('[data-file=".git"]')).to_have_count(0)
                page.locator('[data-file="新增 文件.txt"]').click()
                expect(page.locator("#preview")).to_contain_text(label + "未跟踪")
                page.locator("#showDiff").click()
                diff = page.locator("#preview .add-line .line-text")
                for kind in ("已暂存", "未暂存", "未跟踪"):
                    expect(diff.filter(has_text=re.compile(r"^\+" + label + kind + "$"))).to_have_count(1)
                    expect(page.locator("#preview")).not_to_contain_text(other + kind)

    def check_project(self, outcome, messages):
        page = self.page
        with page.expect_response(lambda res: res.url.endswith("/api/rpc")
                                  and res.request.post_data_json["op"] == "start") as started:
            page.locator("#runCheck").click()
        task = started.value.json()
        self.assertTrue(task["ok"], task)
        expect(page.locator("#taskHistory")).to_have_value(task["data"]["id"])
        expect(page.locator("#taskStatus")).to_have_attribute("data-status", outcome, timeout=15000)
        for message in messages:
            expect(page.locator("#eventLog")).to_contain_text(message)

    def test_checks_validate_the_linked_index_and_working_tree_independently(self):
        self.check_project("succeeded", ["工作区检查通过", "暂存区检查通过"])
        path = self.linked / "examples/notes.txt"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"whitespace error   \n")
            self.check_project("failed", ["工作区检查未通过", "暂存区检查通过"])
            git(self.linked, "add", "--", "examples/notes.txt")
            self.check_project("failed", ["工作区检查通过", "暂存区检查未通过"])
            self.page.goto(self.main_origin)
            self.check_project("succeeded", ["工作区检查通过", "暂存区检查通过"])
        finally:
            path.write_bytes(original)
            git(self.linked, "add", "--", "examples/notes.txt")

    def test_detached_linked_checkout_displays_commit_and_still_has_its_diff(self):
        page = self.page
        revision = git(self.linked, "rev-parse", "--short", "HEAD")
        git(self.linked, "switch", "-q", "--detach", "HEAD")
        try:
            page.locator("#refresh").click()
            expect(page.locator("#branch")).to_contain_text(revision)
            page.locator("#showDiff").click()
            expect(page.locator("#preview")).to_contain_text("+关联工作树已暂存")
            expect(page.locator("#preview")).not_to_contain_text("主工作树已暂存")
        finally:
            git(self.linked, "switch", "-q", self.branch)

    def test_diff_detects_broken_and_repaired_registration_without_discarding_draft(self):
        root = self.project.parent / "独立 干净工作树"
        branch = "feature/diff-recovery"
        git(self.project, "worktree", "add", "-q", "-b", branch, str(root))
        page = self.page
        page.goto(self.launch_project(root))
        label, check = page.locator("#branch"), page.locator("#runCheck")
        diff, notice = page.locator("#showDiff"), page.locator("#fileNotice")
        readme, editor = page.locator('[data-file="README.md"]'), page.locator("#fileEditArea")
        expect(label).to_have_text(branch)
        readme.click()
        page.locator("#editFile").click()
        draft = editor.input_value() + "保留到下一次保存的中文编辑\n"
        editor.fill(draft)

        requests, held = [], []
        hold_status = False

        def observe(route):
            query = route.request.post_data_json
            op = query["op"]
            if op in ("diff", "status"):
                requests.append((op, query.get("scope")))
            if hold_status and op == "status":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", observe)
        diff.click()
        expect(notice).to_contain_text("没有 Git 差异")
        self.assertCountEqual(requests, [("diff", "staged"), ("diff", "unstaged")])

        marker = root / ".git"
        original = marker.read_bytes()
        self.addCleanup(marker.write_bytes, original)
        broken = b"gitdir: ./missing-admin-directory\n"
        marker.write_bytes(broken)
        diff.click()
        expect(label).to_have_text("Git 仓库不可用")
        expect(check).to_be_disabled()
        expect(notice).to_contain_text("Git 工作树登记无效")
        readme.click()
        expect(editor).to_have_value(draft)

        marker.write_bytes(original)
        diff.click()
        expect(notice).to_contain_text("没有 Git 差异")
        expect(label).to_have_text(branch)
        expect(check).to_be_enabled()

        # Hold the status request from the diff view while the user reopens the
        # draft. Deliver its actual error snapshot after the marker is repaired.
        hold_status = True
        marker.write_bytes(broken)
        with page.expect_request(lambda req: req.url.endswith("/api/rpc")
                                 and req.post_data_json["op"] == "status"):
            diff.click()
        readme.click()
        expect(editor).to_have_value(draft)
        self.assertEqual(len(held), 1)
        route = held.pop()
        snapshot = route.fetch()
        self.assertEqual(snapshot.json()["data"]["gitState"], "error")
        marker.write_bytes(original)
        with page.expect_response(lambda res: res.url.endswith("/api/rpc")
                                  and res.request.post_data_json["op"] == "status") as late:
            route.fulfill(response=snapshot)
        self.assertEqual(late.value.json()["data"]["gitState"], "error")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(label).to_have_text(branch)
        expect(check).to_be_enabled()
        expect(page.locator("#fileTitle")).to_have_text("README.md")
        expect(notice).not_to_contain_text("Git")
        expect(editor).to_be_visible()
        expect(editor).to_have_value(draft)
        self.assertEqual(git(root, "status", "--porcelain"), "")

    def test_plain_directory_and_invalid_git_metadata_keep_files_available(self):
        page = self.page
        page.goto(self.plain_origin)
        branch, hint = page.locator("#branch"), page.locator("#checkHint")
        notice = page.locator("#fileNotice")
        expect(branch).to_contain_text("没有 Git 仓库")
        expect(page.locator("#runCheck")).to_be_disabled()
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("Git")
        page.locator("#showDiff").click()
        expect(notice).to_contain_text("Git")
        expect(notice).not_to_contain_text("没有 Git 差异")
        page.locator('[data-file="记录.txt"]').click()
        page.locator("#editFile").click()
        page.locator("#fileEditArea").fill("从普通目录保存\n")
        page.locator("#saveFile").click()
        expect(page.locator("#editState")).to_contain_text("已保存")
        self.assertEqual((self.plain / "记录.txt").read_text(encoding="utf-8"), "从普通目录保存\n")

        marker = self.plain / ".git"
        self.assertFalse(marker.exists())
        marker.write_text("gitdir: ./missing-admin-directory\n", encoding="utf-8")
        self.addCleanup(marker.unlink)
        page.locator("#refresh").click()
        expect(branch).to_contain_text("Git 仓库不可用")
        expect(page.locator("#runCheck")).to_be_disabled()
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("Git")
        page.locator("#showDiff").click()
        expect(notice).to_contain_text("Git")
        expect(notice).not_to_contain_text("没有 Git 差异")
        page.locator('[data-file="记录.txt"]').click()
        expect(page.locator("#preview")).to_contain_text("从普通目录保存")
