"""Undecodable Linux path bytes travel intact through the browser and the cloud exec envelope."""
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import instabox
import launcher
from test_browser import BrowserFixture, expect


RAW = os.fsdecode(b"z-\xff.txt")
REPLACEMENT = "z-\ufffd.txt"


class BrowserFilenameTransport(BrowserFixture):
    samples = (
        (RAW, r"z-\xff.txt", "旧文件名"),
        (REPLACEMENT, REPLACEMENT, "替换符文件名"),
        (r"z-\xff.txt", r"z-\\xff.txt", "字面反斜杠"),
        ("z-\U00010080.txt", "z-\U00010080.txt", "合法补充字符"),
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for filename, _, content in cls.samples:
            (cls.project / filename).write_text(content, encoding="utf-8")

    def test_open_and_save_distinct_byte_and_unicode_filenames(self):
        page = self.page
        tree = page.locator("#tree")
        tree.get_by_text("README.md", exact=True).click()
        expect(page.locator("#preview")).to_contain_text("Local render acceptance fixture")
        for _, label, original in self.samples:
            tree.get_by_role("button", name=label, exact=True).click()
            expect(page.locator("#fileTitle")).to_have_text(label)
            expect(page.locator("#preview")).to_contain_text(original)
            page.locator("#editFile").click()
            page.locator("#fileEditArea").fill(original + "已保存")
            page.locator("#saveFile").click()
            expect(page.locator("#editState")).to_contain_text("已保存")
        for filename, _, original in self.samples:
            self.assertEqual((self.project / filename).read_text(encoding="utf-8"), original + "已保存")


class CloudFilenameTransport(unittest.TestCase):
    def rpc(self, op, **fields):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request("POST", "/api/rpc", json.dumps({"op": op, **fields}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 200, raw)
            reply = json.loads(raw)
            self.assertTrue(reply["ok"], reply)
            return reply["data"], raw
        finally:
            conn.close()

    def test_exec_stdin_pagination_preview_and_save_use_lossless_utf8_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for index in range(399):
            (root / f"a{index:03d}.txt").write_text("普通文件", encoding="utf-8")
        (root / RAW).write_text("原始字节", encoding="utf-8")
        (root / REPLACEMENT).write_text("实际替换符", encoding="utf-8")
        sent = []

        def cloud_open(request, timeout):
            if request.get_method() == "GET":
                return io.BytesIO(b'{"machines":[{"id":"m_test"}]}')
            self.assertEqual(request.full_url, instabox.InstaBox.base + "/v1/machines/m_test/exec")
            outer = json.loads(request.data)
            # The provider sends this string to a guest as strict UTF-8.
            stdin = outer["stdin"].encode("utf-8")
            sent.append((json.loads(stdin), stdin, request.data))
            worker = subprocess.run([sys.executable, *outer["argv"][1:]], input=stdin,
                                    env={**os.environ, **outer["envs"]}, capture_output=True, timeout=10)
            body = {"exitCode": worker.returncode, "stdout": worker.stdout.decode("utf-8")}
            return io.BytesIO(json.dumps(body).encode("ascii"))

        opener = patch("urllib.request.build_opener")
        opener.start().return_value.open.side_effect = cloud_open
        self.addCleanup(opener.stop)
        box = instabox.InstaBox("fixture", project=str(root))
        box.worker = str(launcher.APP / "cloud" / "workspace.py")
        self.server = launcher.LocalServer(("127.0.0.1", 0), launcher.Handler)
        self.addCleanup(self.server.server_close)
        self.server.workspace = box
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

        first, first_wire = self.rpc("tree", path="")
        self.assertEqual((len(first["entries"]), first["entries"][-1]["path"]), (400, RAW))
        self.assertIn(b"\\udcff", first_wire)
        second, _ = self.rpc("tree", path="", cursor=first["nextCursor"])
        self.assertEqual([entry["path"] for entry in second["entries"]], [REPLACEMENT])
        for name, expected in (("a000.txt", "普通文件"), (RAW, "原始字节"), (REPLACEMENT, "实际替换符")):
            file, wire = self.rpc("file", path=name)
            self.assertEqual((file["path"], file["content"]), (name, expected))
            self.assertIn(expected.encode(), wire)
            self.rpc("save", path=name, revision=file["revision"], content=expected + "已保存")
            self.assertEqual((root / name).read_text(encoding="utf-8"), expected + "已保存")
        _, inner, outer = next(r for r in sent if r[0]["op"] == "save" and r[0]["path"] == RAW)
        self.assertIn(b"\\udcff", inner)
        self.assertIn(b"\\\\udcff", outer)
        self.assertIn("原始字节已保存".encode(), inner)
        self.assertIn("原始字节已保存".encode(), outer)
