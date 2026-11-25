"""Exercise the Linux guest verifier's on-disk lifecycle through its real CLI."""

import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "linux" / "verify_native_desktop.py"


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /tmp ownership semantics")
class VerifyNativeDesktop(unittest.TestCase):
    def setUp(self):
        self.nonce = secrets.token_hex(16)
        self.root = Path("/tmp") / f"cloudworkspace-verify-{os.getuid()}-{self.nonce}"
        self.folder = self.root / "Files"
        self.assertFalse(os.path.lexists(self.root))
        self.root_identity = None
        self.addCleanup(self.remove_owned_fixture)
        private = tempfile.TemporaryDirectory(prefix="native-guest-test-", dir="/tmp")
        self.addCleanup(private.cleanup)
        self.private = Path(private.name)
        runtime = self.private / "runtime"
        runtime.mkdir(mode=0o700)
        self.env = {"PATH": os.defpath, "HOME": str(self.private),
                    "XDG_RUNTIME_DIR": str(runtime), "LC_ALL": "C.UTF-8"}

    def remove_owned_fixture(self):
        if self.root_identity is None or not os.path.lexists(self.root):
            return
        current = self.root.lstat()
        self.assertEqual((current.st_dev, current.st_ino), self.root_identity)
        self.assertTrue(stat.S_ISDIR(current.st_mode))
        shutil.rmtree(self.root)

    def invoke(self, command, nonce=None):
        result = subprocess.run(
            [sys.executable, str(HELPER), command, self.nonce if nonce is None else nonce],
            env=self.env, capture_output=True, text=True, timeout=8,
        )
        try:
            reply = json.loads(result.stdout)
        except ValueError:
            self.fail(f"{command} returned {result.returncode} without JSON: {result.stderr!r}")
        if reply.get("status") in {"prepared", "cleaned"}:
            self.assertEqual(result.returncode, 0, reply)
        return reply

    def prepare(self):
        reply = self.invoke("prepare")
        self.assertEqual(reply["status"], "prepared", reply)
        self.assertEqual(reply["folder"], str(self.folder))
        self.assertEqual(reply["expected"], "Verified-" + self.nonce[:8])
        node = self.root.lstat()
        self.root_identity = (node.st_dev, node.st_ino)

    def test_prepare_is_private_and_refuses_a_second_prepare(self):
        self.prepare()
        for path in (self.root, *self.root.rglob("*")):
            with self.subTest(path=path.name):
                node = path.lstat()
                self.assertEqual(node.st_uid, os.getuid())
                self.assertFalse(stat.S_ISLNK(node.st_mode))
                self.assertEqual(stat.S_IMODE(node.st_mode) & 0o077, 0)
        marker = self.folder / "user-notes.txt"
        marker.write_text("keep the first run")
        reply = self.invoke("prepare")
        self.assertEqual((reply["status"], reply["code"]), ("error", "already_exists"))
        self.assertEqual(marker.read_text(), "keep the first run")

    def test_wrong_nonce_does_not_attach_or_clean_the_original(self):
        self.prepare()
        invalid = self.nonce[:-1] + "Z"
        for command in ("prepare", "prompt", "check", "cleanup"):
            with self.subTest(command=command):
                reply = self.invoke(command, invalid)
                self.assertEqual((reply["status"], reply["code"]), ("error", "invalid_nonce"))
        unrelated = secrets.token_hex(16)
        self.assertEqual(self.invoke("cleanup", unrelated)["status"], "cleaned")
        self.assertTrue(self.folder.is_dir())

    def test_fresh_check_requires_explicit_attachment(self):
        self.prepare()
        for command in ("prompt", "check"):
            with self.subTest(command=command):
                reply = self.invoke(command)
                self.assertEqual((reply["status"], reply["code"]), ("error", "not_attached"))
        self.assertTrue(self.folder.is_dir())

    def test_cleanup_is_idempotent_after_prepare(self):
        self.prepare()
        self.assertEqual(self.invoke("cleanup")["status"], "cleaned")
        self.assertFalse(os.path.lexists(self.root))
        self.assertEqual(self.invoke("cleanup")["status"], "cleaned")

    def test_cleanup_preserves_user_file(self):
        self.prepare()
        written = self.folder / "user's work.txt"
        written.write_bytes(b"\x00user\n")
        self.assertEqual(self.invoke("cleanup")["status"], "preserved")
        self.assertEqual(written.read_bytes(), b"\x00user\n")

    def test_cleanup_preserves_symbolic_link_and_its_target(self):
        self.prepare()
        outside = self.private / "outside.txt"
        outside.write_text("outside")
        link = self.folder / "shortcut"
        link.symlink_to(outside)
        self.assertEqual(self.invoke("cleanup")["status"], "preserved")
        self.assertEqual(link.readlink(), outside)
        self.assertEqual(outside.read_text(), "outside")

    def test_cleanup_preserves_replaced_empty_files_directory(self):
        self.prepare()
        self.folder.rename(self.private / "original")
        self.folder.mkdir(mode=0o700)
        new = self.folder.lstat()
        self.assertEqual(self.invoke("cleanup")["status"], "preserved")
        current = self.folder.lstat()
        self.assertEqual((current.st_dev, current.st_ino), (new.st_dev, new.st_ino))

    def test_cleanup_preserves_symlink_in_place_of_files_directory(self):
        self.prepare()
        self.folder.rename(self.private / "original")
        outside = self.private / "unrelated"
        outside.mkdir()
        (outside / "kept").write_text("external document")
        self.folder.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.invoke("cleanup")["status"], "preserved")
        self.assertEqual(self.folder.readlink(), outside)
        self.assertEqual((outside / "kept").read_text(), "external document")


if __name__ == "__main__":
    unittest.main()
