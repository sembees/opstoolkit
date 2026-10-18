# -*- coding: utf-8 -*-
"""B1-校验：离线签名仓库的完整性核查（模块级用例，全部靠**临时目录里的真实文件**复算）。

判据设计成"会真的抓到坏文件"：两个 suite，一个哈希/大小都对，一个故意把 `Packages`
改一个字节 ⇒ 必须报 fail 并指出是哪个文件。只断言"返回了字典"的用例没有任何价值。
"""
import gzip
import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe import repo_verify as rv   # noqa: E402


def _mk_repo(root, good=True):
    """造一棵最小的仓库树：dists/<suite>/<comp>/binary-amd64/Packages(.gz) + Release。

    注意：`Release` 里的哈希必须按**磁盘上真实的文件字节**算 —— 我第一版拿"未压缩的
    Packages 内容"去当 Packages.gz 的哈希，gzip 头/时间戳一进去就对不上，
    于是"好仓库"被判 fail，用例自己先把结论搞错了（fixture 的错，不是校验器的错）。
    """
    suite = "trixie"
    sd = os.path.join(root, "dists", suite)
    pkgdir = os.path.join(sd, "main", "binary-amd64")
    os.makedirs(pkgdir, exist_ok=True)
    pkg = b"Package: vim\nVersion: 1\nFilename: pool/main/v/vim/vim.deb\n\n"
    with open(os.path.join(pkgdir, "Packages"), "wb") as f:
        f.write(pkg)
    with gzip.open(os.path.join(pkgdir, "Packages.gz"), "wb") as f:
        f.write(pkg)

    def entry(rel):
        path = os.path.join(sd, rel)
        with open(path, "rb") as f:
            data = f.read()
        return " %s %d %s\n" % (hashlib.sha256(data).hexdigest(), len(data), rel)

    rel_txt = ("Origin: Debian\nSuite: " + suite + "\nSHA256:\n"
               + entry("main/binary-amd64/Packages")
               + entry("main/binary-amd64/Packages.gz"))
    with open(os.path.join(sd, "Release"), "w", encoding="utf-8") as f:
        f.write(rel_txt)
    with open(os.path.join(sd, "Release.gpg"), "w") as f:
        f.write("-----BEGIN PGP SIGNATURE-----\n(fake)\n-----END PGP SIGNATURE-----\n")
    if not good:
        # 搬坏一个文件：Release 保持不变，但 Packages 的内容变了 ⇒ SHA256 与 Size 都对不上
        with open(os.path.join(pkgdir, "Packages"), "ab") as f:
            f.write(b"Broken: yes\n")
    return suite, sd


class CompressedVariantTest(unittest.TestCase):
    """★ 2026-10-18（B1 口径闭环）：索引的**压缩变体**不能被当成"缺失"。

    线上事实：`debian-13.7.0/dists/trixie/` 只有 `main/binary-amd64/Packages.gz`，
    而 Release 同时声明了 `Packages` 与 `Packages.gz`。旧逻辑逐个按声明的路径找文件，
    于是"未压缩的那份没同步"被记进 missing（4 条全误报），整仓永远 warn。
    apt 的取法是"哪个变体在就用哪个"，所以**任一变体在 + 哈希对** ⇒ 该索引齐。
    """

    def _mk(self, root, variants, signed=False):
        """variants: 要落盘的变体名列表，如 ["Packages.gz"]。Release 里三种都声明。"""
        suite = "trixie"
        sd = os.path.join(root, "dists", suite)
        pkgdir = os.path.join(sd, "main", "binary-amd64")
        os.makedirs(pkgdir, exist_ok=True)
        pkg = b"Package: vim\nVersion: 1\n\n"
        payload = {"Packages": pkg,
                   "Packages.gz": gzip.compress(pkg),
                   "Packages.xz": b"\xfd7zXZ\x00fake-xz"}
        lines = ["Origin: Debian", "Suite: " + suite, "SHA256:"]
        for name in ("Packages", "Packages.gz", "Packages.xz"):
            data = payload[name]
            path = os.path.join(pkgdir, name)
            if name in variants:
                with open(path, "wb") as f:
                    f.write(data)
            lines.append(" %s %d %s" % (hashlib.sha256(data).hexdigest(), len(data),
                                        "main/binary-amd64/" + name))
        with open(os.path.join(sd, "Release"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if signed:
            with open(os.path.join(sd, "InRelease"), "w") as f:
                f.write("(fake inrelease)\n")
        return suite, sd

    def test_gz_only_is_fully_present(self):
        with tempfile.TemporaryDirectory() as root:
            suite, _sd = self._mk(root, ["Packages.gz"])
            res = rv.verify_suite(root, suite)
            self.assertEqual(res["index_check"]["missing_count"], 0,
                             "只有 .gz 不该报缺失：%s" % res["index_check"]["missing"])
            self.assertEqual(res["index_check"]["ok"], 1)
            self.assertIn("main/binary-amd64/Packages.gz",
                          res["index_check"]["compressed_used"])

    def test_xz_only_is_fully_present(self):
        with tempfile.TemporaryDirectory() as root:
            suite, _sd = self._mk(root, ["Packages.xz"])
            res = rv.verify_suite(root, suite)
            self.assertEqual(res["index_check"]["missing_count"], 0)

    def test_no_variant_at_all_is_missing_and_lists_tried(self):
        with tempfile.TemporaryDirectory() as root:
            suite, _sd = self._mk(root, [])
            res = rv.verify_suite(root, suite)
            self.assertGreaterEqual(res["index_check"]["missing_count"], 1)
            joined = " ".join(res["index_check"]["missing"])
            self.assertIn("main/binary-amd64/Packages", joined)
            self.assertIn("试过变体", joined)

    def test_variant_with_wrong_hash_is_still_bad(self):
        with tempfile.TemporaryDirectory() as root:
            suite, sd = self._mk(root, ["Packages.gz"])
            with open(os.path.join(sd, "main", "binary-amd64", "Packages.gz"), "ab") as f:
                f.write(b"tampered")
            res = rv.verify_suite(root, suite)
            self.assertGreaterEqual(res["index_check"]["bad_count"], 1)
            self.assertEqual(res["verdict"], "fail")

    def test_unsigned_is_warn_with_explicit_reason(self):
        with tempfile.TemporaryDirectory() as root:
            suite, _sd = self._mk(root, ["Packages.gz"], signed=False)
            res = rv.verify_suite(root, suite)
            self.assertEqual(res["verdict"], "warn")
            self.assertIn("签名", res["reason"])
            self.assertIn("未签名", res["reason"])

    def test_inrelease_present_no_unsigned_warning(self):
        with tempfile.TemporaryDirectory() as root:
            suite, _sd = self._mk(root, ["Packages.gz"], signed=True)
            res = rv.verify_suite(root, suite)
            self.assertNotIn("未签名", res["reason"] or "")


class ReleaseParserTest(unittest.TestCase):
    def test_parses_sha256_section_and_stops_at_next_section(self):
        text = ("Origin: Debian\nSHA256:\n"
                " " + "a" * 64 + " 123 main/binary-amd64/Packages\n"
                " " + "b" * 64 + " 456 main/binary-amd64/Packages.gz\n"
                "MD5Sum:\n"
                " " + "c" * 32 + " 7 main/binary-amd64/Packages\n")
        got = rv.parse_release_sha256(text)
        self.assertEqual(sorted(got), ["main/binary-amd64/Packages",
                                       "main/binary-amd64/Packages.gz"])
        self.assertEqual(got["main/binary-amd64/Packages"], ("a" * 64, 123))

    def test_empty_and_garbage_do_not_raise(self):
        self.assertEqual(rv.parse_release_sha256(""), {})
        self.assertEqual(rv.parse_release_sha256("no sections here\n"), {})


class VerifySuiteTest(unittest.TestCase):
    def test_good_repo_passes_index_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite, _sd = _mk_repo(tmp, good=True)
            res = rv.verify_suite(tmp, suite)
        # ★ 2026-10-18：ok 现在按**逻辑索引**计数（Packages 与 Packages.gz 属同一个索引），
        #   所以 checked=2（两个变体都校验了哈希）但 ok=1（算"一个索引齐了"）。
        self.assertEqual(res["index_check"]["checked"], 2)
        self.assertEqual(res["index_check"]["ok"], 1)
        self.assertEqual(res["index_check"]["bad_count"], 0)
        self.assertEqual(res["index_check"]["missing_count"], 0)
        self.assertTrue(res["files"]["Release"])
        # 没有密钥环 ⇒ 签名**未校验**，只能是 warn（不能自称 ok）
        self.assertIn(res["verdict"], ("ok", "warn"))
        if not res["signature"]["checked"]:
            self.assertEqual(res["verdict"], "warn")
            self.assertIn("签名未校验", res["reason"])

    def test_corrupted_index_is_detected_with_the_file_name(self):
        """★ 核心用例：搬坏一个字节必须被抓出来，并指出是哪个文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            suite, _sd = _mk_repo(tmp, good=False)
            res = rv.verify_suite(tmp, suite)
        self.assertEqual(res["verdict"], "fail")
        self.assertEqual(res["index_check"]["bad_count"], 1)
        self.assertEqual(res["index_check"]["bad"][0]["path"], "main/binary-amd64/Packages")
        self.assertIn("装机取包会失败", res["reason"])

    def test_missing_declared_file_is_reported_as_warn_not_fail(self):
        """★ 线上真数据逼出来的语义修正：本工具同步的是**子集**（只搬 amd64 若干组件），
        所以"Release 声明了、本地没搬"是**预期**，只能 warn —— 判 fail 会让部分同步的仓库
        永远红灯，等于没有校验（"狼来了"）。但**必须报出来**（装机若需要它会失败）。

        ★ 2026-10-18 改口径：**删掉一个变体不算缺失**（Packages 还在，apt 用得到）。
        要触发"缺失"，必须把一个逻辑索引的**全部变体**都删掉。
        """
        with tempfile.TemporaryDirectory() as tmp:
            suite, sd = _mk_repo(tmp, good=True)
            # 只删 .gz：Packages 还在 ⇒ 不算缺失（这正是线上 debian-13.7.0 的反向情形）
            os.remove(os.path.join(sd, "main", "binary-amd64", "Packages.gz"))
            res_gz_gone = rv.verify_suite(tmp, suite)
            self.assertEqual(res_gz_gone["index_check"]["missing_count"], 0)
            # 再把未压缩的也删掉 ⇒ 该索引彻底缺失
            os.remove(os.path.join(sd, "main", "binary-amd64", "Packages"))
            res = rv.verify_suite(tmp, suite)
        self.assertEqual(res["verdict"], "warn")
        self.assertGreaterEqual(res["index_check"]["missing_count"], 1)
        self.assertIn("main/binary-amd64/Packages", " ".join(res["index_check"]["missing"]))
        self.assertIn("未同步", res["reason"])

    def test_missing_release_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite, sd = _mk_repo(tmp, good=True)
            os.remove(os.path.join(sd, "Release"))
            res = rv.verify_suite(tmp, suite)
            self.assertEqual(res["verdict"], "fail")
            self.assertIn("缺少 Release", res["reason"])

    def test_unsigned_but_self_consistent_is_warn_not_ok(self):
        """★ 不能把"没有签名文件"说成通过 —— 那正是"带官方签名"这条要求的核心。

        ★ 2026-10-18：文案改成明确的"未签名"（旧文案是"没有签名文件"），
        并说清后果"能装机但无法证明索引没被改过"——测试跟着锁新文案。
        """
        with tempfile.TemporaryDirectory() as tmp:
            suite, sd = _mk_repo(tmp, good=True)
            os.remove(os.path.join(sd, "Release.gpg"))
            res = rv.verify_suite(tmp, suite)
        self.assertEqual(res["verdict"], "warn")
        self.assertIn("未签名", res["reason"])
        self.assertIn("无法证明索引没被改过", res["reason"])


class VerifyRepoAndListTest(unittest.TestCase):
    def test_list_repos_finds_suites_and_size(self):
        with tempfile.TemporaryDirectory() as root:
            _mk_repo(os.path.join(root, "debian-tuna"), good=True)
            repos = rv.list_repos(root)
        self.assertEqual([r["name"] for r in repos], ["debian-tuna"])
        self.assertEqual(repos[0]["suites"], ["trixie"])
        self.assertGreater(repos[0]["size_bytes"], 0)
        self.assertTrue(repos[0]["has_dists"])

    def test_verify_repo_aggregates_worst_verdict(self):
        with tempfile.TemporaryDirectory() as root:
            _mk_repo(os.path.join(root, "good"), good=True)
            _mk_repo(os.path.join(root, "bad"), good=False)
            good = rv.verify_repo(os.path.join(root, "good"))
            bad = rv.verify_repo(os.path.join(root, "bad"))
        self.assertIn(good["verdict"], ("ok", "warn"))
        self.assertEqual(bad["verdict"], "fail")

    def test_non_debian_repo_tree_is_na_not_fail(self):
        """centos-7 / openeuler / rocky 的本地仓库是 RHEL 系 repodata 结构，本来就没有 dists/。
        把它们标红同样是"狼来了" ⇒ 判 n/a（本核查不适用）。"""
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "not-a-repo"))
            res = rv.verify_repo(os.path.join(root, "not-a-repo"))
        self.assertEqual(res["verdict"], "n/a")
        self.assertIn("没有 dists/", res["reason"])
        # ★ 形状用例：提前返回**必须**带上正常分支的全部键。
        #   我第一版漏了 suites（还有 verify_suite 里漏了 index_check），线上直接把
        #   /repo/status 打成 500 —— centos-7 / openeuler-24.03 / rocky-9.4 都没有 dists/。
        #   教训：写"提前返回"时，先把**正常分支返回的全部键**列一遍再返回。
        self.assertEqual(res["suites"], [])
        self.assertIn("name", res)
        self.assertIn("path", res)

    def test_verify_repo_suites_shape_matches_verify_suite(self):
        """调用方按固定形状无脑读；缺键就是 500。两个函数的键必须齐全。"""
        with tempfile.TemporaryDirectory() as root:
            _mk_repo(root, good=True)
            repo = rv.verify_repo(root)
        self.assertTrue(repo["suites"])
        for s in repo["suites"]:
            for k in ("suite", "files", "index_check", "signature", "verdict", "reason"):
                self.assertIn(k, s)
            for k in ("checked", "ok", "bad", "bad_count", "missing", "missing_count"):
                self.assertIn(k, s["index_check"])

    def test_verify_suite_without_release_still_has_index_check(self):
        """缺 Release 的 suite（线上 testing/unstable 那种只有目录的）也要有完整形状。"""
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "dists", "testing"))
            res = rv.verify_suite(root, "testing")
        # 只有空目录 ⇒ "本仓库没同步这个 suite"，warn（不是 fail：它本来就不该有）
        self.assertEqual(res["verdict"], "warn")
        self.assertEqual(res["index_check"],
                         {"checked": 0, "ok": 0, "bad": [], "bad_count": 0,
                          "missing": [], "missing_count": 0,
                          "compressed_used": [], "compressed_count": 0})
        # 形状必须与"有 Release"分支一致：调用方按固定键读，少一个就是 500
        self.assertEqual(set(res["index_check"]),
                         {"checked", "ok", "bad", "bad_count", "missing", "missing_count",
                          "compressed_used", "compressed_count"})

    def test_suite_with_content_but_no_release_is_fail(self):
        """★ 反过来才是真危险：**有索引内容却没有 Release** ⇒ d-i 无法校验镜像 ⇒ fail。"""
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "dists", "trixie", "main", "binary-amd64")
            os.makedirs(d)
            with open(os.path.join(d, "Packages"), "w") as f:
                f.write("Package: vim\n\n")
            res = rv.verify_suite(root, "trixie")
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("有内容但缺少 Release", res["reason"])

    def test_list_repos_on_missing_root_returns_empty(self):
        self.assertEqual(rv.list_repos("/no/such/dir/at/all"), [])


if __name__ == "__main__":
    unittest.main()
