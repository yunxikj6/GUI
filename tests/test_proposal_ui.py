"""Review a real worker's single-file proposal in Chromium with a deterministic Codex fixture."""
import sys

from test_browser import BrowserFixture, expect


class ProposalAcceptance(BrowserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name in ("approved.txt", "conflicted.txt", "another.txt"):
            (cls.project / name).write_text("原文\n", encoding="utf-8")
        (cls.project / "large.txt").write_text("字" * 8193, encoding="utf-8")
        cls.codex_binary.write_text(f"#!{sys.executable}\n" + """
import json, sys
from pathlib import Path
args = sys.argv
if args[1:3] == ['mcp', 'list']:
    print('[]')
    sys.exit(0)
assert '--output-schema' in args and args[args.index('--sandbox')+1] == 'read-only'
source = json.loads(sys.stdin.read().split('\\n',1)[1])
output = Path(args[args.index('-o') + 1])
output.write_text(json.dumps({'content': source['original'] + source['instruction'] + '\\n'}))
print(json.dumps({'type': 'turn.completed'}))
""", encoding="utf-8")

    def target(self, name):
        self.page.locator("#tree").get_by_text(name, exact=True).click()
        expect(self.page.locator("#fileTitle")).to_have_text(name)
        expect(self.page.locator("#editFile")).to_be_visible()
        expect(self.page.locator("#fileSync")).to_be_empty()
        self.page.locator("#provider").select_option("proposal")

    def test_approve_lost_receipts_and_draft_survives(self):
        page = self.page
        self.target("approved.txt")
        instruction = "追加一行 <b>可核对的文字</b>"
        page.locator("#prompt").fill(instruction)
        submissions = []
        decisions = []

        def drop_receipts(route):
            body = route.request.post_data_json or {}
            seen = {"start": submissions, "decideProposal": decisions}.get(body.get("op"))
            if seen is None:
                route.continue_()
                return
            result = route.fetch()
            seen.append((body, result.json()))
            if len(seen) == 1:
                route.abort("failed")
            else:
                route.fulfill(response=result)

        page.route("**/api/rpc", drop_receipts)
        page.locator("#runAgent").click()
        expect(page.locator("#pendingMessage")).to_contain_text("approved.txt")
        self.target("another.txt")
        page.reload(wait_until="domcontentloaded")
        page.locator("#retryStart").click()
        expect(page.locator("#startNotice")).to_be_empty()
        expect(page.locator("#proposalPath")).to_have_text("approved.txt")
        expect(page.locator("#proposalStatus")).to_have_text("等待审核")
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0][1]["data"]["id"], submissions[1][1]["data"]["id"])
        self.assertEqual(submissions[0][0], submissions[1][0])
        self.assertEqual(submissions[0][0]["path"], "approved.txt")
        path = self.project / "approved.txt"
        self.assertEqual(path.read_text(), "原文\n")
        review = self.context.request.post(self.origin + "/api/rpc", data={
            "op": "proposal", "taskId": submissions[0][1]["data"]["id"],
        }).json()["data"]
        expect(page.locator("#proposalDiff")).to_contain_text(instruction)
        diff = page.locator("#proposalDiff").text_content()
        self.assertEqual(diff, review["diff"])
        self.assertIn("+" + instruction + "\n", diff)
        self.assertEqual(page.locator("#proposalDiff b").count(), 0)

        self.target(path.name)
        page.locator("#editFile").click()
        draft = "本地未提交的独立草稿\n"
        page.locator("#fileEditArea").fill(draft)
        # Two immediate clicks should issue only one decision RPC.
        page.locator("#applyProposal").evaluate("button => { button.click(); button.click(); }")
        expect(page.locator("#proposalStatus")).to_contain_text("操作结果尚未确认")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(page.locator("#proposalDiff").text_content(), diff)
        expect(page.locator("#rejectProposal")).to_be_disabled()
        page.locator("#applyProposal").click()
        expect(page.locator("#proposalStatus")).to_have_text("已应用修改")
        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual(path.read_text(), "原文\n" + instruction + "\n")
        expect(page.locator("#editState")).to_contain_text("云端版本已变化")
        expect(page.locator("#fileEditArea")).to_have_value(draft)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#proposalStatus")).to_have_text("已应用修改")
        expect(page.locator("#rejectProposal")).to_be_hidden()
        self.assertEqual(page.locator("#proposalDiff").text_content(), diff)

    def test_revision_conflict_reject_and_explicit_retarget(self):
        page = self.page
        # Typing before the first preview arrives pins this path to this particular read.
        delayed = []
        def delay_selected_file(route):
            request = route.request.post_data_json or {}
            if request.get("op") == "file" and request.get("path") == "conflicted.txt":
                delayed.append(route)
            else:
                route.continue_()
        page.route("**/api/rpc", delay_selected_file)
        page.locator("#tree").get_by_text("conflicted.txt", exact=True).click()
        page.locator("#provider").select_option("proposal")
        page.locator("#prompt").fill("保留一份建议")
        expect(page.locator("#proposalTargetText")).to_contain_text("conflicted.txt")
        expect(page.locator("#runAgent")).to_be_disabled()
        self.assertEqual(len(delayed), 1)
        delayed.pop().continue_()
        expect(page.locator("#runAgent")).to_be_enabled()
        page.unroute("**/api/rpc", delay_selected_file)
        self.target("another.txt")
        expect(page.locator("#proposalTargetText")).to_contain_text("conflicted.txt")
        expect(page.locator("#runAgent")).to_be_disabled()
        self.target("large.txt")
        expect(page.locator("#proposalTargetText")).to_contain_text("24 KiB")
        expect(page.locator("#runAgent")).to_be_disabled()
        self.target("another.txt")
        page.locator("#useProposalFile").click()
        expect(page.locator("#proposalTargetText")).to_contain_text("another.txt")
        self.target("conflicted.txt")
        page.locator("#useProposalFile").click()
        page.locator("#runAgent").click()
        expect(page.locator("#proposalPath")).to_have_text("conflicted.txt")
        expect(page.locator("#proposalDiff")).to_contain_text("保留一份建议")
        expect(page.locator("#proposalStatus")).to_have_text("等待审核")
        diff = page.locator("#proposalDiff").text_content()
        file = self.project / "conflicted.txt"
        file.write_text("另一个编辑器写入的版本\n", encoding="utf-8")
        page.locator("#applyProposal").click()
        expect(page.locator("#proposalStatus")).to_contain_text("仍可查看")
        self.assertEqual(page.locator("#proposalDiff").text_content(), diff)
        page.locator("#rejectProposal").click()
        expect(page.locator("#proposalStatus")).to_have_text("已拒绝修改")
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#proposalStatus")).to_have_text("已拒绝修改")
        expect(page.locator("#applyProposal")).to_be_hidden()
        self.assertEqual(file.read_text(), "另一个编辑器写入的版本\n")

    def test_switch_while_first_preview_is_in_flight(self):
        page = self.page
        held = []

        def delay_original(route):
            body = route.request.post_data_json or {}
            if body.get("op") == "file" and body.get("path") == "conflicted.txt":
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/rpc", delay_original)
        page.locator("#tree").get_by_text("conflicted.txt", exact=True).click()
        page.locator("#provider").select_option("proposal")
        page.locator("#prompt").fill("等待第一个文件时输入的要求")
        page.locator("#tree").get_by_text("another.txt", exact=True).click()
        expect(page.locator("#editFile")).to_be_visible()
        self.assertEqual(len(held), 1)
        held.pop().continue_()
        expect(page.locator("#proposalTargetText")).to_contain_text("conflicted.txt")
        expect(page.locator("#runAgent")).to_be_disabled()
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#useProposalFile")).to_be_visible()
        expect(page.locator("#proposalTargetText")).to_contain_text("conflicted.txt")
        expect(page.locator("#runAgent")).to_be_disabled()
        page.locator("#useProposalFile").click()
        expect(page.locator("#proposalTargetText")).to_contain_text("another.txt")
        expect(page.locator("#runAgent")).to_be_enabled()
