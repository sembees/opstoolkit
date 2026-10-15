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
        self.assertEqual(res["index_check"]["checked"], 2)
        self.assertEqual(res["index_check"]["ok"], 2)
        self.assertEqual(res["index_check"]["bad_count"], 0)
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
        永远红灯，等于没有校验（"狼来了"）。但**必须报出来**（装机若需要它会失败）。"""
        with tempfile.TemporaryDirectory() as tmp:
            suite, sd = _mk_repo(tmp, good=True)
            os.remove(os.path.join(sd, "main", "binary-amd64", "Packages.gz"))
            res = rv.verify_suite(tmp, suite)
        self.assertEqual(res["verdict"], "warn")
        self.assertEqual(res["index_check"]["missing"], ["main/binary-amd64/Packages.gz"])
        self.assertIn("未同步", res["reason"])

    def test_missing_release_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite, sd = _mk_repo(tmp, good=True)
            os.remove(os.path.join(sd, "Release"))
            res = rv.verify_suite(tmp, suite)
            self.assertEqual(res["verdict"], "fail")
            self.assertIn("缺少 Release", res["reason"])

    def test_unsigned_but_self_consistent_is_warn_not_ok(self):
        """★ 不能把"没有签名文件"说成通过 —— 那正是"带官方签名"这条要求的核心。"""
        with tempfile.TemporaryDirectory() as tmp:
            suite, sd = _mk_repo(tmp, good=True)
            os.remove(os.path.join(sd, "Release.gpg"))
            res = rv.verify_suite(tmp, suite)
        self.assertEqual(res["verdict"], "warn")
        self.assertIn("没有签名文件", res["reason"])


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
                          "missing": [], "missing_count": 0})

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
