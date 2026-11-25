from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from cloud import tasks


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)

    def add(self, rows, root=None):
        with tasks._database(root or self.root) as db, db:
            db.executemany("""
                INSERT INTO task (id, requestId, createdAt, prompt, provider, status)
                VALUES (?, ?, ?, ?, 'codex', 'succeeded')
            """, ((key, key, timestamp, prompt) for key, timestamp, prompt in rows))

    def rpc(self, root, **params):
        run = subprocess.run([sys.executable, str(WORKER), "--stdin"],
                             input=json.dumps({"op": "tasks", **params}).encode(),
                             env={**os.environ, "CLOUD_WORKSPACE_ROOT": str(root)},
                             capture_output=True, check=True, timeout=10)
        return json.loads(run.stdout)

    def test_empty_exact_full_and_partial_pages_preserve_legacy_list(self):
        self.assertEqual(self.rpc(self.root)["data"], {"tasks": [], "nextBeforeTaskId": None})
        long_prompt = "\x01" * 160 + "full-prompt-marker" + "文" * 10000
        self.add((str(i), "2026-01-01", long_prompt) for i in range(80))
        exact = self.rpc(self.root)["data"]
        self.assertIsNone(exact["nextBeforeTaskId"])
        self.assertEqual(exact["tasks"], tasks.list_tasks(self.root))
        self.assertEqual(len(exact["tasks"]), 80)
        self.assertTrue(all(row["prompt"] == long_prompt[:160] for row in exact["tasks"]))
        self.assertNotIn("full-prompt-marker", str(exact))
        self.assertLess(tasks._wire_size(exact), 256 * 1024)
        self.add([("latest", "2026-01-02", "")])
        first = self.rpc(self.root)["data"]
        self.assertEqual(first["nextBeforeTaskId"], first["tasks"][-1]["id"])
        last = self.rpc(self.root, beforeTaskId=first["nextBeforeTaskId"])["data"]
        self.assertEqual([t["id"] for t in last["tasks"]], ["0"])
        self.assertIsNone(last["nextBeforeTaskId"])
        self.assertEqual(self.rpc(self.root, beforeTaskId="0")["data"], {"tasks": [], "nextBeforeTaskId": None})

    def test_paging_is_stable_during_writes_and_new_tasks_are_visible_on_refresh(self):
        dates = [f"2026-11-{21 + (i % 3):02d}" for i in range(243)]
        self.add((str(i), day, "") for i, day in enumerate(dates))
        expected = sorted(range(len(dates)), key=lambda i: (dates[i], i), reverse=True)
        first = tasks.task_page(self.root)
        ids = [int(row["id"]) for row in first["tasks"]]
        lengths = [len(ids)]
        gate = threading.Barrier(2)

        def publish():
            gate.wait(timeout=5)
            # Some inserts share the first page's timestamp and have a larger rowid.
            for batch in range(4):
                self.add((f"new-{batch}-{i}", "2026-11-23", "") for i in range(5))

        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(publish)
            cursor = first["nextBeforeTaskId"]
            gate.wait(timeout=5)
            while cursor is not None:
                page = tasks.task_page(self.root, cursor)
                self.assertEqual(page, tasks.task_page(self.root, cursor))
                ids.extend(int(row["id"]) for row in page["tasks"])
                lengths.append(len(page["tasks"]))
                cursor = page["nextBeforeTaskId"]
            writer.result()
        self.assertEqual(lengths, [80, 80, 80, 3])
        self.assertEqual(ids, expected)
        self.assertEqual(tasks.task_page(self.root)["tasks"][0]["id"], "new-3-4")

    def test_invalid_and_other_workspace_cursors_have_distinct_rpc_errors(self):
        for value in (None, "", "   ", 0, True, [], {}, "a" * 129, "a\x00b", "\ud800"):
            with self.subTest(value=repr(value)):
                reply = self.rpc(self.root, beforeTaskId=value)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], "invalid_cursor")
        other = self.base / "other"
        other.mkdir()
        self.add([("foreign-task", "2026-11-23", "private other-project marker")], other)
        reply = self.rpc(self.root, beforeTaskId="foreign-task")
        self.assertEqual(reply["error"]["code"], "cursor_not_found")
        self.assertNotIn("private other-project marker", str(reply))

    def test_deep_tied_anchor_seeks_both_index_ranges_without_sorting_the_history(self):
        self.add((str(i), "2026-01-01" if i < 200 else "2026-01-02", "") for i in range(6800))
        open_database = tasks._database
        summaries = []
        steps = []

        @contextmanager
        def observed(root):
            with open_database(root) as db:
                db.set_trace_callback(lambda sql: summaries.append(sql) if "substr(prompt" in sql else None)
                db.set_progress_handler(lambda: steps.append(1), 100)
                yield db

        with patch.object(tasks, "_database", observed):
            page = tasks.task_page(self.root, "210")
        self.assertEqual([int(row["id"]) for row in page["tasks"]], list(range(209, 129, -1)))
        self.assertEqual(page["nextBeforeTaskId"], "130")
        self.assertEqual(len(summaries), 2)
        # A scan of the >6,500 newer tied rows cost over 100,000 VM instructions
        # on SQLite 3.46. Two seeks here use <10,000 on both 3.46 and 3.53.
        self.assertLess(len(steps), 300)
        with open_database(self.root) as db:
            for sql in summaries:
                plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql))
                self.assertIn("USING INDEX task_created", plan)
                self.assertNotIn("TEMP B-TREE", plan)


if __name__ == "__main__":
    unittest.main()
