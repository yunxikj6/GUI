import subprocess
import unittest
from unittest.mock import Mock, patch

from linux import native_file_picker as picker


def item(name, kind="file", size=7, **fields):
    return {"name": name, "type": kind, "size": size, **fields}


def page(*entries, cursor=None):
    return {"entries": entries, "truncated": bool(cursor), "nextCursor": cursor}


class PickerTests(unittest.TestCase):
    def run_picker(self, pages, answers):
        rpc = Mock(side_effect=pages)
        answers = iter(answers)
        seen = []

        def dialog(args, **kwargs):
            seen.append((args, kwargs["input"]))
            answer = next(answers)
            return subprocess.CompletedProcess(args, 1 if answer is None else 0,
                                               stdout="" if answer is None else answer + "\n", stderr="")

        with patch.object(picker.subprocess, "run", side_effect=dialog):
            result = picker.choose_file(rpc, "test-machine", "/data/项目<&>")
        calls = [call.args for call in rpc.call_args_list]
        self.assertTrue(all(parts[:2] == ("test-machine", "/data/项目<&>") for parts in calls))
        return result, [parts[2] for parts in calls], seen

    def test_opaque_ids_keep_exact_filenames_separate_from_control_rows(self):
        name = "--help\t|line\n<b>标签 & '<i>\r\u202e\\n "
        entries = [item("云<&>", "dir"), item("上一级"), item("下一页"), item(name), item("不解码\udcff")]
        with self.subTest("data never interpreted as options"):
            got, calls, seen = self.run_picker([page(*entries, cursor="more")], ["3"])
            self.assertEqual(got, name)
            self.assertEqual(calls, [{"op":"tree","path":""}])
            args, cells = seen[0]
            self.assertIn("--hide-column=1", args)
            self.assertIn("--print-column=1", args)
            self.assertIn("--no-markup", args)
            self.assertTrue(all(name not in arg for arg in args))
            self.assertIn("/data/项目&lt;&amp;&gt;", next(x for x in args if x.startswith("--text=")))
            self.assertEqual(len(cells.splitlines()), 3 * (len(entries) + 1))
            self.assertNotIn("\udcff", cells)
            self.assertIn("\\u202e", cells)
            self.assertIn("\\x20", cells)
        got, _, _ = self.run_picker([page(*entries, cursor="more")], ["2"])
        self.assertEqual(got, "下一页")

    def test_forward_page_return_to_first_and_parent(self):
        root = page(item("dir", "dir"), item("chosen.txt"))
        one = page(item("old"), cursor="opaque-1")
        two = page(item("more"))
        got, calls, _ = self.run_picker([root, one, two, one, root], ["0","2","1","0","1"])
        self.assertEqual(got, "chosen.txt")
        self.assertEqual(calls, [
            {"op":"tree","path":""}, {"op":"tree","path":"dir"},
            {"op":"tree","path":"dir","cursor":"opaque-1"}, {"op":"tree","path":"dir"}, {"op":"tree","path":""},
        ])

    def test_select_400th_and_401st_with_bounded_native_rows(self):
        first_page = page(*(item(f"entry-{i:03d}") for i in range(1, 401)), cursor="next")
        second_page = page(item("entry-401"))
        # Back across the server cursor refreshes the earlier listing. Local pages reuse it.
        for actions, expected, cursors in [
            (["下一页"] * 9 + ["entry-400"], "entry-400", [None]),
            (["下一页"] * 10 + ["entry-401"], "entry-401", [None, "next"]),
            (["下一页"] * 10 + ["上一页", "entry-400"], "entry-400", [None, "next", None]),
            (["下一页"] * 10 + ["返回本目录首页", "entry-001"], "entry-001", [None, "next", None]),
        ]:
            with self.subTest(actions=actions[-2:]):
                rpc = Mock(side_effect=lambda _, __, req: second_page if req.get("cursor") else first_page)
                labels = iter(actions)

                def choose(_, cells):
                    values = cells.splitlines()
                    rows = list(zip(values[::3], values[1::3], values[2::3], strict=True))
                    self.assertLessEqual(sum(kind == "文件" for _, _, kind in rows), 40)
                    wanted = next(labels)
                    return next(idx for idx, label, _ in rows if label == wanted)

                with patch.object(picker, "_dialog", side_effect=choose):
                    self.assertEqual(picker.choose_file(rpc, "machine", "project"), expected)
                self.assertEqual([c.args[2].get("cursor") for c in rpc.call_args_list], cursors)
                self.assertTrue(all(c.args[2]["op"] == "tree" for c in rpc.call_args_list))

    def test_known_limits_keep_dialog_and_do_not_request_file_content(self):
        rows = page(item("limit", size=128*1024), item("big", size=128*1024+1), item("binary", binary=True))
        got, calls, seen = self.run_picker([rows], ["1","2","0"])
        self.assertEqual(got, "limit")
        self.assertEqual(len(calls), 1)
        self.assertIn("超过 128 KiB", next(arg for arg in seen[1][0] if arg.startswith("--text=")))
        self.assertIn("二进制文件", next(arg for arg in seen[2][0] if arg.startswith("--text=")))

    def test_empty_cancel_and_invalidated_cursor(self):
        result, _, seen = self.run_picker([page()], [None])
        self.assertIsNone(result)
        self.assertIn("--info", seen[0][0])
        exc = RuntimeError("stale")
        exc.code = "invalid_cursor"
        got, calls, seen = self.run_picker([page(item("a"),cursor="expired"),exc,page(item("b"))], ["1",None])
        self.assertIsNone(got)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("cursor", calls[-1])
        self.assertIn("已返回本目录首页", next(arg for arg in seen[-1][0] if arg.startswith("--text=")))

    def test_reject_malformed_entry_and_unexpected_output(self):
        for bad in ("..", "/absolute", "dir/file", "\x00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_picker([page(item(bad))], [])
        with self.assertRaises(RuntimeError):
            self.run_picker([page(item("x"))], ["--help"])
