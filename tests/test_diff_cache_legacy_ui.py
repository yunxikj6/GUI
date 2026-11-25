"""Compatibility with workers that return only the original combined Git patch."""
import json
import subprocess

from test_browser import BrowserFixture, expect


class LegacyDiffCacheAcceptance(BrowserFixture):
    def test_combined_cache_retries_empty_and_truncated_patches_across_worker_changes(self):
        page = self.page
        readme = self.project / "README.md"
        original = subprocess.check_output(["git", "show", "HEAD:README.md"], cwd=self.project)
        index_line = "只在索引中的改动"
        readme.write_bytes(original + (index_line + "\n").encode())
        subprocess.run(["git", "add", "--", readme.name], cwd=self.project, check=True, capture_output=True)
        readme.write_bytes(original)  # Opposite nonempty layers cancel in the old combined result.
        large = self.project / "large.txt"
        large.write_text("旧合并补丁的开头\n" + ("x" * 180 + "\n") * 450, encoding="utf-8")
        legacy, offline, delayed_scoped = True, False, False
        calls, held = [], []

        def worker(route):
            payload = route.request.post_data_json
            if payload["op"] == "status" and legacy:
                body = route.fetch().json()
                body["data"].pop("diffScopes", None)
                route.fulfill(json=body)
            elif payload["op"] == "diff":
                calls.append(payload.get("scope"))
                if offline:
                    route.abort("internetdisconnected")
                elif delayed_scoped and payload.get("scope") == "unstaged":
                    held.append(route)
                elif legacy:
                    # Old workers ignore scope. Use the real Git RPC that retains this wire format.
                    response = route.fetch(post_data=json.dumps({"op": "diff"}))
                    route.fulfill(response=response)
                else:
                    route.continue_()
            else:
                route.continue_()

        page.route("**/api/rpc", worker)
        page.reload()
        expect(page.locator("#runCheck")).to_be_enabled()
        diff = page.locator("#showDiff")
        file = page.locator('[data-file="README.md"]')
        layers = page.locator("#preview [data-diff-scope]")
        combined = page.locator('[data-diff-scope="combined"]')
        note, stamp = combined.locator(".diff-notice"), combined.locator(".diff-cache-state")
        retry = combined.get_by_role("button", name="重试读取", exact=False)
        workspace_notice = page.locator("#fileNotice")
        diff.click()
        expect(note).to_contain_text("截断")
        expect(combined).to_contain_text("+旧合并补丁的开头")
        self.assertEqual(calls, ["staged"])
        expect(layers).to_have_count(1)

        file.click()
        offline = True
        diff.click()
        expect(stamp).to_contain_text("缓存未校验")
        expect(note).to_contain_text("截断")
        expect(combined).to_contain_text("+旧合并补丁的开头")
        expect(workspace_notice).not_to_contain_text("没有 Git 差异")
        large.unlink()
        offline = False
        calls.clear()
        retry.click()
        expect(workspace_notice).to_contain_text("当前工作区没有 Git 差异")
        expect(combined.locator(".diff-line")).to_have_count(0)
        expect(note).not_to_contain_text("截断")
        self.assertEqual(calls, ["staged"])

        file.click()
        offline = True
        diff.click()
        expect(stamp).to_contain_text("缓存未校验")
        expect(note).to_contain_text("连接")
        expect(combined.locator(".diff-line")).to_have_count(0)
        expect(workspace_notice).not_to_contain_text("没有 Git 差异")

        # An upgraded worker now echoes scope, even before a new status request.
        offline, legacy = False, False
        calls.clear()
        retry.click()
        expect(page.locator('[data-diff-scope="staged"]')).to_contain_text("+" + index_line)
        expect(page.locator('[data-diff-scope="unstaged"]')).to_contain_text("-" + index_line)
        expect(combined).to_have_count(0)
        self.assertEqual(calls, ["staged", "unstaged"])

        # A later downgrade returns one combined patch. An in-flight modern reply
        # arrives afterward and must never be paired with or replace that patch.
        file.click()
        large.write_text("降级后的合并差异\n", encoding="utf-8")
        legacy, delayed_scoped = True, True
        diff.click()
        expect(combined).to_contain_text("+降级后的合并差异")
        expect(layers).to_have_count(1)
        self.assertEqual(len(held), 1)
        with page.expect_response(lambda res: res.url.endswith("/api/rpc")
                                  and res.request.post_data_json.get("scope") == "unstaged") as late:
            held.pop().continue_()
        data = late.value.json()["data"]
        self.assertEqual(data["scope"], "unstaged")
        self.assertIn("-" + index_line, data["diff"])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(layers).to_have_count(1)
        expect(combined).to_contain_text("+降级后的合并差异")
        expect(combined).not_to_contain_text(index_line)
        calls.clear()
        offline = True
        diff.click()
        expect(stamp).to_contain_text("缓存未校验")
        expect(combined).to_contain_text("+降级后的合并差异")
        self.assertEqual(calls, ["staged"])
