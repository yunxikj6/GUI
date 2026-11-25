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

from cloud import tasks, workspace


WORKER = Path(__file__).resolve().parents[1] / "cloud" / "workspace.py"


class OutputPageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.root.mkdir()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root.parent / "state")})
        env.start()
        self.addCleanup(env.stop)

    def create(self, key="job", prompt=""):
        with tasks._database(self.root) as db, db:
            db.execute("""
                INSERT INTO task (id, requestId, provider, prompt, status, createdAt, pid, processKey)
                VALUES (?, ?, 'codex', ?, 'running', '2026-01-01', ?, ?)
            """, (key, key, prompt, os.getpid(), tasks._process_key(os.getpid())))

    def insert(self, key, rows):
        with tasks._database(self.root) as db, db:
            db.executemany("INSERT INTO event (taskId, seq, kind, text) VALUES (?, ?, ?, ?)",
                           ((key, seq, kind, text) for seq, kind, text in rows))

    def rpc(self, key, **params):
        run = subprocess.run([sys.executable, str(WORKER), "--stdin"],
                             input=json.dumps({"op": "events", "taskId": key, **params}).encode(),
                             env={**os.environ, "CLOUD_WORKSPACE_ROOT": str(self.root)},
                             capture_output=True, check=True, timeout=10)
        return json.loads(run.stdout)

    def test_raw_and_packed_boundaries_on_running_task_with_new_appends(self):
        self.create()
        self.assertEqual(tasks.event_window(self.root, "job")["events"], [])
        self.insert("job", ((i, "output", f"log-{i}") for i in range(1, 527)))
        tail = self.rpc("job", tail=True)["data"]
        self.assertTrue(tail["hasEarlier"])
        self.assertFalse(tail["hasMore"])
        self.assertEqual(tail["cursor"], tail["events"][-1]["seq"])
        self.assertEqual([e["text"] for e in tail["events"]], [f"log-{i}" for i in range(367, 527)])
        live = self.rpc("job", after=365)["data"]
        self.assertNotIn("hasEarlier", live)
        self.assertEqual(live["events"][1]["seq"], 367)
        older = self.rpc("job", before=367)["data"]
        self.assertEqual(older, self.rpc("job", before=tail["events"][0]["seq"])["data"])
        self.assertEqual(older["events"][-1]["text"], "log-366")

        self.insert("job", [(527, "status", "new")])
        self.assertEqual(older, tasks.event_window(self.root, "job", 367))
        appended = tasks.events(self.root, "job", tail["cursor"])
        self.assertEqual([e["text"] for e in appended["events"]], ["new"])
        pages = [tail, older]
        while pages[-1]["hasEarlier"]:
            pages.append(tasks.event_window(self.root, "job", pages[-1]["events"][0]["seq"]))
        self.assertEqual([len(page["events"]) for page in pages], [160, 160, 160, 46])
        self.assertEqual([e["text"] for p in reversed(pages) for e in p["events"]],
                         [f"log-{i}" for i in range(1, 527)])
        first = pages[-1]["events"][0]["seq"]
        empty = tasks.event_window(self.root, "job", first)
        self.assertEqual((empty["events"], empty["hasEarlier"], empty["cursor"]), ([], False, 0))

    def test_legacy_event_chunks_preserve_content_and_resume_forward_at_every_window(self):
        self.create(prompt="\x01" * 32000)
        # The first CR/LF pair straddles the fixed character limit. Later newlines
        # produce variable-size chunks, with both multi-byte and JSON-escaped content.
        original = (("文" * 8191 + "\r\n") + "🟢" * 700 + "\n" + "\x01" * 20000 + "\r\n") * 10 + "尾"
        self.insert("job", [(1, "status", "start"), (2, "output", original), (3, "status", "end")])
        expected = [("status", "start"), *(("delta", text) for text in tasks._chunks(original)), ("status", "end")]
        self.assertEqual("".join(text for kind, text in expected if kind == "delta"), original)
        self.assertFalse(any(text.endswith("\r") for kind, text in expected if kind == "delta"))
        pages = [tasks.event_window(self.root, "job")]
        while pages[-1]["hasEarlier"]:
            pages.append(tasks.event_window(self.root, "job", pages[-1]["events"][0]["seq"]))
        self.assertGreater(len(pages), 4)
        offset = 0
        for page in reversed(pages):
            data = page["events"]
            self.assertTrue(data)
            self.assertEqual([(e["kind"], e["text"]) for e in data], expected[offset:offset + len(data)])
            seqs = [e["seq"] for e in data]
            self.assertEqual(seqs, sorted(set(seqs)))
            self.assertTrue(all(seq < 1 << 53 for seq in seqs))
            self.assertLessEqual(tasks._wire_size(page), 256 * 1024)
            self.assertLessEqual(tasks._wire_size(data), 64 * 1024)
            offset += len(data)
            forward = tasks.events(self.root, "job", page["cursor"])["events"]
            self.assertEqual([(e["kind"], e["text"]) for e in forward], expected[offset:offset + len(forward)])
            if offset < len(expected):
                self.assertTrue(forward)
        self.assertEqual(offset, len(expected))
        self.assertEqual([e["text"] for e in tasks.event_window(self.root, "job", 2)["events"]], ["start"])
        self.assertEqual([e["text"] for e in tasks.events(self.root, "job", 2)["events"]], ["end"])

    def test_deep_and_tail_queries_seek_in_the_event_primary_key(self):
        self.create()
        self.insert("job", ((i, "status", "x") for i in range(1, 24001)))
        open_db = tasks._database
        queries, steps = [], []

        @contextmanager
        def observe(root):
            with open_db(root) as db:
                db.set_trace_callback(lambda sql: queries.append(sql) if "ORDER BY seq DESC" in sql else None)
                db.set_progress_handler(lambda: steps.append(1), 100)
                yield db

        with patch.object(tasks, "_database", observe):
            tail = tasks.event_window(self.root, "job")
            first = tasks.event_window(self.root, "job", 161)
        self.assertTrue(tail["hasEarlier"])
        self.assertEqual(len(first["events"]), 160)
        self.assertFalse(first["hasEarlier"])
        self.assertEqual(len(queries), 2)
        self.assertLess(len(steps), 120)
        with open_db(self.root) as db:
            for sql in queries:
                plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql))
                self.assertIn("SEARCH event USING COVERING INDEX", plan)
                self.assertNotIn("TEMP B-TREE", plan)

    def test_append_during_tail_snapshot_is_delivered_by_the_forward_cursor(self):
        self.create()
        self.insert("job", ((i, "delta", str(i)) for i in range(1, 201)))
        ready, reading, writing = threading.Event(), threading.Event(), threading.Event()
        def writer():
            with tasks._database(self.root) as db, db:
                ready.set()
                self.assertTrue(reading.wait(5))
                db.set_trace_callback(lambda sql: writing.set() if sql.lstrip().startswith("INSERT INTO event") else None)
                tasks._append(db, "job", "output", "concurrent")

        reverse = tasks._reverse_events
        def observed(*args):
            reading.set()
            self.assertTrue(writing.wait(5))
            yield from reverse(*args)

        with ThreadPoolExecutor(max_workers=1) as pool:
            append = pool.submit(writer)
            self.assertTrue(ready.wait(5))
            with patch.object(tasks, "_reverse_events", observed):
                page = tasks.event_window(self.root, "job")
            append.result(timeout=5)
        self.assertEqual(page["events"][-1]["text"], "200")
        self.assertEqual([e["text"] for e in tasks.events(self.root, "job", page["cursor"])["events"]],
                         ["concurrent"])

    def test_full_window_skips_previous_large_row_and_can_page_into_its_middle(self):
        self.create()
        text = "x" * tasks._CHUNK_CHARS * 205 + "尾"
        self.insert("job", [(1, "output", text), *((i, "status", "short") for i in range(2, 162))])
        reverse_row = tasks._reverse_row
        def only_small_rows(row, before_index=None):
            self.assertNotEqual(row["seq"], 1, "a full tail should not materialize the previous large row")
            return reverse_row(row, before_index)
        with patch.object(tasks, "_reverse_row", only_small_rows):
            tail = tasks.event_window(self.root, "job")
        self.assertEqual(len(tail["events"]), 160)
        self.assertTrue(tail["hasEarlier"])
        cursor = tail["events"][0]["seq"]
        chunks = []
        while True:
            page = tasks.event_window(self.root, "job", cursor)
            chunks[0:0] = page["events"]
            if not page["hasEarlier"]:
                break
            cursor = page["events"][0]["seq"]
        self.assertEqual([event["seq"] for event in chunks],
                         [tasks._packed_seq(1, i) for i in range(206)])
        self.assertEqual("".join(event["text"] for event in chunks), text)

    def test_bad_boundaries_never_return_false_history_or_unsafe_integers(self):
        self.create()
        self.insert("job", [(1, "status", "start")])
        invalid = [
            ({"before": None}, "invalid_cursor"), ({"before": 0}, "invalid_cursor"),
            ({"before": True}, "invalid_cursor"), ({"before": 1 << 53}, "invalid_cursor"),
            ({"before": 2}, "cursor_not_found"), ({"before": tasks._packed_seq(1, 1)}, "cursor_not_found"),
            ({"tail": False}, "invalid_request"), ({"tail": True, "after": 0}, "invalid_request"),
            ({"before": 1, "after": 0}, "invalid_request"), ({"before": 1, "tail": True}, "invalid_request"),
        ]
        for params, code in invalid:
            with self.subTest(params=params):
                self.assertEqual(self.rpc("job", **params)["error"]["code"], code)
        self.assertEqual(self.rpc("other", tail=True)["error"]["code"], "not_found")
        self.insert("job", [(1 << 34, "status", "past packed range")])
        with self.assertRaises(workspace.WorkspaceError) as error:
            tasks.event_window(self.root, "job")
        self.assertEqual(error.exception.code, "cursor_overflow")


if __name__ == "__main__":
    unittest.main()
