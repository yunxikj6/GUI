import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "linux" / "setup-ubuntu.sh"
# The actual installation workflow runs in a pseudo-terminal with simulated dpkg, sudo and apt.
SHELL = r'''
source "$SETUP"
check_platform() { return 0; }
dpkg-query() {
    case " ${MISSING:-} " in
        *" ${!#} "*) return 1 ;;
        *) printf installed ;;
    esac
}
sudo() { shift; "$@"; }
apt-get() {
    printf '%s\n' "$*" >> "$TRACE"
    [[ ${FAIL_AT:-never} != update || ${!#} != update ]] || return 100
    [[ ${FAIL_AT:-never} != install || " $* " != *" install "* ]] || return 100
}
setup --wsl
'''


@unittest.skipUnless(shutil.which("bash") and shutil.which("script"), "requires Bash and util-linux script")
class UbuntuSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.trace = Path(temp.name) / "calls"

    def invoke(self, missing="", response="\n", fail="", terminal=True):
        command = ["bash", "-c", SHELL]
        if terminal:
            command = ["script", "--quiet", "--return", "--command", shlex.join(command), "/dev/null"]
        return subprocess.run(command, env=dict(os.environ, SETUP=str(SCRIPT), TRACE=str(self.trace),
                              MISSING=missing, FAIL_AT=fail), input=response, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)

    def test_cancel_and_already_installed_never_run_apt(self):
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.invoke("nautilus", terminal=False).returncode, 70)
        for answer in ("\n", "n\n"):
            result = self.invoke("nautilus", answer)
            self.assertEqual(result.returncode, 130, result.stdout)
            self.assertIn("nautilus", result.stdout)
        self.assertFalse(self.trace.exists())

    def test_failed_update_and_retry(self):
        result = self.invoke("nautilus gvfs", "y\n", "update")
        self.assertEqual(result.returncode, 71, result.stdout)
        self.assertEqual(len(self.trace.read_text().splitlines()), 1)
        result = self.invoke("nautilus gvfs", "yes\n", "install")
        self.assertEqual(result.returncode, 72, result.stdout)
        result = self.invoke("nautilus gvfs", "Y\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        commands = self.trace.read_text().splitlines()
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[-1].split(), ["-o", "APT::Get::Assume-Yes=false", "--no-remove",
                                                "--no-install-recommends", "install", "nautilus", "gvfs"])

    def test_unsupported_release_and_wrong_arguments(self):
        for distro, version in (("debian", "24.04"), ("ubuntu", "22.04")):
            # Supply only /etc/os-release fields. Production always reads the OS-owned file.
            harness = 'source "$SETUP"; function . { ID="$TEST_ID"; VERSION_ID="$TEST_VERSION"; }; setup --wsl'
            result = subprocess.run(["bash", "-c", harness], env=dict(os.environ, SETUP=str(SCRIPT),
                                    TEST_ID=distro, TEST_VERSION=version), capture_output=True, text=True)
            self.assertEqual(result.returncode, 65)
            self.assertIn("24.04", result.stderr)
        result = subprocess.run(["bash", str(SCRIPT), "--check"], capture_output=True)
        self.assertEqual(result.returncode, 64)
