# -*- coding: utf-8 -*-
"""U2-F7：回滚之前先确认"磁盘上现在还是**本次**写下的那份"。

要回答的问题：部署写到一半失败了，而**在我们写完之后有别的程序或人改了其中一个文件** ——
这时把部署前的旧内容写回去，等于**悄悄覆盖别人的改动**，而且我们还会报"已恢复原样"。
那是最坏的一种谎报。所以这类路径一律不动，并在结论里点名。

判据（每条都有对应用例）：
  1. 没被外部改过的路径：照旧逐字节回滚 —— **老行为不能被这次改动破坏**；
  2. 被外部改过 / 被删掉的路径：**不动它**、如实报"部分未回滚"、`written` 不清空；
  3. 我们**没能记下 sha** 的路径（典型：写入本身就失败了、文件可能已被截断）：照旧回滚 ——
     这里绝不能因为"sha 对不上"就不回滚，那会把半截文件留在磁盘上（U2-F1 的形态）；
  4. dnsmasq 配置同样受保护（它走 write_conf / sudo tee，必须单独处理）；
  5. 真走一遍 PXE 部署路径：见 test_pxe.py 里的
     `DeployPreflightAndRollbackTest.test_externally_modified_file_is_not_overwritten_by_rollback`。
"""
import unittest


class FilestoreRollbackShaTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest import mock

        import app.core.dhcp as dhcp
        from app.core import filestore

        self.os = os
        self.mock = mock
        self.filestore = filestore
        self.dhcp = dhcp
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.confdir = os.path.join(self.dir, "dnsmasq.d")
        os.makedirs(self.confdir)
        p = mock.patch.object(dhcp, "CONF_DIR", self.confdir)
        p.start()
        self.addCleanup(p.stop)

    # ── 辅助 ──

    def _p(self, name):
        return self.os.path.join(self.dir, name)

    def _conf(self):
        return self.os.path.join(self.confdir, "opstk-pxe.conf")

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def _read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def _case(self, external=None):
        """造一个"部署前 v1 → 本次写 v2（记了 sha）→ 可能被外部改成 external"的现场。"""
        p = self._p("boot.ipxe")
        self._write(p, "v1\n")
        prev = {p: self.filestore.snapshot_path(p)}
        self.filestore.atomic_write(p, "v2\n")
        written_sha = {p: self.filestore.content_sha("v2\n")}
        if external is not None:
            self._write(p, external)
        log, errors = [], []
        errs, written, extra = self.filestore.try_rollback(
            log, errors, prev, [], None, "opstk-pxe.conf", ["boot.ipxe"],
            written_sha=written_sha)
        return p, errs, written, extra, log

    # ── 1. 老行为：没被改过就照旧回滚 ──

    def test_untouched_file_is_restored_byte_for_byte(self):
        p, errs, written, extra, log = self._case()
        self.assertTrue(extra.get("rolled_back"), extra)
        self.assertEqual(written, [], "回滚成功必须清空 files_written")
        self.assertEqual(self._read(p), b"v1\n")
        self.assertTrue(any("已回滚" in e for e in errs), errs)
        self.assertTrue(any("Rolled back" in l for l in log), log)

    def test_without_sha_map_the_old_behaviour_is_kept(self):
        """没传 written_sha 的调用方（第三方/老调用点）不受影响 —— 照旧回滚。"""
        p = self._p("boot.ipxe")
        self._write(p, "v1\n")
        prev = {p: self.filestore.snapshot_path(p)}
        self.filestore.atomic_write(p, "v2\n")
        self._write(p, "SOMEONE-ELSE\n")
        errs, written, extra = self.filestore.try_rollback(
            [], [], prev, [], None, "opstk-pxe.conf", ["boot.ipxe"])
        self.assertTrue(extra.get("rolled_back"))
        self.assertEqual(self._read(p), b"v1\n")

    # ── 2. 外部改动：不动它、如实报 ──

    def test_externally_modified_file_is_left_alone(self):
        p, errs, written, extra, log = self._case(external="SOMEONE-ELSE\n")
        self.assertFalse(extra.get("rolled_back"), extra)
        self.assertTrue(extra.get("externally_modified"), extra)
        self.assertTrue(any(p in s for s in extra["externally_modified"]), extra)
        # ★ 核心断言：别人的改动**原样留着**（没被我们的旧内容覆盖）
        self.assertEqual(self._read(p), b"SOMEONE-ELSE\n")
        # 本次写入没有被真正撤回 ⇒ files_written 不能清空（清了就是谎报"什么都没发生"）
        self.assertEqual(written, ["boot.ipxe"])
        self.assertTrue(any("部分未回滚" in e for e in errs), errs)
        self.assertTrue(any("别的程序或人" in e for e in errs), errs)
        self.assertTrue(any("Skipped rollback" in l for l in log), log)

    def test_deleted_file_is_not_resurrected(self):
        """别人**删掉**了我们写的文件：也不该把部署前的旧内容再变出来。"""
        p = self._p("boot.ipxe")
        self._write(p, "v1\n")
        prev = {p: self.filestore.snapshot_path(p)}
        self.filestore.atomic_write(p, "v2\n")
        self.os.remove(p)
        errs, written, extra = self.filestore.try_rollback(
            [], [], prev, [], None, "opstk-pxe.conf", ["boot.ipxe"],
            written_sha={p: self.filestore.content_sha("v2\n")})
        self.assertFalse(extra.get("rolled_back"), extra)
        self.assertFalse(self.os.path.exists(p), "不该把别人删掉的文件又变回来")
        self.assertTrue(any("已被删除" in s for s in extra["externally_modified"]), extra)

    def test_partial_rollback_still_restores_the_others(self):
        """混合状态：被改的那个不动，**其余照常回滚**（并把两边都报清楚）。"""
        keep = self._p("a.ipxe")
        other = self._p("b.ipxe")
        for f, txt in ((keep, "k1\n"), (other, "o1\n")):
            self._write(f, txt)
        prev = {keep: self.filestore.snapshot_path(keep),
                other: self.filestore.snapshot_path(other)}
        self.filestore.atomic_write(keep, "k2\n")
        self.filestore.atomic_write(other, "o2\n")
        self._write(keep, "SOMEONE-ELSE\n")
        errs, written, extra = self.filestore.try_rollback(
            [], [], prev, [], None, "opstk-pxe.conf", ["a.ipxe", "b.ipxe"],
            written_sha={keep: self.filestore.content_sha("k2\n"),
                         other: self.filestore.content_sha("o2\n")})
        self.assertEqual(self._read(keep), b"SOMEONE-ELSE\n")
        self.assertEqual(self._read(other), b"o1\n")
        self.assertEqual(extra.get("files_restored"), 1, extra)
        self.assertEqual(len(extra.get("externally_modified") or []), 1, extra)

    # ── 3. 没记下 sha 的路径（写入失败/可能被截断）必须照旧回滚 ──

    def test_path_without_recorded_sha_is_still_restored(self):
        """写入失败时文件可能已被截断，而我们**没有**它的 sha ——
        此时必须照旧恢复，绝不能因为"sha 对不上"就把半截文件留在磁盘上（U2-F1 的形态）。"""
        p = self._p("boot.ipxe")
        self._write(p, "v1\n")
        prev = {p: self.filestore.snapshot_path(p)}
        self._write(p, "")                      # 模拟 open("w") 截断后失败
        errs, written, extra = self.filestore.try_rollback(
            [], [], prev, [], None, "opstk-pxe.conf", ["boot.ipxe"],
            written_sha={})                     # 本次没有任何"成功写入"的记录
        self.assertTrue(extra.get("rolled_back"), extra)
        self.assertEqual(self._read(p), b"v1\n")

    # ── 4. dnsmasq 配置（走 write_conf/sudo tee，单独处理） ──

    def test_conf_externally_modified_is_left_alone(self):
        self.dhcp.write_conf("opstk-pxe.conf", "OLD-CONF\n")
        conf_prev = self.filestore.snapshot_path(self._conf())
        self.dhcp.write_conf("opstk-pxe.conf", "OURS-CONF\n")
        written_sha = {"opstk-pxe.conf": self.dhcp.conf_sha("OURS-CONF\n")}
        self.dhcp.write_conf("opstk-pxe.conf", "SOMEONE-ELSE-CONF\n")
        errs, written, extra = self.filestore.try_rollback(
            [], [], {}, [], conf_prev, "opstk-pxe.conf", written_sha,
            conf_written_sha=self.dhcp.conf_sha("OURS-CONF\n"))
        self.assertFalse(extra.get("rolled_back"), extra)
        self.assertEqual(self._read(self._conf()), b"SOMEONE-ELSE-CONF\n")
        self.assertTrue(any("opstk-pxe.conf" in s for s in extra["externally_modified"]), extra)

    def test_conf_untouched_is_restored(self):
        self.dhcp.write_conf("opstk-pxe.conf", "OLD-CONF\n")
        conf_prev = self.filestore.snapshot_path(self._conf())
        self.dhcp.write_conf("opstk-pxe.conf", "OURS-CONF\n")
        errs, written, extra = self.filestore.try_rollback(
            [], [], {}, [], conf_prev, "opstk-pxe.conf", ["opstk-pxe.conf"],
            conf_written_sha=self.dhcp.conf_sha("OURS-CONF\n"))
        self.assertTrue(extra.get("rolled_back"), extra)
        self.assertEqual(self._read(self._conf()), b"OLD-CONF\n")
        # 写回旧配置 ⇒ 调用方要用这个 sha 去确认宿主机重新与磁盘一致
        self.assertEqual(extra.get("rollback_conf_sha"), self.dhcp.conf_sha("OLD-CONF\n"))

    def test_conf_not_recorded_means_restore_as_before(self):
        """write_conf 失败（want_sha 为空）时不能因为 sha 检查而拒绝回滚。"""
        self.dhcp.write_conf("opstk-pxe.conf", "OLD-CONF\n")
        conf_prev = self.filestore.snapshot_path(self._conf())
        self.dhcp.write_conf("opstk-pxe.conf", "")      # 模拟被截断
        errs, written, extra = self.filestore.try_rollback(
            [], [], {}, [], conf_prev, "opstk-pxe.conf", ["opstk-pxe.conf"],
            conf_written_sha="")
        self.assertTrue(extra.get("rolled_back"), extra)
        self.assertEqual(self._read(self._conf()), b"OLD-CONF\n")

    # ── 5. content_sha 的口径必须与 atomic_write 一致 ──

    def test_content_sha_matches_what_atomic_write_puts_on_disk(self):
        """口径不一致会让"没人动过"被误判成"被别人改过"（该回滚的没回滚）。"""
        import hashlib
        p = self._p("x.txt")
        for content in ("hello\n", "中文\n", b"raw-bytes\n"):
            with self.subTest(content=content):
                self.filestore.atomic_write(p, content)
                want = self.filestore.content_sha(content)
                with open(p, "rb") as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), want)
