import concurrent.futures
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cloud import workspace
from instabox import BridgeError
from native_file_copies import Copies


class NativeFileCopiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "本地 工作副本"
        self.machine = "m_local_test"
        self.project = "/data/云项目"
        self.rel = "目录/草稿 中文.txt"
        self.original = "原稿\r\nA\rB\n🙂".encode()
        self.roots = {}
        self.calls = []
        self.offline = False
        self.cloud = self.make_remote(self.machine, self.project)
        self.copies = Copies(self.base, self.rpc)

    def make_remote(self, machine, project):
        root = self.root / f"remote-{len(self.roots)}"
        full = root / self.rel
        full.parent.mkdir(parents=True)
        full.write_bytes(self.original)
        self.roots[machine, project] = root
        return full

    def rpc(self, machine, project, request):
        self.calls.append((machine, project, request.copy()))
        if self.offline:
            raise BridgeError("network", "云机未连接。")
        try:
            return workspace.dispatch(request, self.roots[machine, project])
        except workspace.WorkspaceError as exc:
            raise BridgeError(exc.code, str(exc)) from None
        except (FileNotFoundError, OSError):
            raise BridgeError("invalid_path", "文件不可用。") from None

    def checkout(self):
        return self.copies.checkout(self.machine, self.project, self.rel)

    def expect_error(self, code, func, *args):
        with self.assertRaises(BridgeError) as error:
            func(*args)
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_native_layout_offline_reuse_and_project_machine_isolation(self):
        local = self.checkout()
        self.assertEqual(local.read_bytes(), self.original)
        self.assertEqual(local.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.base.stat().st_mode & 0o777, 0o700)
        self.assertEqual(local.name, "草稿 中文.txt")
        (self.cloud.parent / "同目录.txt").write_text("姐妹文件")
        sister = self.copies.checkout(self.machine, self.project, "目录/同目录.txt")
        self.assertEqual(sister.parent, local.parent)
        for machine, project in ((self.machine, "/other/云项目"), ("m_second", self.project)):
            self.make_remote(machine, project)
            other = self.copies.checkout(machine, project, self.rel)
            self.assertNotEqual(other, local)
        local.write_text("断线草稿")
        self.offline = True
        self.calls.clear()
        again = Copies(self.base, self.rpc)
        self.assertEqual(again.checkout(self.machine, self.project, self.rel), local)
        self.assertEqual(again.inspect(local)["status"], "modified")
        self.assertFalse(self.calls)
        self.expect_error("network", again.publish, local)
        self.expect_error("network", again.refresh, local)
        self.assertEqual(local.read_text(), "断线草稿")
        self.assertEqual(self.cloud.read_bytes(), self.original)
        self.offline = False
        result = again.publish(local)
        self.assertEqual(result["status"], "published")
        self.assertFalse(result["dirty"])
        self.assertEqual({(m, p) for m, p, _ in self.calls}, {(self.machine, self.project)})
        self.assertEqual(again.publish(local)["status"], "unchanged")
        self.assertEqual(again.refresh(local)["status"], "current")

    def test_conflict_refresh_registered_sibling_preserves_both_then_merge_and_publish(self):
        local = self.checkout()
        local.write_text("本地稿")
        self.assertEqual(self.copies.refresh(local)["status"], "local_changes")
        self.cloud.write_text("同事稿")
        self.expect_error("conflict", self.copies.publish, local)
        data = self.copies.refresh(local)
        self.assertEqual(data["status"], "remote_copy")
        self.assertFalse(data["remoteCopyDirty"])
        self.assertEqual(data["remoteCopyLocalRevision"], data["remoteRevision"])
        sibling = Path(data["remoteCopy"])
        self.assertNotEqual(local, sibling)
        self.assertEqual(local.name, sibling.name)
        self.assertEqual(sibling.read_text(), "同事稿")
        self.assertEqual(self.copies.inspect(sibling)["revision"], data["remoteRevision"])
        sibling.write_text("合并稿🙂\n")
        again = Copies(self.base, self.rpc)
        reused = again.refresh(local)
        self.assertTrue(reused["remoteCopyDirty"])
        self.assertEqual(reused["remoteCopy"], str(sibling))
        self.assertEqual(reused["remoteCopyLocalRevision"], hashlib.sha256(sibling.read_bytes()).hexdigest())
        self.assertNotEqual(reused["remoteCopyLocalRevision"], reused["remoteRevision"])
        self.assertEqual(sibling.read_text(), "合并稿🙂\n")
        self.assertEqual(again.publish(sibling)["status"], "published")
        self.assertEqual(self.cloud.read_text(), "合并稿🙂\n")
        self.assertEqual(local.read_text(), "本地稿")
        self.assertEqual(again.refresh(local)["remoteCopy"], str(sibling))
        self.expect_error("conflict", again.publish, local)

    def test_unknown_save_outcome_survives_restart_and_newer_local_edits(self):
        local = self.checkout()
        for replacement in (False, True):
            with self.subTest(newer_local_draft=replacement):
                content = f"已发送 {replacement}\r\n".encode()
                local.write_bytes(content)
                def lose_receipt(machine, project, request):
                    data = self.rpc(machine, project, request)
                    if request["op"] == "save":
                        raise BridgeError("network", "回执丢失。")
                    return data
                self.expect_error("network", Copies(self.base, lose_receipt).publish, local)
                self.assertEqual(self.cloud.read_bytes(), content)
                self.calls.clear()
                if replacement:
                    local.write_bytes(content + b"new")
                result = Copies(self.base, self.rpc).publish(local)
                self.assertFalse(result["dirty"])
                saves = [request for _, _, request in self.calls if request["op"] == "save"]
                if replacement:
                    self.assertEqual(result["status"], "published")
                    self.assertEqual(len(saves), 1)
                    self.assertEqual(saves[0]["revision"], hashlib.sha256(content).hexdigest())
                else:
                    self.assertEqual(result["status"], "already_published")
                    self.assertEqual(saves, [])
                self.assertEqual(self.cloud.read_bytes(), local.read_bytes())

    def test_editor_atomic_replace_while_publishing_and_refreshing_never_loses_draft(self):
        local = self.checkout()
        first = "第一次编辑\r\n".encode()
        second = "发布时的编辑".encode()
        third = "刷新时的编辑".encode()
        def editor_save(data):
            temp = local.with_suffix(".tmp")
            temp.write_bytes(data)
            os.replace(temp, local)
        def racing_save(machine, project, request):
            if request["op"] == "save":
                editor_save(second)
            return self.rpc(machine, project, request)
        editor_save(first)
        saved = Copies(self.base, racing_save).publish(local)
        self.assertTrue(saved["dirty"])
        self.assertEqual(saved["revision"], hashlib.sha256(first).hexdigest())
        self.assertEqual(self.cloud.read_bytes(), first)
        self.assertEqual(local.read_bytes(), second)
        self.cloud.write_bytes(b"remote")
        def racing_refresh(machine, project, request):
            reply = self.rpc(machine, project, request)
            editor_save(third)
            return reply
        refreshed = Copies(self.base, racing_refresh).refresh(local)
        self.assertTrue(refreshed["dirty"])
        self.assertEqual(refreshed["localRevision"], hashlib.sha256(third).hexdigest())
        self.assertEqual(local.read_bytes(), third)
        self.assertEqual(Path(refreshed["remoteCopy"]).read_bytes(), b"remote")
        self.assertEqual(self.cloud.read_bytes(), b"remote")

    def test_cloud_cas_race_is_detected_without_overwriting_either_file(self):
        local = self.checkout()
        local.write_text("本机")
        def race(machine, project, request):
            if request["op"] == "save":
                self.cloud.write_text("别人刚写入")
            return self.rpc(machine, project, request)
        self.expect_error("conflict", Copies(self.base, race).publish, local)
        self.assertEqual(local.read_text(), "本机")
        self.assertEqual(self.cloud.read_text(), "别人刚写入")

    def test_neither_given_path_nor_substituted_links_can_escape(self):
        local = self.checkout()
        outside = self.root / "unrelated"
        outside.write_text("私有")
        self.calls.clear()
        for path in (outside, local.with_name("unregistered.txt"), local / ".." / local.name):
            self.expect_error("unregistered_file", self.copies.publish, path)
        for path in ("/etc/passwd", "../目录/file", ".git/config", "目录/.cloudwork/save", "目录//file"):
            self.expect_error("invalid_path", self.copies.checkout, self.machine, self.project, path)
        local.unlink()
        for create in (lambda: local.symlink_to(outside), lambda: os.link(outside, local), lambda: os.mkfifo(local)):
            create()
            with self.assertRaises(BridgeError):
                self.copies.publish(local)
            local.unlink()
        local.parent.rmdir()
        local.parent.symlink_to(self.root)
        with self.assertRaises(BridgeError):
            self.copies.publish(local)
        self.assertFalse(self.calls)
        self.assertEqual(outside.read_text(), "私有")
        self.assertEqual(self.cloud.read_bytes(), self.original)

    def test_128_kib_exact_boundary_binary_and_unregistered_destination(self):
        self.cloud.write_bytes(b"a" * workspace.MAX_TEXT)
        local = self.checkout()
        for contents, code in ((b"a" * (workspace.MAX_TEXT + 1), "too_large"), (b"abc\0", "unsupported_file"),
                               (b"\xff", "unsupported_file")):
            with self.subTest(code=code):
                local.write_bytes(contents)
                self.offline = True
                self.assertEqual(self.checkout(), local)
                self.offline = False
                self.expect_error(code, self.copies.publish, local)
                self.assertEqual(self.cloud.stat().st_size, workspace.MAX_TEXT)
        local.write_bytes(b"x" * workspace.MAX_TEXT)
        self.assertEqual(self.copies.publish(local)["status"], "published")
        self.assertEqual(self.cloud.read_bytes(), local.read_bytes())
        (self.cloud.parent / "unregistered").write_bytes(b"cloud")
        occupied = local.with_name("unregistered")
        occupied.write_bytes(b"local")
        self.expect_error("local_conflict", self.copies.checkout, self.machine, self.project, "目录/unregistered")
        self.expect_error("unregistered_file", self.copies.publish, occupied)
        self.assertEqual(occupied.read_bytes(), b"local")

    def test_concurrent_checkout_only_fetches_once_and_interrupted_checkout_finishes_offline(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            locals_ = list(pool.map(lambda _: self.checkout(), range(8)))
        self.assertEqual(len(set(locals_)), 1)
        self.assertEqual([r["op"] for _, _, r in self.calls], ["file"])
        (self.cloud.parent / "新.txt").write_bytes(b"saved")
        with patch.object(self.copies, "_materialize", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.copies.checkout(self.machine, self.project, "目录/新.txt")
        self.offline = True
        again = Copies(self.base, self.rpc)
        recovered = again.checkout(self.machine, self.project, "目录/新.txt")
        self.assertEqual(recovered.read_bytes(), b"saved")
        self.assertEqual(again.inspect(recovered)["status"], "clean")
        with sqlite3.connect(self.base / ".registry/copies.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM copies WHERE initial IS NOT NULL").fetchone()[0], 0)

    def test_deleted_registered_file_can_be_refreshed_without_clobbering_resurrected_draft(self):
        local = self.checkout()
        local.unlink()
        self.cloud.write_bytes(b"new cloud version")
        self.offline = True
        self.expect_error("network", self.checkout)
        self.expect_error("network", self.copies.refresh, local)
        self.assertFalse(local.exists())
        self.offline = False
        def editor_recreates(machine, project, request):
            data = self.rpc(machine, project, request)
            local.write_bytes(b"editor restored a draft")
            return data
        racing = Copies(self.base, editor_recreates)
        self.expect_error("local_changed", racing.refresh, local)
        self.assertEqual(local.read_bytes(), b"editor restored a draft")
        local.unlink()
        self.expect_error("local_changed", racing.checkout, self.machine, self.project, self.rel)
        self.assertEqual(local.read_bytes(), b"editor restored a draft")
        self.expect_error("conflict", self.copies.publish, local)
        local.unlink()
        self.assertEqual(self.checkout(), local)
        self.assertEqual(local.read_bytes(), b"new cloud version")
        self.assertFalse(self.copies.inspect(local)["dirty"])
        self.cloud.write_bytes(b"yet another version")
        local.unlink()
        result = self.copies.refresh(local)
        self.assertEqual(result["status"], "restored")
        self.assertFalse(result["dirty"])
        self.assertEqual(local.read_bytes(), self.cloud.read_bytes())
        self.assertEqual(self.checkout(), local)
        local.write_bytes(b"edited again")
        self.assertEqual(self.copies.publish(local)["status"], "published")
        self.assertEqual(self.cloud.read_bytes(), b"edited again")

    def test_replaced_root_and_mismatched_registry_binding_are_rejected_before_rpc(self):
        local = self.checkout()
        self.calls.clear()
        db_path = self.base / ".registry/copies.sqlite3"
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE copies SET machine='m_other'")
        self.expect_error("unsafe_local", self.copies.publish, local)
        self.expect_error("unsafe_local", self.checkout)
        self.base.rename(self.root / "moved")
        self.base.mkdir(mode=0o700)
        self.expect_error("unsafe_local", self.checkout)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertFalse(self.calls)

    def test_invalid_worker_response_creates_no_copy_and_never_advances_revision(self):
        def corrupt(machine, project, request):
            result = self.rpc(machine, project, request)
            return {**result, "revision": "0" * 64}
        self.expect_error("invalid_response", Copies(self.base, corrupt).checkout, self.machine, self.project, self.rel)
        local = self.checkout()
        base_revision = self.copies.inspect(local)["revision"]
        local.write_text("edited")
        def corrupt_save(machine, project, request):
            if request["op"] == "save":
                return corrupt(machine, project, request)
            return self.rpc(machine, project, request)
        self.expect_error("invalid_response", Copies(self.base, corrupt_save).publish, local)
        self.assertEqual(self.copies.inspect(local)["revision"], base_revision)
        self.assertEqual(self.copies.publish(local)["status"], "already_published")


if __name__ == "__main__":
    unittest.main()
