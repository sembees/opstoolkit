"""方案 B（自动补齐 AppStream）用例 —— 2026-10-09，RUNBOOK §5.83.62。

规则：RHEL 系（rhel/rocky/almalinux/oraclelinux）缺 AppStream 时，
**确认兄弟仓库存在**才自动补进 extra_repos；确认不到就保留警示 A（绝不凭约定猜，
因为给一个不存在的 repo 可能让装机硬失败）。
"""
from app.it.pxe.generator import (appstream_sibling_url, appstream_warning,
                                  complete_appstream)


class TestSiblingUrl:
    """同级 URL 推导：只在路径明确以 /BaseOS 结尾时才推，不做别的猜测。"""

    def test_plain(self):
        assert appstream_sibling_url(
            "http://m/rocky/9.4/BaseOS") == "http://m/rocky/9.4/AppStream/"

    def test_trailing_slash(self):
        assert appstream_sibling_url(
            "http://m/rocky/9.4/BaseOS/") == "http://m/rocky/9.4/AppStream/"

    def test_case_and_spaces(self):
        assert appstream_sibling_url("  http://m/x/BaseOS/  ") == "http://m/x/AppStream/"

    def test_not_baseos_shape_gives_empty(self):
        for bad in ("http://m/rocky/9.4/", "http://m/rocky/9.4/AppStream/",
                    "http://m/rocky/9.4/everything/", "", None):
            assert appstream_sibling_url(bad) == ""

    def test_official_mirror_nested_shape(self):
        """官方镜像站的标准嵌套形态（rocky/9/BaseOS/x86_64/os/）也要能推出同级 AppStream。

        依据：测试基线里就有 `http://mirror.example/rocky/9/BaseOS/x86_64/os/` 这种写法，
        而真实 RHEL 系镜像站普遍是 `…/<ver>/BaseOS/x86_64/os/` 与 `…/<ver>/AppStream/x86_64/os/`。
        """
        assert appstream_sibling_url(
            "http://mirror.example/rocky/9/BaseOS/x86_64/os/"
        ) == "http://mirror.example/rocky/9/AppStream/x86_64/os/"


class TestCompleteAppstream:
    """自动补齐：注入探针，逐个分支钉死。"""

    BASEOS = "http://m/rocky/9.4/BaseOS/"

    def test_adds_when_sibling_confirmed(self):
        seen = []

        def exists(u):
            seen.append(u)
            return True

        extra, added = complete_appstream("rocky", self.BASEOS, [], exists)
        assert added == "http://m/rocky/9.4/AppStream/"
        assert extra and extra[-1]["name"] == "AppStream"
        assert seen == ["http://m/rocky/9.4/AppStream/"], "探针只该发一次，且探的是同级"
        # 补上之后就不该再有警示
        assert appstream_warning("rocky", self.BASEOS, extra) == ""

    def test_no_add_when_sibling_missing(self):
        extra, added = complete_appstream("rocky", self.BASEOS, [], lambda u: False)
        assert added == "" and extra == []
        assert appstream_warning("rocky", self.BASEOS, extra), "没补上就必须保留警示"

    def test_probe_exception_degrades_to_warning(self):
        def boom(u):
            raise TimeoutError("probe timeout")

        extra, added = complete_appstream("rocky", self.BASEOS, [], boom)
        assert added == "" and extra == []
        assert appstream_warning("rocky", self.BASEOS, extra), "探测异常必须降级为只警示"

    def test_no_probe_when_appstream_already_present(self):
        called = []

        def exists(u):
            called.append(u)
            return True

        extra, added = complete_appstream(
            "rocky", self.BASEOS, [{"name": "AppStream", "url": "http://m/AppStream/"}], exists)
        assert added == "" and not called, "已有 AppStream 时一次探针都不该发"

    def test_single_repo_family_untouched(self):
        called = []
        extra, added = complete_appstream("openeuler", "http://m/openeuler-24.03/", [],
                                          lambda u: called.append(u) or True)
        assert added == "" and extra == [] and not called
        extra2, added2 = complete_appstream("ubuntu", "http://m/ubuntu/22.04/", [],
                                            lambda u: called.append(u) or True)
        assert added2 == "" and extra2 == [] and not called

    def test_tree_root_shape_not_probed(self):
        """实测矩阵入口 #1：树根形态已由 _detect_rhel_media 识别成 AppStream+BaseOS，不走这里。"""
        called = []
        extra, added = complete_appstream("rocky", "http://m/rocky/9.4/", [],
                                          lambda u: called.append(u) or True)
        assert added == "" and not called

    def test_existing_extras_preserved(self):
        base = [{"name": "Extras", "url": "http://m/rocky/9.4/Extras/"}]
        extra, added = complete_appstream("rocky", self.BASEOS, base, lambda u: True)
        assert [e["name"] for e in extra] == ["Extras", "AppStream"]
        assert [e["name"] for e in base] == ["Extras"], "不得就地改动传入列表"
