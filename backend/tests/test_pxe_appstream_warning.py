"""AppStream 缺失护栏用例（2026-10-09，RUNBOOK §5.83.60）。

背景（核查结论）：RHEL 8+ 系里
  · `wget` 只存在于 AppStream；
  · `vim` 不是真包名，唯一 provider `vim-enhanced` 也只在 AppStream；
  · 两者都没有依赖反拉 ⇒ `%packages --ignoremissing` 会**静默少装**，不报错。
所以"生效仓库集合里没有 AppStream"必须显式提示，而不是安静地装出一个缺 vim/wget 的系统。
"""
from app.it.pxe.generator import appstream_warning


class TestAppStreamWarning:
    """纯函数口径：适用/不适用、命中/不命中的边界。"""

    def test_rocky_baseos_only_warns(self):
        w = appstream_warning("rocky", "http://192.168.199.1:8000/pxe/serve/t/repo/rocky-9.4/BaseOS/", [])
        assert w, "只给 BaseOS 时必须警告"
        assert "AppStream" in w and "wget" in w and "vim" in w

    def test_rocky_with_appstream_in_extras_is_silent(self):
        w = appstream_warning(
            "rocky", "http://s/repo/rocky-9.4/BaseOS/",
            [{"name": "AppStream", "url": "http://s/repo/rocky-9.4/AppStream/"}])
        assert w == ""

    def test_appstream_url_string_form_is_silent(self):
        """extra_repos 元素既可能是 dict 也可能是裸字符串（schema 校验为 URL 字符串）。"""
        w = appstream_warning("rocky", "http://s/repo/rocky-9.4/BaseOS/",
                              ["http://s/repo/rocky-9.4/AppStream/"])
        assert w == ""

    def test_appstream_as_main_mirror_is_silent(self):
        """实测线上 Rocky 形态：主 url 就是 AppStream、BaseOS 走额外仓库 ⇒ 不该报。"""
        w = appstream_warning("rocky", "http://s/repo/rocky-9.4/AppStream/",
                              [{"name": "BaseOS", "url": "http://s/repo/rocky-9.4/BaseOS/"}])
        assert w == ""

    def test_alias_and_case_tolerated(self):
        """入参可能带空白/大小写（DB 归一化只发生在保存入口）—— 与 is_rhel_family 同口径。"""
        assert appstream_warning("  ROCKY ", "http://s/BaseOS/", [])
        assert appstream_warning("rhel", "http://s/BaseOS/", [])

    def test_single_repo_families_do_not_warn(self):
        """openEuler 24.03 实测是单仓库（没有 AppStream）⇒ 不能误报；CentOS 7 同理。"""
        assert appstream_warning("openeuler", "http://s/repo/openeuler-24.03/", []) == ""
        assert appstream_warning("centos", "http://s/repo/centos-7/", []) == ""
        assert appstream_warning("kylin", "http://s/repo/kylin/", []) == ""

    def test_ubuntu_does_not_warn(self):
        """非 kickstart 家族（apt 系）不适用本条规则。"""
        assert appstream_warning("ubuntu", "http://s/ubuntu/22.04/", []) == ""

    def test_none_and_empty_are_safe(self):
        assert appstream_warning(None, None, None) == ""
        assert appstream_warning("", "", []) == ""
