"""Measure fresh Linux tree workers on the same synthetic directory across checkouts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


# Run the regular stdin entry point. VmHWM is this worker's peak resident memory.
WORKER = """
import runpy, sys
from pathlib import Path
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).parent))
runpy.run_path(sys.argv[0], run_name="__main__")
print(next(row.split()[1] for row in Path("/proc/self/status").read_text().splitlines()
           if row.startswith("VmHWM:")), file=sys.stderr)
"""


def run(source, rows, samples, trace):
    path = f"w{rows:05d}"
    width = max(5, len(str(rows - 1)))
    names = [("ftd-" if i < 16 else "ftf-") + f"{i:0{width}d}" for i in range(rows)]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = root / path
        directory.mkdir()
        for name in sorted(names, key=lambda n: hashlib.sha256(n.encode()).digest()):
            child = directory / name
            if name.startswith("ftd-"):
                child.mkdir()
            else:
                child.touch()
        env = {**os.environ, "CLOUD_WORKSPACE_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
        worker = [sys.executable, "-c", WORKER, str(source / "cloud" / "workspace.py"), "--stdin"]
        request = {"op": "tree", "path": path}
        pages = listed = total_bytes = 0
        total_ms = 0
        details = []
        while True:
            elapsed, peaks = [], []
            body = json.dumps(request).encode()
            for _ in range(samples if pages < 2 else 1):
                start = time.perf_counter()
                proc = subprocess.run(worker, input=body, env=env, capture_output=True, timeout=45, check=True)
                elapsed.append((time.perf_counter() - start) * 1000)
                peaks.append(int(proc.stderr.splitlines()[-1]))
            reply = json.loads(proc.stdout)
            if not reply["ok"]:
                raise RuntimeError(reply["error"])
            page = reply["data"]
            page_names = [entry["name"] for entry in page["entries"]]
            end = listed + len(page_names)
            if "nextCursor" in page and page_names != names[listed:end]:
                raise RuntimeError("Paged results differ from fixture order or contents")
            size = len(proc.stdout.rstrip(b"\n"))
            duration = statistics.median(elapsed)
            if pages < 2:
                result = {"entries": len(page_names), "json_bytes": size, "median_ms": round(duration, 2),
                          "peak_rss_kib": max(peaks)}
                if trace:
                    log = root / "tree.strace"
                    subprocess.run(["strace", "-qq", "-f", "-yy", "-e", "trace=%%stat,getdents64",
                                    "-o", str(log), *worker], input=body, env=env, capture_output=True,
                                    timeout=45, check=True)
                    lines = log.read_text().splitlines()
                    result["child_stat_calls"] = sum('"ftf-' in line or '"ftd-' in line for line in lines)
                    result["directory_getdents64_calls"] = sum(
                        "getdents64(" in line and str(directory) + ">" in line for line in lines)
                details.append(result)
            listed = end
            pages += 1
            total_bytes += size
            total_ms += duration
            cursor = page.get("nextCursor")
            if not cursor:
                if not page["truncated"] and listed != rows:
                    raise RuntimeError("Listing ended before all fixture entries were returned")
                break
            if not page_names or cursor == request.get("cursor") or listed >= rows:
                raise RuntimeError("Unexpected continuation cursor")
            request["cursor"] = cursor
        return {"rows": rows, "samples_first_two_pages": samples, "first_pages": details, "pages": pages,
                "reachable_entries": listed, "complete": listed == rows, "total_json_bytes": total_bytes,
                "sum_page_median_ms": round(total_ms, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rows", type=int, default=8000)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--strace", action="store_true", help="Count directory reads and per-entry stat calls separately")
    args = parser.parse_args()
    if args.rows < 1 or args.samples < 1:
        parser.error("--rows and --samples must be positive")
    if args.strace and not shutil.which("strace"):
        parser.error("--strace needs strace in PATH")
    print(json.dumps(run(args.source.resolve(), args.rows, args.samples, args.strace), indent=2))
