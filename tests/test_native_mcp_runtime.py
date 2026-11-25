import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from linux import native_mcp_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"HOME": directory.name, "XDG_DATA_HOME": str(self.root / "data")})
        env.start()
        self.addCleanup(env.stop)

    def patch_module(self, name, **kwargs):
        patcher = patch.object(runtime, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_available_interpreter_without_pip_or_packaging_keeps_its_symlink(self):
        # A uv venv can legitimately contain just the distributions the client needs.
        venv = self.root / "already configured"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        python = str(venv / "bin" / "python")
        sites = Path(subprocess.check_output(
            [python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
        for name, body in {
            "mcp/server/lowlevel.py": "class Server: pass",
            "mcp/server/stdio.py": "def stdio_server(): pass",
            "keyring/backends/SecretService.py": "class Keyring: pass",
            "mcp-1.29.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: mcp\nVersion: 1.29.0+local\n",
            "keyring-25.7.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: keyring\nVersion: 25.7.0\n",
        }.items():
            file = sites / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(body)
        subprocess.run([python, "-I", "-c", "import importlib.util as u; assert all("
                        "u.find_spec(n) is None for n in ('pip','packaging'))"], check=True)
        self.patch_module("_external", return_value=(python,))
        self.assertEqual(runtime.find_runtime(python), python)
        self.assertEqual(runtime.prepare_runtime(python), python)
        self.assertFalse(runtime._storage().exists())
        metadata = sites / "mcp-1.29.0.dist-info" / "METADATA"
        old = metadata.read_text()
        for version in ("1.28.9", "2.0", "1.30.0rc1"):
            with self.subTest(version=version):
                metadata.write_text(old.replace("1.29.0+local", version))
                self.assertIsNone(runtime.find_runtime(python))
        metadata.write_text(old)
        (sites / "keyring/backends/SecretService.py").unlink()
        self.assertIsNone(runtime.find_runtime(python))
        self.assertFalse(runtime._storage().exists())

    def test_pip_configuration_only_preserves_download_options(self):
        env = {"PIP_CERT": "/env-ca", "PIP_TARGET": "/unrelated", "PIP_PREFIX": "/other",
               "PIP_CONFIG_FILE": "/explicit", "PIP_ROOT": "/wrong", "PIP_PYTHON": "/wrong",
               "HTTPS_PROXY": "https://proxy.invalid"}
        config = ("global.index-url='https://a.invalid/simple'\n"
                  "install.index-url='https://b.invalid/simple'\nglobal.cert='/file-ca'\n"
                  "global.trusted-host='one.invalid\\ntwo.invalid'\ninstall.target='/should never write'\n")
        result = subprocess.CompletedProcess([], 0, config, "")
        with patch.object(subprocess, "run", return_value=result):
            got = runtime._pip_env("/python", env)
        self.assertEqual(got, {"PIP_CONFIG_FILE": os.devnull, "PIP_CERT": "/env-ca",
                               "PIP_INDEX_URL": "https://b.invalid/simple", "PIP_TRUSTED_HOST": "one.invalid\ntwo.invalid",
                               "HTTPS_PROXY": "https://proxy.invalid"})
        with patch.dict(os.environ, {"INSTABOX_API_KEY": "fixture"}):
            with patch.object(subprocess, "run", return_value=result) as run:
                runtime._usable(sys.executable)
            self.assertNotIn("INSTABOX_API_KEY", run.call_args.kwargs["env"])

    def fake_setup(self, install):
        self.patch_module("_external", return_value=(sys.executable,))
        self.patch_module("_available", side_effect=lambda paths: iter(dict.fromkeys(str(p) for p in paths if p)))
        self.patch_module("_usable", side_effect=lambda path: Path(path + ".works").is_file())
        self.patch_module("_pip_env", return_value={})

        def commands(cmd, env, message, timeout):
            if "venv" in cmd:
                bindir = Path(cmd[-1]) / "bin"
                bindir.mkdir(parents=True)
                (bindir / "python").symlink_to(sys.executable)
            if "install" in cmd:
                install(Path(cmd[0]))

        self.patch_module("_install", side_effect=commands)

    def test_failed_and_interrupted_install_can_retry_and_keep_existing_venvs(self):
        calls = []
        def install(python):
            calls.append(python)
            if len(calls) == 1:
                raise RuntimeError("offline")
            Path(str(python) + ".works").touch()
        self.fake_setup(install)
        store = runtime._storage()
        store.mkdir(mode=0o700, parents=True)
        stale = store / "venv-interrupted"
        stale.mkdir(mode=0o700)
        (stale / ".installing").touch()
        older = store / "venv-previous"
        older.mkdir(mode=0o700)
        (older / ".ready").touch()
        with self.assertRaisesRegex(RuntimeError, "offline"):
            runtime.prepare_runtime()
        self.assertEqual(set(store.iterdir()), {older})
        selected = runtime.prepare_runtime()
        self.assertEqual(selected, str(calls[1]))
        self.assertTrue((older / ".ready").is_file())
        self.assertEqual(runtime.find_runtime(), selected)
        self.assertEqual(runtime.prepare_runtime(), selected)
        self.assertEqual(len(calls), 2)

    def test_concurrent_preparation_publishes_one_complete_venv(self):
        installing, release, waiting = (threading.Event() for _ in range(3))
        installs = []
        def install(python):
            installs.append(python)
            installing.set()
            self.assertTrue(release.wait(5))
            Path(str(python) + ".works").touch()
        self.fake_setup(install)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runtime.prepare_runtime)
            self.assertTrue(installing.wait(5))
            self.assertIsNone(runtime.find_runtime())
            second = pool.submit(runtime.prepare_runtime, progress=lambda msg: waiting.set() if "等待" in msg else None)
            try:
                self.assertTrue(waiting.wait(5))
            finally:
                release.set()
            self.assertEqual(first.result(), second.result())
            self.assertEqual(len(installs), 1)
