"""The marked GNOME 46 verifier sees only the current official Wayland window."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from linux import gnome_session


class AppFocus(unittest.TestCase):
    token = "12" * 16

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mode = self.root / "system/gnome-shell/modes/ubuntu.json"
        self.mode.parent.mkdir(parents=True)
        self.mode.write_text('{"parentMode":"user","enabledExtensions":["ubuntu-dock@ubuntu.com"]}')
        self.child = self.root / "session"
        self.child.mkdir()

    def prepare(self, flag="1", token=None, version="GNOME Shell 46.0", backend="--nested", mode="ubuntu"):
        self.env = {"CLOUDWORKSPACE_VERIFY_APPS": flag, "CLOUDWORKSPACE_VERIFY_ID": self.token if token is None else token,
                    "XDG_DATA_DIRS": str(self.root / "system")}
        return gnome_session.app_observer_env(self.env, self.child, version, backend, mode)

    def test_requires_exact_opt_in_and_nonce(self):
        for token in ("", self.token[:-1], self.token + "\n", "AB" * 16, "0/" * 16):
            with self.subTest(token=token):
                self.assertEqual(self.prepare(token=token), [])
        for flag in ("", "0", "yes"):
            self.assertEqual(self.prepare(flag=flag), ["CLOUDWORKSPACE_VERIFY_ID=" + self.token])
        self.assertEqual(list(self.child.iterdir()), [])

    def test_incompatible_or_malformed_mode_fails_only_for_opt_in(self):
        for override in ({"mode": "user"}, {"backend": "--devkit"}, {"version": "GNOME Shell 48.1"}):
            with self.subTest(override=override), self.assertRaisesRegex(gnome_session.LaunchError, "GNOME Shell 46"):
                self.prepare(**override)
        self.mode.write_text('{}{}')
        with self.assertRaisesRegex(gnome_session.LaunchError, "会话模式"):
            self.prepare()
        self.assertEqual(list(self.child.iterdir()), [])

    def test_only_compositor_receives_private_data_and_original_mode_is_preserved(self):
        before = self.mode.read_bytes()
        values = dict(item.split("=", 1) for item in self.prepare())
        self.assertEqual(self.env, {"XDG_DATA_DIRS": str(self.root / "system")})
        private, original = values["XDG_DATA_DIRS"].split(":")
        self.assertEqual(original, self.env["XDG_DATA_DIRS"])
        self.assertEqual(Path(private).parent, self.child)
        mode = json.loads((Path(private) / "gnome-shell/modes/ubuntu.json").read_bytes())
        first, uuid = mode["enabledExtensions"]
        self.assertEqual(first, "ubuntu-dock@ubuntu.com")
        self.assertEqual(mode["parentMode"], "user")
        extension = Path(private) / "gnome-shell/extensions" / uuid
        metadata = json.loads((extension / "metadata.json").read_bytes())
        self.assertEqual(metadata["workspace-display"], str(self.child / "wayland-0"))
        self.assertEqual(metadata["uuid"], uuid)
        self.assertEqual(self.mode.read_bytes(), before)
        self.assertEqual((extension / "extension.js").read_bytes(), Path(gnome_session.__file__).with_name("verify_app_focus.js").read_bytes())

    @unittest.skipUnless(shutil.which("node"), "Node.js required for isolated GNOME Shell API fixtures")
    def test_mutter_focus_read_filters_overview_modal_lock_foreign_and_x11(self):
        module = Path(gnome_session.__file__).with_name("verify_app_focus.js").read_text()
        # Exercise the shipped Read method with in-memory Mutter/Shell objects.
        body = "\n".join(line for line in module.splitlines() if not line.startswith("import "))
        fixture = """
import assert from 'node:assert/strict';
const Extension = class {};
const Main = {overview: {visible: false}, sessionMode: {}, actionMode: 1, modalCount: 0};
let id = 'chatgpt.desktop';
const Shell = {ActionMode: {NORMAL: 1}, WindowTracker: {get_default: () => ({
    get_window_app: () => ({get_id: () => id})})}};
const Meta = {WindowClientType: {WAYLAND: 0}};
let focused = true, type = 0;
const win = {has_focus: () => focused, get_client_type: () => type,
    get_pid: () => 71, get_stable_sequence: () => 42};
global.display = {focus_window: win};
"""
        checks = """
const observer = new AppFocus();
observer._shellPid = 15;
const empty = ['', 0, 0, 15];
for (id of ['chatgpt.desktop', 'com.anthropic.Claude.desktop'])
    assert.deepEqual(observer.Read(), [id, 71, 42, 15]);
for (const [obj, key, value] of [[Main, 'modalCount', 1], [Main, 'actionMode', 8],
        [Main.overview, 'visible', true], [Main.sessionMode, 'isLocked', true],
        [Main.sessionMode, 'isGreeter', true], [global.display, 'focus_window', null]]) {
    const old = obj[key]; obj[key] = value;
    assert.deepEqual(observer.Read(), empty);
    obj[key] = old;
}
type = 1; assert.deepEqual(observer.Read(), empty);
type = 0; focused = false; assert.deepEqual(observer.Read(), empty);
focused = true; id = 'another.desktop'; assert.deepEqual(observer.Read(), empty);
"""
        subprocess.run(["node", "--input-type=module"], input=fixture + body + checks, text=True,
                       check=True, capture_output=True, timeout=8)
