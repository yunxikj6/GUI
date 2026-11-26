"""Install the pinned original application and its cloud-only observer in a disposable guest."""
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile

from .packages import NoRedirect, unpack

PACKAGES = (
    "grep", "findutils", "sed", "diffutils", "mawk", "procps", "hostname", "util-linux",
    "gnome-keyring", "libsecret-tools", "dbus-x11", "python3-pil", "libasound2t64",
    "libnspr4", "libnss3", "libnotify4", "libcups2t64", "libtss2-esys-3.0.2-0t64",
    "libtss2-mu-4.0.1-0t64", "libtss2-tcti-device0t64", "libxkbcommon-x11-0",
    "xz-utils", "xdg-utils", "mesa-vulkan-drivers", "gcc", "libc6-dev", "libssl-dev",
)
# Both hashes were independently matched to the vendor APT archive and the originating ELF.
PACKAGE = {
    "size": 501426562,
    "sha256": "e8ab38df96d6070fd5d4582a606490637728243bb529e9edae19b3f0a6e664d2",
}
INSTALLED_BYTES = 1560353 * 1024  # Installed-Size in the verified vendor control file.
EXECUTABLE_SHA256 = "b36d94aff6ec46074f9d4ac5f898d1cc449396b661663487fcf2d11e41c502a7"


def checked_copy(source, destination, record):
    digest = hashlib.sha256()
    count = 0
    with destination.open("xb") as output:
        while chunk := source.read(256 * 1024):
            count += len(chunk)
            if count > record["size"]:
                raise ValueError("云端安装文件超过已核对的大小。")
            digest.update(chunk)
            output.write(chunk)
    if count != record["size"] or digest.hexdigest() != record["sha256"]:
        raise ValueError("云端安装文件与指定版本的上游摘要不符。")


def download(record, destination):
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(2):
        try:
            with opener.open(record["url"], timeout=60) as response:
                checked_copy(response, destination, record)
            return
        except OSError:
            destination.unlink(missing_ok=True)
            if attempt:
                raise


def stage(local_deb, directory):
    archive = directory / "verified-chatgpt.deb"
    with local_deb.open("rb") as supplied:
        checked_copy(supplied, archive, PACKAGE)
    return archive


def install(root, archive):
    unpack(archive, root)
    archive.unlink()
    with (root / "usr/lib/chatgpt/ChatGPT").open("rb") as elf:
        if hashlib.file_digest(elf, "sha256").hexdigest() != EXECUTABLE_SHA256:
            raise ValueError("云端应用可执行文件校验失败。")
    with tempfile.TemporaryDirectory(dir=root.parent, prefix="app-download-") as tmp:
        scratch = Path(tmp)
        site = root / "usr/local/lib/python3.12/dist-packages"
        site.mkdir(parents=True, exist_ok=True)
        for wheel in json.loads(Path(__file__).with_name("original-app-wheels.json").read_text()):
            package = scratch / "dependency.whl"
            download(wheel, package)
            with zipfile.ZipFile(package) as verified:
                # These four fixed, checksummed purelib/platlib wheels have no install hooks.
                verified.extractall(site)
            package.unlink()
        (site / "playwright/driver/node").chmod(0o755)
