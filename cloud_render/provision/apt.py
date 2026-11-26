"""Resolve a cloud-only clean Ubuntu Noble desktop from signed Canonical APT indexes."""

import hashlib
import os
import re
import subprocess
import urllib.parse

REPO = "https://archive.ubuntu.com/ubuntu"
SECURITY = "https://security.ubuntu.com/ubuntu"
KEYRING = "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
PACKAGES = (
    "base-files",
    "bash",
    "dash",
    "coreutils",
    "libc-bin",
    "python3",
    "gnome-shell",
    "ubuntu-session",
    "gnome-shell-extension-ubuntu-dock",
    "ubuntu-wallpapers-noble",
    "yaru-theme-gnome-shell",
    "yaru-theme-icon",
    "yaru-theme-gtk",
    "fonts-ubuntu",
    "fonts-dejavu-core",
    "adwaita-icon-theme",
    "gnome-themes-extra-data",
    "libgl1-mesa-dri",
    "virgl-server",
    "xvfb",
    "xdotool",
    "x11-utils",
    "xauth",
    "dbus-daemon",
    "gsettings-desktop-schemas",
    "libglib2.0-bin",
    "libgdk-pixbuf2.0-bin",
    "libgtk-3-bin",
    "dconf-gsettings-backend",
)


def resolve(base, keyring):
    if hashlib.file_digest(keyring.open("rb"), "sha256").hexdigest() != KEYRING:
        raise ValueError("unexpected Ubuntu archive keyring")
    base = base.resolve()
    if not re.fullmatch(r"/[a-zA-Z0-9/_-]+", str(base)):
        raise ValueError("temporary path must be representable without APT quoting")

    def child(name):
        return base / name

    for sub in ("conf", "parts", "lists/partial", "cache/archives/partial", "state", "logs"):
        child(sub).mkdir(parents=True, exist_ok=True)
    child("state/status").touch()
    child("ubuntu-archive-keyring.gpg").write_bytes(keyring.read_bytes())
    lines = [
        f"deb [arch=amd64 signed-by={child('ubuntu-archive-keyring.gpg')}] {url} {dist} main universe"
        for url, dist in ((REPO, "noble"), (REPO, "noble-updates"), (SECURITY, "noble-security"))
    ]
    child("sources.list").write_text("\n".join(lines) + "\n")
    config = {
        "Dir::Etc::main": "/dev/null",
        "Dir::Etc::parts": str(child("parts")),
        "Dir::Etc::sourcelist": str(child("sources.list")),
        "Dir::Etc::sourceparts": str(child("conf")),
        "Dir::Etc::trusted": "/dev/null",
        "Dir::Etc::trustedparts": str(child("conf")),
        "Dir::Etc::preferences": "/dev/null",
        "Dir::Etc::preferencesparts": str(child("conf")),
        "Dir::State": str(child("state")),
        "Dir::State::status": str(child("state/status")),
        "Dir::State::lists": str(child("lists")),
        "Dir::Cache": str(child("cache")),
        "Dir::Cache::pkgcache": "",
        "Dir::Cache::srcpkgcache": "",
        "Dir::Log": str(child("logs")),
        "APT::Architecture": "amd64",
        "APT::Install-Recommends": "false",
        "APT::Install-Suggests": "false",
        "Acquire::Languages": "none",
        "Acquire::IndexTargets::deb::DEP-11::DefaultEnabled": "false",
        "Acquire::Retries": "2",
        "Acquire::https::AllowRedirect": "false",
        "Acquire::AllowInsecureRepositories": "false",
        "Acquire::AllowDowngradeToInsecureRepositories": "false",
        "Acquire::Check-Valid-Until": "true",
    }
    child("apt.conf").write_text("".join(f'{key} "{value}";\n' for key, value in config.items()))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(base), "LC_ALL": "C", "APT_CONFIG": str(child("apt.conf"))}
    # Forward standard host network transport settings without credentials in outputs.
    for key in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "no_proxy"):
        if key in os.environ:
            env[key] = os.environ[key]

    def apt(argv):
        result = subprocess.run(
            argv, env=env, cwd=base, capture_output=True, text=True, timeout=240, check=False
        )
        if result.returncode:
            # Diagnostic source is a fixed, public source; omit any proxy paths or usernames.
            reasons = [
                tag
                for tag in (
                    "NO_PUBKEY",
                    "is not signed",
                    "certificate",
                    "TLS",
                    "Unable to locate package",
                    "failed to fetch",
                    "Hash Sum mismatch",
                    "unmet dependencies",
                )
                if tag.lower() in (result.stderr + result.stdout).lower()
            ]
            missing = re.findall(r"Unable to locate package ([a-z0-9.+-]+)", result.stderr)
            raise RuntimeError((argv[0], argv[1], result.returncode, reasons, missing))
        return result.stdout

    apt(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"])
    specs = apt(["apt-get", "--yes", "--print-uris", "--download-only", "install", *PACKAGES])
    rows = []
    for line in specs.splitlines():
        m = re.fullmatch(r"'(https://[^']+)' (\S+) (\d+) (?:\S+)", line)
        if m:
            url, name, size = m.groups()
            u = urllib.parse.urlsplit(url)
            if (
                u.hostname not in ("archive.ubuntu.com", "security.ubuntu.com")
                or not u.path.startswith("/ubuntu/pool/")
                or u.query
                or u.fragment
            ):
                raise ValueError("unexpected package origin")
            parts = urllib.parse.unquote(name).rsplit("_", 2)
            assert len(parts) == 3
            rows.append(
                {
                    "package": parts[0],
                    "version": parts[1],
                    "arch": parts[2][:-4],
                    "url": url,
                    "size": int(size),
                }
            )
    if not rows or not set(PACKAGES).issubset({r["package"] for r in rows}):
        raise ValueError("APT did not produce all source packages")
    # Read once from the just-verified package lists, including archive SHA256s.
    stdin = apt(["apt-cache", "--no-all-versions", "show", *[r["package"] for r in rows]])
    pinned = {}
    for para in stdin.split("\n\n"):
        fields = {
            k: v
            for k, v in re.findall(
                r"^(Package|Version|Architecture|SHA256|Filename|Size|Installed-Size): (.+)$",
                para,
                re.MULTILINE,
            )
        }
        if {"Package", "Architecture", "Version", "SHA256", "Filename", "Size"} <= fields.keys():
            pinned[fields["Package"], fields["Version"], fields["Architecture"]] = fields
    for row in rows:
        ref = pinned[row["package"], row["version"], row["arch"]]
        if (
            not re.fullmatch("[a-f0-9]{64}", ref["SHA256"])
            or int(ref["Size"]) != row["size"]
            or not urllib.parse.unquote(row["url"]).endswith("/" + ref["Filename"])
        ):
            raise ValueError("inconsistent package hashes for " + row["package"])
        row["sha256"] = ref["SHA256"]
        row["installed_bytes"] = int(ref.get("Installed-Size", "0")) * 1024
    return rows
