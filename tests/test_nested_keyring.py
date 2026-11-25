"""The native desktop's private UI bus and the user's unlocked credential bus stay independent."""
import concurrent.futures
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import workspace_mcp


@unittest.skipUnless(sys.platform.startswith("linux"), "GNOME Secret Service")
class NestedKeyring(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "user runtime"
        self.runtime.mkdir(mode=0o700)
        self.bus = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.bus.close)
        self.bus.bind(str(self.runtime / "bus"))
        self.address = "unix:path=" + str(self.runtime).replace(" ", "%20") + "/bus"
        self.private = "unix:abstract=nested-gnome-synthetic"
        self.user_value = "synthetic-Δ read/write"
        self.env = {
            "XDG_RUNTIME_DIR": str(self.runtime), "DBUS_SESSION_BUS_ADDRESS": self.private,
            "CLOUDWORKSPACE_NESTED_GNOME": "1", "INSTABOX_API_KEY": "unrelated",
            "PATH": os.defpath, "LC_ALL": "C.UTF-8", "PYTHONPATH": str(self.root),
        }
        pkg = self.root / "keyring" / "backends"
        pkg.mkdir(parents=True)
        (pkg.parent / "__init__.py").touch()
        (pkg / "__init__.py").touch()
        (pkg / "SecretService.py").write_text(
            "import os,sys\n"
            "class Keyring:\n"
            " def get_password(self, service, name):\n"
            "  assert 'INSTABOX_API_KEY' not in os.environ\n"
            "  if name == 'dismissed': raise RuntimeError('dismissed')\n"
            "  return os.environ['DBUS_SESSION_BUS_ADDRESS']\n"
            " def set_password(self, service, name, value):\n"
            "  assert value == 'synthetic-Δ read/write'\n"
            "  assert value not in sys.argv and value not in os.environ.values()\n"
            "  assert 'INSTABOX_API_KEY' not in os.environ\n"
        )

    def test_worker_uses_private_pipes_and_preserves_ui_bus_across_parallel_requests(self):
        with patch.dict(os.environ, self.env, clear=True):
            vault = workspace_mcp.system_vault()
            vault.set_password(workspace_mcp.VAULT_SERVICE, "m_synthetic", self.user_value)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                reads = [pool.submit(vault.get_password, workspace_mcp.VAULT_SERVICE, "m_synthetic") for _ in range(5)]
                self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], self.private)
                self.assertTrue(all(read.result() == self.address for read in reads))
            with self.assertRaises(workspace_mcp.BridgeError) as failure:
                workspace_mcp.read_secret("dismissed", "system")
            self.assertEqual(failure.exception.code, "credential_store")
            self.assertNotIn(self.user_value, str(failure.exception))
            self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], self.private)

    def test_unmarked_missing_or_unsafe_socket_keeps_explicit_session(self):
        backend = MagicMock()
        with patch.dict(os.environ, self.env, clear=True), patch.dict(sys.modules, {"keyring.backends.SecretService": backend}):
            os.environ.pop("CLOUDWORKSPACE_NESTED_GNOME")
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
            os.environ["CLOUDWORKSPACE_NESTED_GNOME"] = "1"
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.address + ",guid=1234"
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.private
            self.runtime.chmod(0o755)
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
            self.runtime.chmod(0o700)
            (self.runtime / "bus").rename(self.runtime / "renamed")
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
            (self.runtime / "bus").symlink_to(self.runtime / "renamed")
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
            self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], self.private)

    def test_wslg_shared_runtime_routes_only_marked_sessions_to_the_systemd_user_socket(self):
        uid = os.getuid()
        canonical = Path(f"/run/user/{uid}")
        shared = Path("/mnt/wslg/runtime-dir")
        paths = {canonical: self.runtime, canonical / "bus": self.runtime / "bus"}
        original = Path.lstat

        def inspect(path):
            if path not in paths:
                raise FileNotFoundError(path)
            return original(paths[path])

        backend = MagicMock()
        with patch.dict(os.environ, {**self.env, "XDG_RUNTIME_DIR": str(shared)}, clear=True), \
                patch.object(Path, "lstat", inspect), patch.dict(sys.modules, {"keyring.backends.SecretService": backend}):
            self.assertEqual(workspace_mcp.system_vault().address, f"unix:path={canonical}/bus")
            self.runtime.chmod(0o755)
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
            self.runtime.chmod(0o700)
            os.environ.pop("CLOUDWORKSPACE_NESTED_GNOME")
            self.assertIs(workspace_mcp.system_vault(), backend.Keyring.return_value)
