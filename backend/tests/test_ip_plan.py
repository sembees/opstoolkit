# -*- coding: utf-8 -*-
"""装机规划表解析与冲突校验（app.it.pxe.ip_plan）的纯逻辑用例。

【核心判定：3 台设备 6 行 → 解析出 3 条】
真实规划表是"多行合并单元格"：同一个设备名下多行、IP 只写在第一行。规划表的
记录单位是"带外管理 IP"——同一个合并的 IP 单元格只属于**一个逻辑设备**（两台
交换机做 IRF/堆叠后共用一个管理地址）；但两行各有一个"设备位置"（不同 U 位），
说明是两台物理机器。所以一个设备名两行 = **两台物理机器、一条逻辑设备记录**，
解析出 3 条记录，每条用 ``rows``/``locations`` 保留全部物理行，供后续 MAC 自动
发现后按物理行配对。若那两行真是各自独立的机器，表里不会共用同一个合并的 IP
单元格（一台 BMC 一个 IP，各自要填自己的地址）。

所有 IP 均为 RFC 5737 文档专用网段（192.0.2.0/24、198.51.100.0/24 等），
设备名/型号均为自造样例，与任何真实内网无关。
"""
import pytest

from app.it.pxe.ip_plan import parse_plan, find_occupied_ips, build_ping_checker

# 用户原文的列结构（多行合并单元格）
CH = ["序号", "设备名称", "设备位置", "带外管理IP地址", "带外管理地址网关", "所属VLAN", "设备型号"]


def _merged_table():
    """3 台设备 6 行（每台 2 行合并单元格），与用户真实表同构。"""
    return [
        CH,
        ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "H3C S7506X-G"],
        ["2", "", "A16U03", "", "", "10", "H3C S7506X-G"],
        ["3", "CORE-SW-B", "A15U38", "192.0.2.15/24", "/", "10", "H3C S6526XE-32X4CC-EI"],
        ["4", "", "A14U39", "", "", "10", "H3C S6526XE-32X4CC-EI"],
        ["5", "FW-DMZ-C", "A18U20", "198.51.100.1/24", "/", "30", "H3C F100-C-G5"],
        ["6", "", "A18U21", "", "", "30", "H3C F100-C-G5"],
    ]


# ---------------------------------------------------------------------------
# 正常解析
# ---------------------------------------------------------------------------
class TestBasicParsing:
    def test_dict_rows_three_devices_full_fields(self):
        """list[dict] 形态：3 台独立设备，字段完整解析（ip/prefix/netmask/network）。"""
        rows = [
            {"序号": 1, "设备名称": "CORE-SW-A", "设备位置": "A17U03",
             "带外管理IP地址": "192.0.2.1/24", "带外管理地址网关": "/",
             "所属VLAN": 10, "设备型号": "H3C S7506X-G"},
            {"序号": 2, "设备名称": "CORE-SW-B", "设备位置": "A16U03",
             "带外管理IP地址": "192.0.2.2/24", "带外管理地址网关": "192.0.2.254",
             "所属VLAN": 10, "设备型号": "H3C S7506X-G"},
            {"序号": 3, "设备名称": "FW-DMZ-C", "设备位置": "A18U20",
             "带外管理IP地址": "198.51.100.1/24", "带外管理地址网关": "198.51.100.254",
             "所属VLAN": 30, "设备型号": "H3C F100-C-G5"},
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert result["warnings"] == []
        assert len(result["devices"]) == 3
        d1, d2 = result["devices"][0], result["devices"][1]
        assert d1["ip"] == "192.0.2.1" and d1["prefix"] == 24
        assert d1["netmask"] == "255.255.255.0" and d1["network"] == "192.0.2.0/24"
        assert d1["gateway"] is None and d1["vlan"] == 10 and d1["row"] == 1
        assert d2["gateway"] == "192.0.2.254" and d2["vlan"] == 10
        assert result["devices"][2]["network"] == "198.51.100.0/24"
        stats = result["stats"]
        assert stats["device_count"] == 3 and stats["data_rows"] == 3
        assert stats["error_count"] == 0 and stats["warning_count"] == 0

    def test_list_rows_with_header_same_result(self):
        """list[list] 形态（第一行表头）与 dict 形态解析结果一致。"""
        result = parse_plan(_merged_table())
        assert len(result["devices"]) == 3
        d1 = result["devices"][0]
        assert d1["name"] == "CORE-SW-A" and d1["ip"] == "192.0.2.1"
        assert d1["prefix"] == 24 and d1["network"] == "192.0.2.0/24"
        assert d1["model"] == "H3C S7506X-G" and d1["seq"] == "1"
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# 合并单元格继承 & 行数判定
# ---------------------------------------------------------------------------
class TestMergedCellInheritance:
    def test_merged_inheritance_two_rows_one_device(self):
        """合并单元格：第 2 行空 name/ip/gateway 向上继承，只出 1 条设备记录。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "H3C S7506X-G"],
            ["2", "", "A16U03", "", "", "10", "H3C S7506X-G"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert len(result["devices"]) == 1
        d = result["devices"][0]
        assert d["name"] == "CORE-SW-A" and d["ip"] == "192.0.2.1" and d["prefix"] == 24
        assert d["gateway"] is None  # 网关 "/" → 无网关
        assert d["vlan"] == 10
        assert d["row"] == 1 and d["rows"] == [1, 2] and d["physical_units"] == 2
        assert d["locations"] == [{"row": 1, "location": "A17U03"},
                                  {"row": 2, "location": "A16U03"}]
        assert d["location"] == "A17U03"  # 首个非空位置作代表
        assert result["stats"]["device_count"] == 1
        assert result["stats"]["physical_unit_count"] == 2
        assert result["stats"]["multi_row_devices"] == 1

    def test_three_devices_six_rows_yield_three_devices(self):
        """3 台设备 6 行 → 3 条记录（判定见模块 docstring：两行=两台物理机器、一条逻辑记录）。

        理由：记录单位是"带外管理 IP"。合并的 IP 单元格说明这两行共用一个管理地址
        （IRF/堆叠共管），是 1 个逻辑设备；两行各有设备位置 → 是 2 台物理机器。
        各行位置/型号必须保留（rows/locations），MAC 自动发现后按物理行配对。
        """
        result = parse_plan(_merged_table())
        assert len(result["devices"]) == 3
        assert [d["name"] for d in result["devices"]] == ["CORE-SW-A", "CORE-SW-B", "FW-DMZ-C"]
        assert [d["rows"] for d in result["devices"]] == [[1, 2], [3, 4], [5, 6]]
        stats = result["stats"]
        assert stats["physical_unit_count"] == 6  # 6 行都是真实的物理机器
        assert stats["device_count"] == 3
        assert stats["multi_row_devices"] == 3

    def test_blank_row_does_not_break_inheritance(self):
        """中间的空行被跳过，且不打断合并继承（carry 跨过空行继续）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M"],
            ["2", "", "A16U03", "", "", "10", "M"],
            ["", "", "", "", "", "", ""],
            ["3", "", "A15U38", "", "", "10", "M"],
        ]
        result = parse_plan(rows)
        assert len(result["devices"]) == 1
        assert result["devices"][0]["rows"] == [1, 2, 4]
        assert result["stats"]["skipped_empty_rows"] == 1
        assert result["errors"] == []

    def test_location_and_model_never_inherited(self):
        """设备位置/型号不继承：空就是空（每行一台物理机器，不能张冠李戴）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "H3C S7506X-G"],
            ["2", "", "", "", "", "", ""],  # 位置/型号为空 → 必须保持为空
        ]
        d = parse_plan(rows)["devices"][0]
        assert d["locations"] == [{"row": 1, "location": "A17U03"},
                                  {"row": 2, "location": None}]
        assert d["location"] == "A17U03"  # 取第一个非空位置作代表
        # 反向：首行位置为空、次行有值 → 代表位置取第一个非空的
        rows2 = [
            CH,
            ["1", "CORE-SW-A", "", "192.0.2.1/24", "/", "10", ""],
            ["2", "", "A16U03", "", "", "10", "H3C S7506X-G"],
        ]
        d2 = parse_plan(rows2)["devices"][0]
        assert d2["locations"][0]["location"] is None
        assert d2["location"] == "A16U03" and d2["model"] == "H3C S7506X-G"

    def test_new_device_boundary_stops_inheritance(self):
        """显式写了新设备名的行不继承上一台设备的 IP/网关/VLAN（避免假"IP 重复"）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "192.0.2.254", "10", "M1"],
            ["2", "CORE-SW-B", "A16U03", "", "", "", ""],  # 新设备，IP 忘了填
        ]
        result = parse_plan(rows)
        assert len(result["devices"]) == 2
        assert result["devices"][1]["ip"] is None
        assert result["devices"][1]["gateway"] is None
        fields = {"ip", "name"}
        assert all(e["field"] in fields for e in result["errors"])
        assert any("缺少 IP" in e["message"] for e in result["errors"])


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------
class TestGateway:
    @pytest.mark.parametrize("none_gw", ["/", "-", "无"])
    def test_gateway_none_marks(self, none_gw):
        """网关写 "/"（或 "-"、"无"）= 无网关，跳过同网段校验。"""
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", none_gw, "10", "M"]]
        result = parse_plan(rows)
        assert result["devices"][0]["gateway"] is None
        assert result["errors"] == []

    def test_gateway_out_of_subnet_error(self):
        """网关不在设备所在网段 → 报错（中文原因 + 行号）。"""
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "192.0.2.10/24", "192.0.3.254", "10", "M"]]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "gateway" and err["row"] == 1
        assert "不在" in err["message"] and "网段" in err["message"]

    def test_gateway_in_subnet_ok(self):
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "192.0.2.10/24", "192.0.2.254", "10", "M"]]
        assert parse_plan(rows)["errors"] == []

    def test_gateway_invalid_format_error(self):
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "192.0.2.254", "10", "M"],
            ["2", "FW-DMZ-B", "A18U20", "198.51.100.1/24", "abc", "30", "M2"],
        ]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "gateway" and err["row"] == 2
        assert "不是合法的 IP 地址" in err["message"]
        assert result["devices"][0]["gateway"] == "192.0.2.254"  # 正常设备不受影响

    def test_gateway_inherited_from_merged_cell(self):
        """第 1 行写了网关、第 2 行空（合并单元格）→ 继承后仍是同一设备的网关。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "192.0.2.254", "10", "M"],
            ["2", "", "A16U03", "", "", "10", "M"],
        ]
        d = parse_plan(rows)["devices"][0]
        assert d["gateway"] == "192.0.2.254"
        assert parse_plan(rows)["errors"] == []

    def test_gateway_slash_inherits_as_none(self):
        """第 1 行网关 "/"（无网关）、第 2 行空 → 继承"无网关"，不会继承到别的设备网关。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M"],
            ["2", "", "A16U03", "", "", "10", "M"],
            ["3", "FW-DMZ-B", "A18U20", "198.51.100.1/24", "198.51.100.254", "30", "M2"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert result["devices"][0]["gateway"] is None
        assert result["devices"][1]["gateway"] == "198.51.100.254"


# ---------------------------------------------------------------------------
# IP 与掩码
# ---------------------------------------------------------------------------
class TestIpAndMask:
    def test_duplicate_ip_error(self):
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M1"],
            ["2", "CORE-SW-B", "A16U03", "192.0.2.1/24", "/", "10", "M2"],
        ]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "ip" and err["row"] == 2
        assert "重复" in err["message"] and "192.0.2.1" in err["message"]
        assert "1" in err["message"] and "2" in err["message"]  # 指到两行行号

    def test_duplicate_name_error(self):
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M1"],
            ["2", "CORE-SW-A", "A16U03", "192.0.2.2/24", "/", "10", "M2"],
        ]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "name" and err["row"] == 2
        assert "重复" in err["message"]

    def test_same_name_and_ip_reappearing_reports_error(self):
        """同样的名字+IP 在不相邻行再次出现 → 报错（合并继承后一条设备只应占一段连续行）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M1"],
            ["2", "CORE-SW-B", "A16U03", "192.0.2.2/24", "/", "10", "M2"],
            ["3", "CORE-SW-A", "A15U38", "192.0.2.1/24", "/", "10", "M1"],
        ]
        result = parse_plan(rows)
        assert len(result["devices"]) == 3
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert "重复出现" in err["message"] and "只应占一段连续行" in err["message"]

    def test_invalid_ip_format(self):
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "300.1.1.1/24", "/", "10", "M"]]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "ip" and err["row"] == 1
        assert "不是合法的 IP 地址" in err["message"]
        d = result["devices"][0]
        assert d["ip"] == "300.1.1.1/24" and d["prefix"] is None  # 原样保留便于回显

    @pytest.mark.parametrize("bad", ["192.0.2.1/33", "192.0.2.1/abc", "192.0.2.1/-1",
                                     "192.0.2.1/255.0.255.0"])
    def test_invalid_netmask(self, bad):
        """掩码非法：/33、非数字、负数、不连续点分掩码。"""
        rows = [CH, ["1", "CORE-SW-A", "A17U03", bad, "/", "10", "M"]]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "ip"
        assert "掩码" in err["message"]

    def test_ip_mask_space_form(self):
        """兼容 "IP 掩码" 空格写法与点分掩码写法。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1 255.255.255.0", "/", "10", "M1"],
            ["2", "FW-DMZ-B", "A18U20", "198.51.100.1/255.255.255.0", "/", "30", "M2"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert result["devices"][0]["prefix"] == 24
        assert result["devices"][0]["netmask"] == "255.255.255.0"
        assert result["devices"][1]["network"] == "198.51.100.0/24"

    def test_bare_ip_without_mask_warns(self):
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "192.0.2.1", "192.0.2.254", "10", "M"]]
        result = parse_plan(rows)
        assert result["errors"] == []  # 不是错误，是告警
        assert any("未携带掩码" in w["message"] for w in result["warnings"])
        d = result["devices"][0]
        assert d["ip"] == "192.0.2.1" and d["prefix"] is None and d["network"] is None


# ---------------------------------------------------------------------------
# VLAN
# ---------------------------------------------------------------------------
class TestVlan:
    def test_vlan_non_numeric_error(self):
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "VLAN10", "M"]]
        result = parse_plan(rows)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "vlan" and err["row"] == 1
        assert "不是纯数字" in err["message"]
        assert result["devices"][0]["vlan"] == "VLAN10"  # 原样保留

    @pytest.mark.parametrize("vlan", ["0", "4095"])
    def test_vlan_out_of_range_warns(self, vlan):
        """VLAN 是数字但超出 1-4094 → 告警（不作为硬错误）。"""
        rows = [CH, ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", vlan, "M"]]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert any("1-4094" in w["message"] for w in result["warnings"])

    def test_vlan_conflict_within_device_warns(self):
        """同一设备两行 VLAN 不同 → 告警并取首行值（合并单元格本应一致）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M"],
            ["2", "", "A16U03", "", "", "20", "M"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert any("VLAN 不一致" in w["message"] and w["row"] == 2 for w in result["warnings"])
        assert result["devices"][0]["vlan"] == 10

    def test_model_conflict_within_device_warns(self):
        """同一设备两行型号不一致 → 告警（位置/型号不继承，但不一致要提醒核对）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "H3C S7506X-G"],
            ["2", "", "A16U03", "", "", "10", "H3C S6526XE-32X4CC-EI"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert any("型号不一致" in w["message"] for w in result["warnings"])
        assert result["devices"][0]["model"] == "H3C S7506X-G"  # 取首行


# ---------------------------------------------------------------------------
# 列名兼容
# ---------------------------------------------------------------------------
class TestHeaderAliases:
    def test_chinese_header_variants(self):
        """中文长名/带空格/全角字符的表头都能识别。"""
        rows = [
            ["序号", "设备 名称", "设备位置", "带外管理ＩＰ地址", "带外管理地址网关",
             "所属ＶＬＡＮ", "设备型号"],
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        d = result["devices"][0]
        assert d["name"] == "CORE-SW-A" and d["ip"] == "192.0.2.1"
        assert result["stats"]["columns"]["detected"]["ip"] == "带外管理ＩＰ地址"

    def test_english_header_aliases(self):
        rows = [
            ["No", "Name", "Location", "IP", "Gateway", "VLAN", "Model"],
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "192.0.2.254", 10, "M"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        d = result["devices"][0]
        assert d["name"] == "CORE-SW-A" and d["gateway"] == "192.0.2.254" and d["vlan"] == 10

    def test_mixed_language_headers(self):
        rows = [
            ["Index", "设备名称", "Rack", "管理IP", "网关", "VLAN ID", "Model"],
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "192.0.2.254", 10, "M"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert result["devices"][0]["network"] == "192.0.2.0/24"

    def test_unknown_extra_column_ignored(self):
        rows = [CH + ["备注"], ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M", "核心"]]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert len(result["devices"]) == 1
        assert result["stats"]["columns"]["ignored"] == ["备注"]

    def test_short_word_headers_do_not_misfire(self):
        """description 不该被当成 IP 列、notes 不该被当成序号列（词边界保护）。"""
        rows = [
            ["编号", "设备名称", "Description", "带外管理IP地址", "网关", "VLAN", "Model"],
            ["1", "CORE-SW-A", "desc", "192.0.2.1/24", "/", 10, "M"],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        detected = result["stats"]["columns"]["detected"]
        assert detected["ip"] == "带外管理IP地址"
        assert result["stats"]["columns"]["ignored"] == ["Description"]


# ---------------------------------------------------------------------------
# 缺列 / 空表 / 异常输入
# ---------------------------------------------------------------------------
class TestMissingAndEmpty:
    def test_missing_ip_column_is_fatal(self):
        rows = [["序号", "设备名称", "所属VLAN"], ["1", "CORE-SW-A", 10]]
        result = parse_plan(rows)
        assert result["devices"] == []
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["field"] == "header" and err["row"] == 0
        assert "缺少 IP 列" in err["message"]

    def test_undetectable_header_is_fatal(self):
        rows = [["foo", "bar"], ["1", "2"]]
        result = parse_plan(rows)
        assert result["devices"] == []
        assert any("无法识别表头" in e["message"] for e in result["errors"])

    def test_missing_name_column_warns_but_parses(self):
        rows = [
            ["序号", "设备位置", "带外管理IP地址", "所属VLAN"],
            ["1", "A17U03", "192.0.2.1/24", 10],
        ]
        result = parse_plan(rows)
        assert result["errors"] == []
        assert any("设备名称" in w["message"] for w in result["warnings"])
        d = result["devices"][0]
        assert d["name"] is None and d["ip"] == "192.0.2.1"

    @pytest.mark.parametrize("rows", [[], [CH]], ids=["empty-input", "header-only"])
    def test_empty_inputs(self, rows):
        result = parse_plan(rows)
        assert result["devices"] == [] and result["errors"] == []
        assert len(result["warnings"]) == 1
        assert result["stats"]["data_rows"] == 0

    def test_malformed_row_skipped(self):
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M"],
            "not-a-row",
        ]
        result = parse_plan(rows)
        assert len(result["devices"]) == 1
        assert any("不是字典/列表" in e["message"] and e["row"] == 2 for e in result["errors"])

    def test_non_sequence_input_raises(self):
        with pytest.raises(TypeError):
            parse_plan("1,2,3")
        with pytest.raises(TypeError):
            parse_plan({"设备名称": "X"})  # 单个 dict 不是行列表


# ---------------------------------------------------------------------------
# 单元格取值形态
# ---------------------------------------------------------------------------
class TestCellValues:
    def test_excel_like_cell_types_normalized(self):
        """Excel 数值（int/float）、带空格文本都能正确规整。"""
        rows = [{
            "序号": 1,
            "设备名称": " CORE-SW-A ",
            "设备位置": "A17U03",
            "带外管理IP地址": " 192.0.2.5/24 ",
            "带外管理地址网关": "192.0.2.254 ",
            "所属VLAN": 10.0,
        }]
        d = parse_plan(rows)["devices"][0]
        assert d["seq"] == "1" and d["name"] == "CORE-SW-A"
        assert d["ip"] == "192.0.2.5" and d["prefix"] == 24
        assert d["gateway"] == "192.0.2.254" and d["vlan"] == 10  # 10.0 → int 10

    def test_explicit_duplicate_rows_merge_with_warning(self):
        """名称/IP 在两行都被显式填写且相同 → 合并为一条 + 告警（可能是两台机器误用同 IP）。"""
        rows = [
            CH,
            ["1", "CORE-SW-A", "A17U03", "192.0.2.1/24", "/", "10", "M"],
            ["2", "CORE-SW-A", "A16U03", "192.0.2.1/24", "/", "10", "M"],
        ]
        result = parse_plan(rows)
        assert len(result["devices"]) == 1
        assert result["devices"][0]["rows"] == [1, 2]
        assert result["errors"] == []
        assert any("显式填写" in w["message"] for w in result["warnings"])

    def test_merged_rows_produce_no_warning(self):
        """正常合并单元格继承（后续行名称/IP 为空）不应产生任何告警。"""
        result = parse_plan(_merged_table())
        assert result["warnings"] == []


# ---------------------------------------------------------------------------
# 可选占用探测（默认关闭，本模块零网络）
# ---------------------------------------------------------------------------
class TestOccupancyInterface:
    def test_default_off_no_network_and_no_stats_key(self):
        """默认 occupancy_checker=None：不探测、stats 无占用键（parse_plan 零网络）。"""
        result = parse_plan(_merged_table())
        assert "occupancy_probed" not in result["stats"]
        assert "occupancy_occupied" not in result["stats"]
        assert find_occupied_ips(result, None) == []

    def test_opt_in_checker_flags_occupied_ips(self):
        occupied_ip = "192.0.2.1"

        def fake_checker(ip):
            return True if ip == occupied_ip else False

        result = parse_plan(_merged_table(), occupancy_checker=lambda ip: ip == occupied_ip)
        assert result["stats"]["occupancy_probed"] == 3  # 3 个唯一 IP 都探测了
        assert result["stats"]["occupancy_occupied"] == 1
        flagged = [w for w in result["warnings"] if "占用" in w["message"]]
        assert len(flagged) == 1 and "192.0.2.1" in flagged[0]["message"]
        assert result["errors"] == []

        findings = find_occupied_ips(parse_plan(_merged_table()), lambda ip: ip == occupied_ip)
        assert len(findings) == 3  # 每个唯一 IP 一条
        assert {f["occupied"] for f in findings} == {True, False}
        assert next(f for f in findings if f["occupied"])["ip"] == occupied_ip

    def test_checker_exception_does_not_break(self):
        def bad_checker(ip):
            raise RuntimeError("probe down")

        findings = find_occupied_ips(parse_plan(_merged_table()), bad_checker)
        assert len(findings) == 3  # 每个唯一 IP 一条
        assert all(f["occupied"] is None for f in findings)  # 探测失败 ≠ 占用
        result = parse_plan(_merged_table(), occupancy_checker=bad_checker)
        assert result["devices"]  # 解析不受影响
        assert result["errors"] == []
