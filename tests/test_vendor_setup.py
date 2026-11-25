import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import ssl
from threading import Thread
import unittest
from email.utils import formatdate


SCRIPT = Path(__file__).resolve().parents[1] / "linux" / "setup-vendor-apps.sh"


def make_deb(directory, name, arch="amd64", contents="vendor"):
    source = directory / (name + "-input")
    control = source / "DEBIAN"
    control.mkdir(parents=True)
    control.chmod(0o755)
    (control / "control").write_text(
        f"Package: {name}\nVersion: 1.0\nArchitecture: {arch}\n"
        f"Maintainer: Fixture <test@example.invalid>\nDescription: {contents}\n"
    )
    path = directory / f"{name}.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(source), str(path)],
                   stdout=subprocess.DEVNULL, check=True)
    return path


@unittest.skipUnless(all(shutil.which(command) for command in ("bash", "script", "dpkg-deb")),
                     "requires Bash, util-linux and dpkg")
class VendorSetupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.trace = self.root / "trace"
        self.fake = self.root / "fake"
        self.fake.mkdir()
        for package in ("chatgpt", "claude-desktop"):
            make_deb(self.fake, package)

    def invoke(self, args=(), answers="", terminal=True, overrides=""):
        shell = r'''
source "$SETUP"
platform_arch() { echo amd64; }
check_vendor_files() { echo "check $1" >> "$TRACE"; }
dpkg-query() { [[ -e $HOOK ]] || return 1; echo "install ok installed amd64 1.0"; }
sudo() {
    printf 'sudo %s\n' "$2" >> "$TRACE"
    for arg; do
        case "$arg" in
        DPkg::Pre-Install-Pkgs::=*) printf '%s' "${arg#*=}" > "$HOOK" ;;
        *.deb)
            dpkg-deb -f "$arg" Package >> "$TRACE"
            stat -c %a "$(dirname -- "$arg")" "$arg" "$work" >> "$HOOK.modes"
            printf '%s\n' "$arg" >> "$HOOK.paths" ;;
        esac
    done
}
fetch_https() {
    printf 'HTTPS %s\n' "$1" >> "$TRACE"
    case "$1" in *key.asc) touch "$2" ;; *) cp "$FAKE/chatgpt.deb" "$2" ;; esac
}
download_signed_claude() { cp "$FAKE/claude-desktop.deb" "$1/claude-desktop.deb"; }
''' + overrides + '\nsetup_vendor_apps "$@"'
        cmd = ["bash", "-c", shell, "vendor", *args]
        if terminal:
            cmd = ["script", "-qec", shlex.join(cmd), "/dev/null"]
        env = dict(os.environ, SETUP=str(SCRIPT), TRACE=str(self.trace), FAKE=str(self.fake),
                   HOOK=str(self.root / "hook"), PATH="/usr/bin:/bin")
        return subprocess.run(cmd, env=env, input=answers, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=8)

    def test_empty_cancel_invalid_and_non_interactive_have_no_side_effect(self):
        for args, answer, code, tty in (
            ((), "\n", 130, True), ((), "2\n\n", 130, True), (("--app", "both"), "no\n", 130, True),
            (("none",), "", 0, False), (("--app", "chatgpt"), "", 70, False), (("--app", "x"), "", 64, True),
        ):
            with self.subTest(args=args, answer=answer):
                result = self.invoke(args, answer, tty)
                self.assertEqual(result.returncode, code, result.stdout)
                self.assertFalse(self.trace.exists())

    def test_single_app_confirmation_names_the_selected_ubuntu_package(self):
        for value, name in (("chatgpt", "ChatGPT"), ("claude", "Claude")):
            with self.subTest(app=name):
                result = self.invoke(("--app", value), "\n")
                self.assertEqual(result.returncode, 130)
                self.assertIn(f"将在本机 Ubuntu 中安装 {name}。", result.stdout)
                self.assertNotIn("请选择", result.stdout)
                self.assertFalse(self.trace.exists())

    def test_missing_download_or_signature_tool_stops_before_network(self):
        for tool, app in (("curl", "chatgpt"), ("gpg", "claude")):
            override = f'command() {{ if [[ "$*" == "-v {tool}" ]]; then return 1; fi; builtin command "$@"; }}'
            result = self.invoke((app,), "y\n", overrides=override)
            self.assertEqual(result.returncode, 69, result.stdout)
            self.assertIn(f"未找到 {tool}", result.stdout)
            self.assertFalse(self.trace.exists())

    def test_exact_packages_and_hook_rejects_substituted_same_version(self):
        result = self.invoke((), "3\ny\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.trace.read_text().splitlines(), [
            "HTTPS https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb",
            "HTTPS https://downloads.claude.ai/claude-desktop/key.asc", "check chatgpt", "check claude-desktop",
            "sudo /usr/bin/apt-get", "chatgpt", "claude-desktop",
        ])
        self.assertEqual((self.root / "hook.modes").read_text().splitlines(), ["711", "644", "700"] * 2)
        self.assertTrue(all(not Path(path).parent.exists() for path in (self.root / "hook.paths").read_text().splitlines()))
        hook = (self.root / "hook").read_text()
        genuine = self.fake / "chatgpt.deb"
        substituted = make_deb(self.root, "chatgpt", contents="same name and version, different file")
        dependency = make_deb(self.root, "unrelated")
        for archive, expected in ((genuine, 0), (dependency, 0), (substituted, 1)):
            result = subprocess.run(["/bin/sh", "-c", hook], input=f"{archive}\n", text=True, capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_bad_package_or_failed_download_never_reaches_sudo(self):
        for replacement in ('fetch_https() { cp "$FAKE/claude-desktop.deb" "$2"; }',
                            'fetch_https() { return 42; }'):
            result = self.invoke(("chatgpt",), "Y\n", overrides=replacement)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("sudo", self.trace.read_text() if self.trace.exists() else "")
        mismatch = make_deb(self.root, "chatgpt", arch="arm64")
        result = self.invoke(("chatgpt",), "y\n", overrides='fetch_https() { cp ' + shlex.quote(str(mismatch)) + ' "$2"; }')
        self.assertEqual(result.returncode, 74, result.stdout)
        result = self.invoke(("both",), "y\n", overrides='download_signed_claude() { return 42; }')
        self.assertEqual(result.returncode, 42, result.stdout)
        self.assertNotIn("sudo", self.trace.read_text() if self.trace.exists() else "")

    def test_foreign_key_link_or_edited_file_is_preserved(self):
        original = b"test managed file\n"
        digest = hashlib.sha256(original).hexdigest()
        regular = self.root / "key"
        absent = self.root / "missing"
        modified = self.root / "edited"
        linked = self.root / "symlink"
        regular.write_bytes(original)
        modified.write_bytes(original + b"# local change\n")
        linked.symlink_to(regular)
        for path, code in ((absent, 0), (regular, 0), (modified, 73), (linked, 73)):
            result = subprocess.run(["bash", "-c", 'source "$1"; check_managed_file "$2" "$3"',
                                     "_", str(SCRIPT), str(path), digest], capture_output=True)
            self.assertEqual(result.returncode, code)
        self.assertEqual(modified.read_bytes(), original + b"# local change\n")
        self.assertTrue(linked.is_symlink())

    def test_same_or_newer_version_is_kept_without_sudo(self):
        for version in ("1.0", "1:1.0", "2.0"):
            result = self.invoke(("chatgpt",), "y\n", overrides=f'dpkg-query() {{ echo "install ok installed amd64 {version}"; }}')
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("sudo", self.trace.read_text())
            self.assertNotIn("check chatgpt", self.trace.read_text())

    def test_current_official_one_line_claude_source_is_recognized(self):
        source = self.root / "claude-desktop.list"
        source.write_text("deb [signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] "
                          "https://downloads.claude.ai/claude-desktop/apt/stable stable main\n")
        harness = '''
source "$1"
check_managed_file() {
    if [[ $1 == /etc/apt/sources.list.d/claude-desktop.list ]]; then
        local actual candidate
        actual=$(sha256sum < "$TEST_SOURCE")
        shift
        for candidate; do [[ ${actual%% *} != "$candidate" ]] || return 0; done
        return 73
    fi
}
check_vendor_files claude-desktop amd64
'''
        command = ["bash", "-c", harness, "_", str(SCRIPT)]
        env = dict(os.environ, TEST_SOURCE=str(source))
        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
        source.write_text(source.read_text() + "# local edit\n")
        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 73)

    def test_apt_zero_without_installed_version_does_not_report_success(self):
        for status in ("", "install ok installed amd64 0.9", "install ok installed arm64 1.0", "install ok unpacked amd64 1.0"):
            result = self.invoke(("chatgpt",), "y\n", overrides=f"dpkg-query() {{ echo {shlex.quote(status)}; }}")
            self.assertEqual(result.returncode, 75, result.stdout)
            self.assertNotIn("安装完成。可从", result.stdout)

    @unittest.skipUnless(shutil.which("curl") and shutil.which("openssl"), "requires curl and OpenSSL")
    def test_https_redirects_keep_tls_and_validate_server(self):
        certificate, key = self.root / "fixture.crt", self.root / "fixture.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(certificate)], capture_output=True, check=True)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/package":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b"verified TLS")))
                    self.end_headers()
                    self.wfile.write(b"verified TLS")
                else:
                    self.send_response(302)
                    self.send_header("Content-Length", "0")
                    scheme = "http" if self.path == "/downgrade" else "https"
                    self.send_header("Location", f"{scheme}://127.0.0.1:{self.server.server_port}/package")
                    self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        def fetch(path, ca):
            url = f"https://127.0.0.1:{server.server_port}/{path}"
            return subprocess.run(["bash", "-c", 'source "$1"; fetch_https "$2" "$3"', "_", str(SCRIPT),
                                   url, str(self.root / "download")], env=dict(os.environ, CURL_CA_BUNDLE=str(ca),
                                   NO_PROXY="127.0.0.1", no_proxy="127.0.0.1"), capture_output=True, timeout=5)

        positive = fetch("redirect", certificate)
        self.assertEqual(positive.returncode, 0, positive.stderr)
        self.assertEqual((self.root / "download").read_bytes(), b"verified TLS")
        self.assertNotEqual(fetch("downgrade", certificate).returncode, 0)
        self.assertNotEqual(fetch("package", self.root / "nonexistent.pem").returncode, 0)

    def test_platform_and_both_architectures(self):
        for distro, version, arch, code in (
            ("ubuntu", "24.04", "amd64", 0), ("ubuntu", "24.04", "arm64", 0),
            ("ubuntu", "22.04", "amd64", 65), ("debian", "24.04", "amd64", 65), ("ubuntu", "24.04", "i386", 65),
        ):
            cmd = 'source "$1"; function . { ID=$TEST_ID; VERSION_ID=$TEST_VERSION; }; dpkg() { echo "$TEST_ARCH"; }; platform_arch'
            result = subprocess.run(["bash", "-c", cmd, "_", str(SCRIPT)], capture_output=True, text=True,
                                    env=dict(os.environ, TEST_ID=distro, TEST_VERSION=version, TEST_ARCH=arch))
            self.assertEqual(result.returncode, code, result.stderr)
            if code == 0:
                self.assertEqual(result.stdout.strip(), arch)

    @unittest.skipUnless(shutil.which("gpg") and Path("/usr/bin/apt-get").exists(), "requires GnuPG and APT")
    def test_private_apt_download_authenticates_release_and_payload(self):
        gpg_home = self.root / "sign"
        gpg_home.mkdir(mode=0o700)
        gpg = ["gpg", "--batch", "--homedir", str(gpg_home), "--pinentry-mode", "loopback", "--passphrase", ""]
        subprocess.run([*gpg, "--quick-generate-key", "Local Fixture <test@example.invalid>", "rsa2048", "sign", "1d"],
                       capture_output=True, check=True)
        self.addCleanup(subprocess.run, ["gpgconf", "--homedir", str(gpg_home), "--kill", "all"], capture_output=True)
        fingerprint = next(row.split(":")[9] for row in subprocess.check_output(
            [*gpg, "--with-colons", "--list-keys"], text=True, stderr=subprocess.DEVNULL).splitlines() if row.startswith("fpr:"))
        key = self.root / "fixture.asc"
        key.write_bytes(subprocess.check_output([*gpg, "--armor", "--export", fingerprint]))
        repo = self.root / "repo"
        index_dir = repo / "dists/stable/main/binary-amd64"
        index_dir.mkdir(parents=True)
        archive = self.fake / "claude-desktop.deb"
        shutil.copyfile(archive, repo / archive.name)
        index = subprocess.check_output(["dpkg-deb", "-f", str(archive)]) + (
            f"Filename: {archive.name}\nSize: {archive.stat().st_size}\n"
            f"SHA256: {hashlib.sha256(archive.read_bytes()).hexdigest()}\n\n"
        ).encode()
        (index_dir / "Packages").write_bytes(index)
        (index_dir / "Packages.gz").write_bytes(gzip.compress(index))
        release = repo / "dists/stable/Release"
        release.write_text("Suite: stable\nCodename: stable\nArchitectures: amd64\nComponents: main\n"
                           f"Date: {formatdate(usegmt=True)}\nSHA256:\n" +
                           "".join(f" {hashlib.sha256(p.read_bytes()).hexdigest()} {p.stat().st_size} main/binary-amd64/{p.name}\n"
                                   for p in sorted(index_dir.iterdir())))
        subprocess.run([*gpg, "--armor", "--clearsign", "-o", str(release.with_name("InRelease")), str(release)], check=True)
        def download(signer):
            workdir = Path(tempfile.mkdtemp(dir=self.root))
            (workdir / "gnupg").mkdir(mode=0o700)
            result = subprocess.run(["bash", "-c", 'source "$1"; work=$2; download_signed_claude "$2" amd64 "$3" "$4" "$5"',
                                     "_", str(SCRIPT), str(workdir), repo.as_uri(), signer, str(key)],
                                    env=dict(os.environ, PATH="/usr/bin:/bin"), capture_output=True, text=True, timeout=12)
            return workdir, result
        valid, result = download(fingerprint)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((valid / archive.name).read_bytes(), archive.read_bytes())
        _, wrong_key = download("0" * 40)
        self.assertNotEqual(wrong_key.returncode, 0)
        (repo / archive.name).write_bytes(b"corrupted vendor archive")
        _, corrupt = download(fingerprint)
        self.assertNotEqual(corrupt.returncode, 0)
