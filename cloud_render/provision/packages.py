"""Build a disposable Ubuntu guest with verified Canonical .deb payloads, in a new private directory."""

import concurrent.futures
import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def unpack(archive, root):
    started = subprocess.Popen(
        ["dpkg-deb", "--fsys-tarfile", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    try:
        with started.stdout as pipe, tarfile.open(fileobj=pipe, mode="r|") as tar:
            for item in tar:
                if item.name in (".", "./"):
                    continue
                parts = PurePosixPath(item.name)
                if parts.is_absolute() or ".." in parts.parts or not parts.parts or len(item.name) > 4096:
                    raise ValueError("unexpected path in signed package")
                if parts.parts[0] in ("dev", "proc", "run", "sys") or item.isdev() or item.isfifo():
                    continue
                if item.issym() and item.linkname.startswith("/"):
                    relative = posixpath.relpath(posixpath.normpath(item.linkname), "/" + str(parts.parent))
                    item = item.replace(linkname=relative)
                if not item.isfile() and not item.isdir() and not item.issym() and not item.islnk():
                    raise ValueError("unknown signed package member type")
                item = item.replace(mode=item.mode & 0o1777)
                tar.extract(item, root, filter="data", set_attrs=not item.isdir())
        if started.wait() != 0:
            raise RuntimeError("dpkg-deb could not unpack verified bytes")
    finally:
        if started.poll() is None:
            started.kill()
            started.wait()


def fetch(row, target):
    parsed = urllib.parse.urlsplit(row["url"])
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ("archive.ubuntu.com", "security.ubuntu.com")
        or not parsed.path.startswith("/ubuntu/pool/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("unexpected Canonical URL")
    if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) or not 0 < row["size"] <= 160 * 1024**2:
        raise ValueError("invalid signed archive metadata")
    urls = [
        row["url"],
        parsed._replace(netloc="us.archive.ubuntu.com").geturl(),
        parsed._replace(
            netloc=("security.ubuntu.com" if parsed.netloc == "archive.ubuntu.com" else "archive.ubuntu.com")
        ).geturl(),
    ]
    for attempt in range(9):
        digest = hashlib.sha256()
        received = 0
        try:
            with (
                urllib.request.build_opener(NoRedirect()).open(urls[attempt % len(urls)], timeout=35) as r,
                target.open("wb") as out,
            ):
                while data := r.read(256 * 1024):
                    received += len(data)
                    if received > row["size"]:
                        raise ValueError("package exceeds announced size")
                    out.write(data)
                    digest.update(data)
            if received != row["size"] or digest.hexdigest() != row["sha256"]:
                raise ValueError("package digest mismatch")
            return target
        except OSError as exc:
            if attempt == 8:
                raise RuntimeError(
                    (row["package"], row["version"], getattr(exc, "code", type(exc).__name__))
                ) from None
            time.sleep(min(0.35 * 2**attempt, 4))


def build(rows, target):
    if target.exists() or target.is_symlink():
        raise ValueError("guest target must be a new directory")
    if (
        not 0 < len(rows) < 2000
        or shutil.disk_usage(target.parent).free < sum(r["installed_bytes"] for r in rows) + 500 * 1024**2
    ):
        raise ValueError("insufficient free disk or unexpected package count")
    target.mkdir(mode=0o700)
    started = time.monotonic()
    try:
        # Ubuntu usrmerge symlinks are root-relative inside this single guest.
        for folder, dest in (
            ("bin", "usr/bin"),
            ("sbin", "usr/sbin"),
            ("lib", "usr/lib"),
            ("lib64", "usr/lib64"),
        ):
            (target / dest).mkdir(parents=True, exist_ok=True)
            (target / folder).symlink_to(dest)
        with (
            tempfile.TemporaryDirectory(prefix="verified-debs-", dir=target.parent) as d,
            concurrent.futures.ThreadPoolExecutor(3) as pool,
        ):
            for start in range(0, len(rows), 12):
                batch = rows[start : start + 12]
                tasks = [pool.submit(fetch, r, Path(d) / f"{i}.deb") for i, r in enumerate(batch)]
                for task in tasks:
                    file = task.result()
                    unpack(file, target)
                    file.unlink()
                if start % 60 == 0:
                    print("verified and unpacked", start + len(batch), len(rows), flush=True)
        for directory in ("etc", "dev", "proc", "run", "sys", "tmp", "var/lib/dbus", "home/workspace"):
            (target / directory).mkdir(parents=True, exist_ok=True)
        # Account and bus metadata inside the disposable guest only. The host is never a dpkg target.
        # These alternatives normally come from maintainer scripts. The guest
        # uses the verified Noble executables without running those scripts.
        for alias, implementation in (("sh", "dash"), ("awk", "mawk")):
            binary = target / "usr/bin" / alias
            if not os.path.lexists(binary):
                if not (binary.parent / implementation).is_file():
                    raise ValueError("verified Ubuntu executable missing: " + implementation)
                binary.symlink_to(implementation)
        accounts = {
            "machine-id": "",
            "passwd": f"root:x:0:0:root:/root:/bin/sh\nworkspace:x:{os.getuid()}:{os.getgid()}:Workspace:/home/workspace:/bin/sh\nmessagebus:x:100:101:Message bus:/nonexistent:/usr/sbin/nologin\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n",
            "group": f"root:x:0:\nworkspace:x:{os.getgid()}:\nmessagebus:x:101:\nnogroup:x:65534:\n",
            "hostname": "cloud-workspace\n",
            "hosts": "127.0.0.1 localhost\n::1 localhost\n",
        }
        for name, value in accounts.items():
            f = target / "etc" / name
            if f.is_symlink():
                f.unlink()
            f.write_text(value)
        return {
            "packages": len(rows),
            "packageBytes": sum(r["size"] for r in rows),
            "buildSeconds": round(time.monotonic() - started, 3),
        }
    except BaseException:
        shutil.rmtree(target)
        raise
