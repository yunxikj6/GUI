"""Measure task history on a fixed local SQLite fixture, including fresh worker startup."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
PAGE_SIZE = 80


def task_id(index):
    return f"{index:032x}"


def seed(database, count):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(database) as db:
        for index in range(1, count + 1):
            # Four records in every millisecond exercise the rowid tie-breaker.
            stamp = (start + timedelta(milliseconds=(index - 1) // 4)).isoformat(timespec="milliseconds")
            provider = ("codex", "claude", "check", "check")[(index - 1) % 4]
            prompt = "" if provider == "check" else f"第 {index} 项 " + "核对工作区中的中文记录。" * 18
            key = task_id(index)
            db.execute("""
                INSERT INTO task (id,requestId,provider,prompt,status,createdAt,finishedAt)
                VALUES (?, ?, ?, ?, 'succeeded', ?, ?)
            """, (key, task_id(count + index), provider, prompt, stamp, stamp))
            db.execute("INSERT INTO task_control(taskId) VALUES (?)", (key,))
            db.execute("INSERT INTO event VALUES (?, 1, 'status', '任务已完成。')", (key,))
            if provider in ("codex", "claude"):
                parent = task_id(index - 4) if (index - 1) // 4 % 2 else None
                db.execute(f"INSERT INTO {provider}_turn VALUES (?, ?, ?)", (key, parent, parent or key))


def worker(source, env, request):
    data = json.dumps(request).encode()
    begin = time.perf_counter()
    result = subprocess.run([sys.executable, str(source), "--stdin"], input=data, capture_output=True,
                            check=True, env=env)
    elapsed = (time.perf_counter() - begin) * 1000
    reply = json.loads(result.stdout)
    if not reply["ok"]:
        raise RuntimeError(reply["error"])
    return elapsed, len(result.stdout), reply["data"]


def statistics_ms(values):
    return {"median_ms": round(statistics.median(values), 3),
            "p95_ms": round(sorted(values)[math.ceil(0.95 * len(values)) - 1], 3)}


def measure_page_sql(project, database, before, samples):
    # Trace the shipping query so measurements and EXPLAIN use its actual projection and joins.
    sys.path.insert(0, str(REPO / "cloud"))
    import tasks

    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    with patch.object(sqlite3, "connect", traced):
        if before is None:
            tasks.task_page(project)
        else:
            tasks.task_page(project, before)
    queries = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT ")]
    if not queries:
        raise RuntimeError("No history SELECTs captured")
    with connect(database) as db:
        plan = [[row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)] for sql in queries]
        times = []
        for run in range(samples + 1):
            begin = time.perf_counter()
            for sql in queries:
                db.execute(sql).fetchall()
            if run:
                times.append((time.perf_counter() - begin) * 1000)
    return {**statistics_ms(times), "plan": plan}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=8000)
    parser.add_argument("--samples", type=int, default=21)
    parser.add_argument("--baseline", default="ac9c08e9", help="Git revision used by the original first-page worker")
    args = parser.parse_args()
    if args.rows < 2 * PAGE_SIZE or args.samples < 1:
        parser.error("at least 160 rows and one sample are required")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        original = directory / "baseline"
        original.mkdir()
        archive = directory / "baseline.tar"
        subprocess.run(["git", "-C", str(REPO), "archive", "-o", str(archive), args.baseline, "cloud"], check=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(original)], check=True)
        project = directory / "project"
        project.mkdir()
        state = directory / "state"
        os.environ["XDG_STATE_HOME"] = str(state)
        # Warm both revisions under the same cache policy, including the archived baseline.
        env = {**os.environ, "CLOUD_WORKSPACE_ROOT": str(project), "PYTHONPYCACHEPREFIX": str(directory / "pycache")}
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        first = {"op": "tasks"}
        old = original / "cloud" / "workspace.py"
        current = REPO / "cloud" / "workspace.py"
        worker(old, env, first)
        database, = state.glob("cloudwork/*/tasks.sqlite")
        seed(database, args.rows)
        # The cursor is the last task of the preceding page. Time a full page near the end.
        offset = (args.rows // PAGE_SIZE - 1) * PAGE_SIZE
        before = task_id(args.rows - offset + 1)
        deep = {"op": "tasks", "beforeTaskId": before}
        cases = [("baseline_first", old, first, 0), ("current_first", current, first, 0),
                 ("current_deep", current, deep, offset)]
        times = {name: [] for name, *_ in cases}
        sizes = {}
        for run in range(args.samples + 1):
            for name, source, request, skip in cases[run % 3:] + cases[:run % 3]:
                duration, size, page = worker(source, env, request)
                expected = list(range(args.rows - skip, args.rows - skip - PAGE_SIZE, -1))
                if [task["id"] for task in page["tasks"]] != [task_id(i) for i in expected]:
                    raise RuntimeError(f"{name} did not return the expected ordered page")
                if run:
                    times[name].append(duration)
                sizes[name] = size
        result = {"baseline": args.baseline, "rows": args.rows, "deep_offset": offset, "samples": args.samples,
                  "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                  "worker": {name: {**statistics_ms(values), "stdout_bytes": sizes[name]}
                             for name, values in times.items()},
                  "current_first_selects": measure_page_sql(project, database, None, args.samples),
                  "current_deep_selects": measure_page_sql(project, database, before, args.samples)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
