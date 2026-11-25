import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"


@unittest.skipUnless(sys.platform.startswith("linux"), "requires a Linux filesystem")
class TreePageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()

    def rpc(self, *, root=None, **parameters):
        request = json.dumps({"op": "tree", **parameters}).encode()
        env = {**os.environ, "CLOUD_WORKSPACE_ROOT": str(root or self.root),
               "XDG_STATE_HOME": str(self.base / "state"), "PYTHONDONTWRITEBYTECODE": "1"}
        run = subprocess.run([sys.executable, str(WORKER), "--stdin"], env=env,
                             input=request, capture_output=True, timeout=15, check=True)
        self.assertEqual(run.stderr, b"")
        self.assertLessEqual(len(run.stdout.removesuffix(b"\n")), 256 * 1024)
        return json.loads(run.stdout)

    def page(self, **parameters):
        response = self.rpc(**parameters)
        self.assertTrue(response["ok"], response.get("error"))
        data = response["data"]
        self.assertEqual(data["path"], parameters.get("path", ""))
        self.assertLessEqual(len(data["entries"]), 400)
        cursor = data["nextCursor"]
        if cursor is not None:
            self.assertIsInstance(cursor, str)
            self.assertTrue(cursor)
            self.assertTrue(data["entries"])
        self.assertIs(data["truncated"], cursor is not None)
        return data

    def pages(self, path):
        parameters = {"path": path}
        seen = set()
        while True:
            page = self.page(**parameters)
            yield page
            cursor = page["nextCursor"]
            if cursor is None:
                return
            self.assertNotIn(cursor, seen)
            seen.add(cursor)
            parameters["cursor"] = cursor

    def invalid(self, **parameters):
        response = self.rpc(**parameters)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_cursor")
        self.assertTrue(response["error"]["message"])

    def test_empty_exact_page_and_single_overflow(self):
        empty = self.page()
        self.assertEqual(empty["entries"], [])
        self.assertIsNone(empty["nextCursor"])
        names = [f"v{i:03}" for i in range(400)]
        for name in reversed(names):
            (self.root / name).touch()
        exact = self.page()
        self.assertEqual([item["name"] for item in exact["entries"]], names)
        self.assertIsNone(exact["nextCursor"])

        last = self.root / "v400"
        last.touch()
        start = self.page()
        self.assertEqual(start["entries"], exact["entries"])
        cursor = start["nextCursor"]
        overflow = self.page(cursor=cursor)
        self.assertEqual([item["name"] for item in overflow["entries"]], [last.name])
        self.assertIsNone(overflow["nextCursor"])
        last.unlink()
        exhausted = self.page(cursor=cursor)
        self.assertEqual(exhausted["entries"], [])
        self.assertIsNone(exhausted["nextCursor"])

    def test_total_order_across_unicode_collisions_and_three_pages(self):
        directory = self.root / "source"
        directory.mkdir()
        dirs = ["zebra", "Último", "ZEBRA"]
        group = ["Straße", "STRASSE", "Strasse", "straße"]
        files = [f"0-{i:03}" for i in range(396)] + group + ["Σ", "ς", "σ"]
        files += [f"z-{i:03}" for i in range(415)]
        for name in reversed(files):
            (directory / name).touch()
        for name in dirs:
            (directory / name).mkdir()
        expected = sorted(dirs + files, key=lambda name: (name not in dirs, name.casefold(), name))
        pages = list(self.pages("source"))
        self.assertEqual([len(page["entries"]) for page in pages], [400, 400, len(expected) - 800])
        self.assertEqual(pages[0]["entries"][-1]["name"], sorted(group)[0])
        self.assertEqual(pages[1]["entries"][0]["name"], sorted(group)[1])
        entries = [item for page in pages for item in page["entries"]]
        self.assertEqual([item["name"] for item in entries], expected)
        self.assertEqual([item["path"] for item in entries], ["source/" + name for name in expected])

    def test_live_seek_after_anchor_deleted_or_renamed(self):
        for mutation in ("delete", "rename"):
            with self.subTest(mutation=mutation):
                directory = self.root / mutation
                directory.mkdir()
                for i in range(405):
                    (directory / f"v{i:03}").touch()
                start = self.page(path=mutation)
                anchor = directory / start["entries"][-1]["name"]
                self.assertEqual(anchor.name, "v399")
                if mutation == "delete":
                    anchor.unlink()
                else:
                    anchor.rename(directory / "v800")
                (directory / "v001").unlink()
                (directory / "v401").unlink()
                (directory / "a-new").touch()
                (directory / "v399-new").touch()
                end = self.page(path=mutation, cursor=start["nextCursor"])
                expected = ["v399-new", "v400", "v402", "v403", "v404"]
                if mutation == "rename":
                    expected.append("v800")
                self.assertEqual([item["name"] for item in end["entries"]], expected)
                self.assertIsNone(end["nextCursor"])

    def test_cursor_scope_and_replaced_directory_and_root(self):
        nested = self.root / "nested"
        nested.mkdir()
        for i in range(401):
            (nested / f"x{i:03}").touch()
        cursor = self.page(path="nested")["nextCursor"]
        (self.root / "other").mkdir()
        self.invalid(path="other", cursor=cursor)
        (self.base / "second" / "nested").mkdir(parents=True)
        self.invalid(root=self.base / "second", path="nested", cursor=cursor)

        held = self.root / "held"
        nested.rename(held)
        nested.mkdir()
        self.invalid(path="nested", cursor=cursor)

        previous_root = self.base / "previous-root"
        self.root.rename(previous_root)
        self.root.mkdir()
        (previous_root / "held").rename(nested)
        self.invalid(path="nested", cursor=cursor)

    def test_malformed_cursor_returns_structured_error(self):
        for token in (None, False, 1, {}, [], "", "@invalid!", "x" * 16384):
            with self.subTest(token_type=type(token).__name__, size=len(str(token))):
                self.invalid(cursor=token)

    def test_visibility_file_types_and_byte_sizes(self):
        nested = self.root / "source"
        nested.mkdir()
        for name in (".git", ".cloudwork", ".cloudwork-save-old", "visible-dir"):
            (nested / name).mkdir()
        (nested / ".cloudwork-save-temp").touch()
        regular = {".env": b"x", ".cloudwork-other": b"y", "empty": b"", "文.txt": "中🟢".encode()}
        for name, content in regular.items():
            (nested / name).write_bytes(content)
        (nested / "linked-file").symlink_to(nested / ".env")
        (nested / "linked-dir").symlink_to(self.base)
        (nested / "dangling").symlink_to("absent")
        os.mkfifo(nested / "fifo")
        with socket.socket(socket.AF_UNIX) as local:
            local.bind(str(nested / "socket"))
            data = self.page(path="source")
        expected = {"visible-dir": ("dir", 0), **{name: ("file", len(content)) for name, content in regular.items()}}
        self.assertEqual({e["name"]: (e["type"], e["size"]) for e in data["entries"]}, expected)
        self.assertIsNone(data["nextCursor"])
        self.assertEqual(self.rpc(path="source/linked-dir")["error"]["code"], "invalid_path")

    def test_deep_unicode_paths_and_escaped_names_fit_transport(self):
        components = [f'd{i:02}-' + "文" * 45 + '\t"\\' for i in range(21)]
        relative = "/".join(components)
        directory = self.root / relative
        directory.mkdir(parents=True)
        names = [f"{i:03}-" + "葉" * 42 + '"\\' * 8 + ".txt" for i in range(400)]
        for name in reversed(names):
            (directory / name).touch()
        self.assertGreater(sum(len((relative + "/" + name).encode()) for name in names), 1024 * 1024)
        pages = list(self.pages(relative))
        self.assertGreater(len(pages), 1)
        entries = [item for page in pages for item in page["entries"]]
        self.assertEqual([item["name"] for item in entries], names)
        self.assertEqual([item["path"] for item in entries], [relative + "/" + name for name in names])
