"""Create a reproducible, credential-free source archive from a reviewed git revision."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

SOURCE = (
    "cloud_render",
    "requirements-render.txt",
    "experiments/application_surface.py",
    "experiments/live_application_surface.py",
    "experiments/gnome_command_stream.py",
    "experiments/original_desktop_bridge.py",
    "experiments/original_desktop_protocol.py",
    "experiments/original_desktop_source.py",
    "experiments/mutter_app",
    "docs/gnome-source.md",
    "docs/thin-client.md",
    "docs/interactive-original-desktop.md",
    "docs/measurements/instabox-ubuntu-bootstrap.json",
)


def build(root, output, revision="HEAD"):
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], cwd=root, text=True
    ).strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        archive = Path(directory) / "source.zip"
        with archive.open("wb") as dest:
            subprocess.run(
                ["git", "archive", "--format=zip", "--prefix=cloud-workspace-server/", commit, "--", *SOURCE],
                cwd=root, stdout=dest, check=True,
            )
        os.replace(archive, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.output, args.revision)
