"""Measure a fresh browser opening a completed task, through the actual loopback launcher."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright


def measure(source, rows, samples, chromium):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        env = {**os.environ, "XDG_STATE_HOME": str(base / "state")}
        with subprocess.Popen([sys.executable, str(source / "launcher.py"), "--local", "--no-browser",
                               "--port", "0", "--project", str(base / "project")],
                              env=env, stdout=subprocess.PIPE, text=True) as server:
            try:
                origin = server.stdout.readline().strip().split()[-1]
                req = Request(origin + "api/rpc", data=b'{"op":"tasks"}', headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=10) as response:
                    assert json.load(response)["ok"]
                db_path, = (base / "state").glob("cloudwork/*/tasks.sqlite")
                with sqlite3.connect(db_path) as db:
                    db.execute("""INSERT INTO task (id,requestId,provider,prompt,status,createdAt)
                                  VALUES ('long-log','probe','check','','succeeded','2026-01-01')""")
                    db.executemany("INSERT INTO event VALUES ('long-log', ?, 'output', ?)",
                                   ((i, f"row[{i:08d}] " + "正在核对项目检查项。 " * 4) for i in range(1, rows + 1)))

                results = []
                with sync_playwright() as pw:
                    with pw.chromium.launch(executable_path=chromium) as browser:
                        version = browser.version
                        for _ in range(samples):
                            with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
                                page = context.new_page()
                                sizes = []

                                def observe(response):
                                    if response.url == origin + "api/rpc" and response.request.post_data_json["op"] == "events":
                                        sizes.append(int(response.headers["content-length"]))

                                page.on("response", observe)
                                started = time.perf_counter()
                                page.goto(origin, wait_until="domcontentloaded")
                                page.wait_for_function("marker => document.getElementById('eventLog').textContent.includes(marker)",
                                                       arg=f"row[{rows:08d}]", timeout=180000)
                                elapsed = round((time.perf_counter() - started) * 1000, 1)
                                in_view = page.locator("#eventLog").evaluate("""log => {
                                    const row = log.lastElementChild.getBoundingClientRect();
                                    const box = log.getBoundingClientRect();
                                    return row.top < Math.min(innerHeight, box.bottom) && row.bottom > Math.max(0, box.top);
                                }""")
                                page.wait_for_load_state("networkidle")
                                results.append({"last_rendered_ms": elapsed, "last_row_in_view": in_view,
                                                "events_requests": len(sizes), "events_body_bytes": sum(sizes)})
                return {"rows": rows, "chromium": version, "samples": results,
                        "median_last_rendered_ms": statistics.median(r["last_rendered_ms"] for r in results)}
            finally:
                server.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rows", type=int, default=8000)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--chromium", default=os.environ.get("CLOUD_WORKSPACE_TEST_CHROMIUM") or shutil.which("chromium"))
    args = parser.parse_args()
    if not args.chromium or args.rows < 1 or args.samples < 1:
        parser.error("Provide a Chromium binary, positive row count and at least one sample")
    print(json.dumps(measure(args.source, args.rows, args.samples, args.chromium), indent=2))
