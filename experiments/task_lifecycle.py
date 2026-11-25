"""Measure a short Git check from local task submission to its observable terminal state."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    sys.path.insert(0, str(args.checkout.resolve() / "cloud"))
    import tasks
    import workspace

    samples = []
    with tempfile.TemporaryDirectory() as directory:
        os.environ["XDG_STATE_HOME"] = str(Path(directory) / "state")
        project = Path(directory) / "project"
        workspace.initialize(project)
        for index in range(args.samples + 1):
            begin = time.monotonic()
            task = tasks.start_task(project, uuid.uuid4().hex, "check", "")
            while task["status"] in ("queued", "running"):
                if time.monotonic() - begin >= 30:
                    raise RuntimeError("Git check did not finish within 30 seconds")
                time.sleep(0.004)
                task = tasks.events(project, task["id"], 0)["task"]
            if task["status"] != "succeeded":
                raise RuntimeError("Git check finished with status " + task["status"])
            if index:  # Exclude the first run, which initializes task storage.
                samples.append(round(1000 * (time.monotonic() - begin), 2))

    print(json.dumps({"samples_ms": samples, "median_ms": statistics.median(samples),
                      "p95_ms": sorted(samples)[math.ceil(0.95 * len(samples)) - 1]}))


if __name__ == "__main__":
    main()
