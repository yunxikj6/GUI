import io
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import instabox
from instabox import BridgeError
from linux import native_project_setup as setup
from native_files import validate
from workspace_mcp import VAULT_SERVICE


TOKEN = "synthetic-onboarding-value"
MACHINE = "m_lab"
PROJECT = "/data/name <&> with spaces"


def page(*records, cursor=None):
    return {"machines": [{"id": machine, "name": name, "state": state} for machine, name, state in records],
            "nextCursor": cursor}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.vault = Mock()
        self.items = {}
        self.vault.get_password.side_effect = lambda service, machine: self.items.get((service, machine))
        self.vault.set_password.side_effect = lambda service, machine, key: self.items.update({(service, machine): key})
        self.vault.delete_password.side_effect = lambda service, machine: self.items.pop((service, machine))
        self.ui = Mock()
        self.ui.choose.side_effect = [0, 0]
        self.ui.password.return_value = TOKEN
        self.ui.entry.return_value = PROJECT
        self.ui.confirm.return_value = True
        self.list_page = Mock(return_value=page((MACHINE, "--help\n<&>", "running")))
        self.tree = Mock()
        self.integrate = Mock()

    def configure(self, **options):
        args = dict(dialogs=self.ui, vault_factory=lambda: self.vault, list_page=self.list_page,
                    check_tree=self.tree)
        return setup.configure_project(validate, self.integrate, **(args | options))

    def test_fresh_binding_commits_only_after_explicit_confirmation(self):
        def confirmed(machine, project, action):
            self.assertEqual((machine, project), (MACHINE, PROJECT))
            self.assertIn("Secret Service", action)
            self.tree.assert_called_once_with(TOKEN, machine, project)
            self.vault.set_password.assert_not_called()
            self.integrate.assert_not_called()
            return True
        self.ui.confirm.side_effect = confirmed
        self.assertTrue(self.configure())
        self.assertEqual(self.items, {(VAULT_SERVICE, MACHINE): TOKEN})
        self.integrate.assert_called_once_with(MACHINE, PROJECT)

    def test_cancel_at_each_dialog_has_no_effect(self):
        for choices, password, directory, consent in [
            ([None], TOKEN, PROJECT, True), ([0], None, PROJECT, True),
            ([0, None], TOKEN, PROJECT, True), ([0, 0], TOKEN, None, True),
            ([0, 0], TOKEN, PROJECT, False),
        ]:
            with self.subTest(choices=choices, directory=directory, consent=consent):
                self.ui.choose.side_effect = choices
                self.ui.password.return_value = password
                self.ui.entry.return_value = directory
                self.ui.confirm.return_value = consent
                self.assertFalse(self.configure())
                self.vault.set_password.assert_not_called()
                self.integrate.assert_not_called()

    def test_reuse_known_or_manually_identified_vault_item(self):
        self.items[VAULT_SERVICE, MACHINE] = TOKEN
        for current in (None, (MACHINE, PROJECT)):
            with self.subTest(current=current):
                self.ui.choose.side_effect = [0 if current else 1, 0]
                self.ui.entry.side_effect = [PROJECT] if current else [MACHINE, PROJECT]
                self.assertTrue(self.configure(current=current))
                self.ui.password.assert_not_called()
                self.vault.set_password.assert_not_called()
                self.assertIn("继续使用", self.ui.confirm.call_args.args[2])

    def test_credential_replacement_and_rollback(self):
        for previous in (None, "previous-synthetic"):
            with self.subTest(previous=previous):
                self.items = {} if previous is None else {(VAULT_SERVICE, MACHINE): previous}
                self.ui.choose.side_effect = [0, 0]
                self.integrate.side_effect = OSError(TOKEN)
                with self.assertRaises(ValueError) as ctx:
                    self.configure()
                self.assertNotIn(TOKEN, str(ctx.exception))
                self.assertEqual(self.items.get((VAULT_SERVICE, MACHINE)), previous)
                if previous:
                    self.assertIn("替换", self.ui.confirm.call_args.args[2])

    def test_pagination_stopped_machine_and_directory_retry(self):
        self.list_page.side_effect = [page(("m_off", "Paused", "suspended"), cursor="page2"),
                                      page(("m_off", "Paused", "suspended"), cursor="page2"),
                                      page((MACHINE, "User name", "running"))]
        self.ui.choose.side_effect = [0, 0, 1, 0]
        self.ui.entry.side_effect = [TOKEN, "../relative", "/missing", PROJECT]
        self.tree.side_effect = [BridgeError("not_found", TOKEN), None]
        self.assertTrue(self.configure())
        self.assertEqual([c.args[1] for c in self.list_page.call_args_list], [None, None, "page2"])
        self.assertEqual([c.args[1:] for c in self.tree.call_args_list], [(MACHINE, "/missing"), (MACHINE, PROJECT)])
        for call in self.ui.entry.call_args_list:
            self.assertNotIn(TOKEN, call.args)

    def test_unavailable_vault_or_cloud_never_echoes_secret(self):
        with self.assertRaises(ValueError) as denied:
            self.configure(vault_factory=Mock(side_effect=RuntimeError(TOKEN)))
        self.assertNotIn(TOKEN, str(denied.exception))
        self.ui.choose.side_effect = [0, 0]
        self.tree.side_effect = BridgeError("denied", TOKEN)
        with self.assertRaises(ValueError) as denied:
            self.configure()
        self.assertNotIn(TOKEN, str(denied.exception))
        self.integrate.assert_not_called()
        self.vault.set_password.assert_not_called()

    def test_masking_and_safe_machine_cells(self):
        ui = setup.Zenity()
        def run(argv, **kwargs):
            self.assertTrue(all(TOKEN not in arg for arg in argv))
            self.assertNotIn("INSTABOX_API_KEY", kwargs["env"])
            if "--password" in argv:
                self.assertEqual(kwargs["input"], "")
                out = TOKEN + "\n"
            else:
                self.assertIn("--no-markup", argv)
                self.assertIn("--hide-column=1", argv)
                self.assertIn("--print-column=1", argv)
                self.assertFalse(any("--help" in arg or "<b>" in arg for arg in argv))
                self.assertEqual(kwargs["input"], "0\n--help\\n<b>\\u202e\n")
                out = "0\n"
            return subprocess.CompletedProcess(argv, 0, out)
        with patch.dict(os.environ, {"INSTABOX_API_KEY": TOKEN}), patch.object(subprocess, "run", side_effect=run):
            self.assertEqual(ui.password(), TOKEN)
            self.assertEqual(ui.choose("选择 InstaBox 云机", "选择", ["名称"], [("--help\n<b>\u202e",)]), 0)

    def test_missing_display_and_concurrent_vault_change(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "Gtk-WARNING: cannot open display " + TOKEN)):
            with self.assertRaises(ValueError) as failed:
                setup.Zenity().password()
            self.assertNotIn(TOKEN, str(failed.exception))

        def concurrent_change(*args):
            self.items[VAULT_SERVICE, MACHINE] = "concurrent-synthetic"
            return True
        self.ui.confirm.side_effect = concurrent_change
        with self.assertRaisesRegex(ValueError, "已被更改"):
            self.configure()
        self.vault.set_password.assert_not_called()
        self.integrate.assert_not_called()

    def test_wire_uses_fixed_account_host_and_a_read_only_guest_operation(self):
        opaque = "https://elsewhere.invalid/p?x=1 &y=#"
        reqs = []
        def open_request(req, timeout):
            url = urlsplit(req.full_url)
            self.assertEqual((url.scheme, url.netloc), ("https", "api.instabox.sh"))
            self.assertEqual(req.get_header("X-api-key"), TOKEN)
            reqs.append((req.method, url.path, parse_qs(url.query)))
            if req.method == "GET":
                response = page((MACHINE, "Machine", "running"))
            else:
                body = json.loads(req.data)
                self.assertEqual(json.loads(body["stdin"]), {"op": "tree", "path": ""})
                self.assertEqual(body["envs"], {"CLOUD_WORKSPACE_ROOT": PROJECT})
                self.assertTrue(body["noHistory"])
                self.assertEqual(body["argv"][:4], ["python3", "-I", "-B", "-c"])
                response = {"stdout":json.dumps({"ok": True, "data": {"path": "", "entries": []}}), "exitCode":0}
            return io.BytesIO(json.dumps(response).encode())
        opener = Mock()
        opener.open.side_effect = open_request
        with patch.object(instabox.urllib.request, "build_opener", return_value=opener) as factory:
            self.assertEqual(setup.machine_page(TOKEN, opaque)["machines"][0]["id"], MACHINE)
            setup.check_project(TOKEN, MACHINE, PROJECT)
        self.assertTrue(all(isinstance(call.args[0], instabox.NoRedirect) for call in factory.call_args_list))
        self.assertEqual(reqs, [("GET", "/v1/machines", {"limit": ["30"], "cursor": [opaque]}),
                                ("GET", "/v1/machines", {}), ("POST", "/v1/machines/m_lab/exec", {})])
