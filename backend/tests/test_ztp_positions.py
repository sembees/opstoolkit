# -*- coding: utf-8 -*-
"""ZTP 落位登记 + MAC 认领测试。

背景：设备到货时只有「落位 + 规划管理 IP + 规划主机名」，没有 MAC（还没上电）。
覆盖：
  1. norm_mac 三种写法归一 + 非法输入返回空串；
  2. parse_leases 解析 dnsmasq 租约（* 主机名 / client-id / 注释行）；
  3. parse_positions_csv（正常行 + IP 非法 + 落位为空，错误文本含行号）；
  4. 落位 HTTP 接口（创建 200 / 管理 IP 非法 422 / 落位重复 409 / 模板不存在 404）；
  5. POST /claim：认领后 mac/claimed_at/source 正确；重复认领到另一落位 → 409；
  6. generate_all(positions=...)：有 mac 的落位出现 dhcp-host= 且 bootfile 指向正确
     文件名；无 mac 的落位只有注释、不出现 dhcp-host=；两台落位各自的配置文件都在
     files 里。ZTP 生成器没有口令就抛 ValueError —— 这里显式给测试口令。
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core import models  # noqa: E402
from app.ct.ztp import positions as ztp_positions  # noqa: E402
from app.ct.ztp.generator import ZtpDevice, ZtpProfile, generate_all  # noqa: E402

LEASES_TEXT = (
    "# 注释行 / duid 行都要忽略\n"
    "1700000000 aa:bb:cc:dd:ee:01 10.0.0.11 SW01 01:aa:bb:cc:dd:ee:01\n"
    "1700000001 AA-BB-CC-DD-EE-02 10.0.0.12 *\n"
    "1700000002 aabb.ccdd.ee03 10.0.0.13 sw03\n"
    "this line is broken\n"
)


class NormMacTest(unittest.TestCase):
    def test_three_common_forms_normalize(self):
        expect = "aa:bb:cc:dd:ee:ff"
        for raw in ("aabb.ccdd.eeff", "AA:BB:CC:DD:EE:FF", "aabbccddeeff",
                    "AA-BB-CC-DD-EE-FF"):
            with self.subTest(raw=raw):
                self.assertEqual(ztp_positions.norm_mac(raw), expect)

    def test_invalid_inputs_return_empty(self):
        for raw in ("", None, "hello", "aa:bb:cc:dd:ee", "aabb.ccdd.eefff",
                    "zz:bb:cc:dd:ee:ff", "12"):
            with self.subTest(raw=raw):
                self.assertEqual(ztp_positions.norm_mac(raw), "")


class ParseLeasesTest(unittest.TestCase):
    def test_parse_fixture(self):
        records = ztp_positions.parse_leases(LEASES_TEXT)
        self.assertEqual(len(records), 3)
        r0 = records[0]
        self.assertEqual(r0["mac"], "aa:bb:cc:dd:ee:01")
        self.assertEqual(r0["ip"], "10.0.0.11")
        self.assertEqual(r0["hostname"], "SW01")
        self.assertEqual(r0["client_id"], "01:aa:bb:cc:dd:ee:01")
        self.assertEqual(r0["expires"], "1700000000")
        # `*` 主机名置空；大写连字符写法归一
        self.assertEqual(records[1]["mac"], "aa:bb:cc:dd:ee:02")
        self.assertEqual(records[1]["hostname"], "")
        self.assertEqual(records[1]["client_id"], "")
        # 点分写法归一
        self.assertEqual(records[2]["mac"], "aa:bb:cc:dd:ee:03")
        self.assertEqual(records[2]["hostname"], "sw03")

    def test_empty_input(self):
        self.assertEqual(ztp_positions.parse_leases(""), [])
        self.assertEqual(ztp_positions.parse_leases(None), [])


class LeasesCandidatesTest(unittest.TestCase):
    def test_order_with_env_first(self):
        env = os.environ.get("OPS_DNSMASQ_LEASES")
        os.environ["OPS_DNSMASQ_LEASES"] = "/x/leases"
        try:
            self.assertEqual(ztp_positions.leases_candidates(), [
                "/x/leases",
                # 实测（dnsmasq 2.85，/proc/<pid>/fd 取证）：/var/lib/dnsmasq/ 在前
                "/var/lib/dnsmasq/dnsmasq.leases",
                "/var/lib/misc/dnsmasq.leases",
                "/var/lib/misc/dnsmasq/dnsmasq.leases",
            ])
        finally:
            if env is None:
                os.environ.pop("OPS_DNSMASQ_LEASES", None)
            else:
                os.environ["OPS_DNSMASQ_LEASES"] = env

    def test_order_without_env(self):
        env = os.environ.pop("OPS_DNSMASQ_LEASES", None)
        try:
            self.assertEqual(ztp_positions.leases_candidates(), [
                "/var/lib/dnsmasq/dnsmasq.leases",
                "/var/lib/misc/dnsmasq.leases",
                "/var/lib/misc/dnsmasq/dnsmasq.leases",
            ])
        finally:
            if env is not None:
                os.environ["OPS_DNSMASQ_LEASES"] = env


class ReadLeasesTest(unittest.TestCase):
    def test_reads_first_readable_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "dnsmasq.leases"
            f.write_text("1700000000 aa:bb:cc:dd:ee:01 10.0.0.11 SW01\n", encoding="utf-8")
            orig = ztp_positions.leases_candidates
            ztp_positions.leases_candidates = lambda: [str(pathlib.Path(td, "no.leases")), str(f)]
            try:
                path, note, records = ztp_positions.read_leases()
            finally:
                ztp_positions.leases_candidates = orig
            self.assertEqual(path, str(f))
            self.assertIn(str(f), note)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["mac"], "aa:bb:cc:dd:ee:01")

    def test_unreadable_returns_note_never_raises(self):
        orig = ztp_positions.leases_candidates
        ztp_positions.leases_candidates = lambda: ["Z:/definitely/not/here.leases"]
        try:
            path, note, records = ztp_positions.read_leases()
        finally:
            ztp_positions.leases_candidates = orig
        self.assertEqual(path, "")
        self.assertEqual(records, [])
        self.assertIn("读取不到 dnsmasq 租约文件", note)
        self.assertIn("OPS_DNSMASQ_LEASES", note)
        self.assertIn("Z:/definitely/not/here.leases", note)


class ParsePositionsCsvTest(unittest.TestCase):
    CSV_TEXT = (
        "落位,管理IP,主机名,序列号,MAC,备注\n"
        "A01-03-U11,10.0.0.11,sw11,SN11,AA-BB-CC-DD-EE-11,web 接入\n"
        "A01-03-U12,10.0.0.12,sw12,SN12,,\n"
        "A01-03-U13,10.0.0.13,sw13,SN13,aabb.ccdd.ee13,核心\n"
        "A01-03-U14,999.1.1.1,sw14,,,非法 IP\n"
        ",10.0.0.15,sw15,,,落位为空\n"
    )

    def test_parse_with_chinese_header(self):
        rows, errors = ztp_positions.parse_positions_csv(self.CSV_TEXT)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(errors), 2)
        self.assertEqual(rows[0]["position"], "A01-03-U11")
        self.assertEqual(rows[0]["mgmt_ip"], "10.0.0.11")  # 保留原字符串
        self.assertEqual(rows[0]["hostname"], "sw11")
        self.assertEqual(rows[0]["serial"], "SN11")
        self.assertEqual(rows[0]["remark"], "web 接入")
        # 错误文本含行号（N 从 1 起、含表头行）：非法 IP 在第 5 行、落位为空在第 6 行
        self.assertIn("第5行", errors[0])
        self.assertIn("999.1.1.1", errors[0])
        self.assertIn("第6行", errors[1])
        self.assertIn("落位为空", errors[1])

    def test_parse_with_english_header(self):
        csv_text = "position,mgmt_ip,hostname,serial,mac,remark\nB02-U01,10.0.1.1,host1,SN1,,x\n"
        rows, errors = ztp_positions.parse_positions_csv(csv_text)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position"], "B02-U01")
        self.assertEqual(rows[0]["mgmt_ip"], "10.0.1.1")

    def test_parse_without_header_fixed_columns(self):
        csv_text = "A01-01-U01,10.9.9.9,sw9,SN9,aa:bb:cc:dd:ee:09,备注\n"
        rows, errors = ztp_positions.parse_positions_csv(csv_text)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position"], "A01-01-U01")
        self.assertEqual(rows[0]["hostname"], "sw9")
        self.assertEqual(rows[0]["remark"], "备注")

    # ---- 外部审查 U3-2nd-F10：表头判定与别名表不一致 + 子串误映射 ----

    def test_header_using_position_aliases_is_recognized(self):
        """表头写「机位/位置」时也要认出来（改前只认 position/落位）。

        改前的后果：整表按固定列序解析（列序一变就错位），而且表头行本身被当成数据，
        报一条误导性的「管理 IP 非法：管理IP」。
        """
        for head in ("机位", "位置"):
            with self.subTest(head=head):
                rows, errors = ztp_positions.parse_positions_csv(
                    "%s,管理IP,主机名,序列号,MAC,备注\nA01-03-U31,10.0.0.31,sw31,SN31,,x\n" % head)
                self.assertEqual(errors, [], errors)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["position"], "A01-03-U31")
                self.assertEqual(rows[0]["mgmt_ip"], "10.0.0.31")
                self.assertEqual(rows[0]["hostname"], "sw31")

    def test_decoys_are_not_mapped_to_mgmt_ip_or_serial(self):
        """`IPMI地址` / `SNMP社区` 这类列不能被当成管理 IP / 序列号。

        改前：`"ip" in "ipmi地址"` 为真 ⇒ 该列被当成 mgmt_ip，而**真正的「管理IP」列**
        因为 key 已被占用被丢弃；`SNMP社区` 命中 `sn` ⇒ 被当成序列号。导入进来一批
        张冠李戴的数据，接口还报成功。
        """
        csv_text = ("落位,IPMI地址,管理IP,SNMP社区,主机名,MAC\n"
                    "A01-03-U41,192.168.9.9,10.0.0.41,public,sw41,aa:bb:cc:dd:ee:41\n")
        rows, errors = ztp_positions.parse_positions_csv(csv_text)
        self.assertEqual(errors, [], errors)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mgmt_ip"], "10.0.0.41")   # 不是 IPMI 那个地址
        self.assertEqual(rows[0]["serial"], "")             # 不是 SNMP 团体名
        self.assertEqual(rows[0]["hostname"], "sw41")
        self.assertEqual(rows[0]["mac"], "aa:bb:cc:dd:ee:41")

    def test_short_aliases_still_work_at_word_boundary(self):
        """`IP地址`/`SN号` 这类常见写法仍要认（短别名按整词边界匹配）。"""
        rows, errors = ztp_positions.parse_positions_csv(
            "落位,IP地址,SN号,主机名\nA01-03-U51,10.0.0.51,SN51,sw51\n")
        self.assertEqual(errors, [], errors)
        self.assertEqual(rows[0]["mgmt_ip"], "10.0.0.51")
        self.assertEqual(rows[0]["serial"], "SN51")

    def test_header_alias_spacing_and_case_insensitive(self):
        # 管理 IP 的各种写法（注意：数据行必须有**合法**管理 IP，否则整行被跳过）
        for head in ("管理 IP", "管理_ip", "管理ip地址", "MANAGEMENT_IP"):
            with self.subTest(head=head):
                rows, errors = ztp_positions.parse_positions_csv(
                    "落位,%s,MAC\nA01-03-U61,10.0.0.61,aa:bb:cc:dd:ee:61\n" % head)
                self.assertEqual(errors, [], errors)
                self.assertEqual(rows[0]["mgmt_ip"], "10.0.0.61")
        # 主机名的大小写/下划线写法
        for head in ("HostName", "host_name", "主机名"):
            with self.subTest(head=head):
                rows, errors = ztp_positions.parse_positions_csv(
                    "落位,管理IP,%s\nA01-03-U61,10.0.0.61,sw61\n" % head)
                self.assertEqual(errors, [], errors)
                self.assertEqual(rows[0]["hostname"], "sw61")

    def test_data_rows_are_not_mistaken_for_headers(self):
        """无表头时不能被误判成表头（数据行里认不出 ≥2 个字段名）。"""
        csv_text = ("A01-01-U01,10.9.9.9,sw9,SN9,aa:bb:cc:dd:ee:09,备注\n"
                    "A01-01-U02,10.9.9.10,sw10,SN10,aa:bb:cc:dd:ee:10,说明\n")
        rows, errors = ztp_positions.parse_positions_csv(csv_text)
        self.assertEqual(errors, [], errors)
        self.assertEqual([r["position"] for r in rows], ["A01-01-U01", "A01-01-U02"])
        self.assertEqual(rows[1]["mgmt_ip"], "10.9.9.10")


class _ApiTestBase(unittest.TestCase):
    """落位 HTTP 接口测试：临时文件 SQLite + 覆盖 JWT/DB 依赖，不碰真实库。

    NullPool 的原因：TestClient 的每个请求跑在**自己的事件循环**里，而测试里用
    asyncio.run 播种数据又是另一个循环 —— 池化的 aiosqlite 连接会绑死在创建它的
    循环上（跨循环复用直接报错），所以连接即用即建、用完即关。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(cls._tmp.name) / "positions-test.db"
        cls.engine = create_async_engine(
            "sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
        cls.SessionLocal = async_sessionmaker(cls.engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.engine.dispose())
        cls._tmp.cleanup()

    def setUp(self):
        async def _reset():
            async with self.engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
        asyncio.run(_reset())

    def _seed_template(self, name="t") -> str:
        async def _go():
            async with self.SessionLocal() as s:
                t = models.ZtpTemplate(name=name, vendor="h3c")
                s.add(t)
                await s.commit()
                await s.refresh(t)
                return t.id
        return asyncio.run(_go())

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import ztp as ztp_api
        from app.core.auth import get_current_user
        from app.database import get_db

        app = FastAPI()
        app.include_router(ztp_api.router, prefix="/api/ct/ztp")

        async def _override_db():
            async with self.SessionLocal() as session:
                yield session

        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        app.dependency_overrides[get_db] = _override_db
        return TestClient(app)

    def _create(self, client, tid, position="A01-03-U12", mgmt_ip="10.0.0.12", **kw):
        payload = {"template_id": tid, "position": position, "mgmt_ip": mgmt_ip,
                   "hostname": kw.get("hostname", "sw12"), "serial": kw.get("serial", "SN12"),
                   "mac": kw.get("mac", ""), "remark": kw.get("remark", "")}
        return client.post("/api/ct/ztp/positions", json=payload)


class ZtpPositionsApiTest(_ApiTestBase):
    def test_create_position_returns_200_and_defaults(self):
        client = self._client()
        tid = self._seed_template("T1")
        r = self._create(client, tid)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["position"], "A01-03-U12")
        self.assertEqual(data["template_id"], tid)
        self.assertEqual(data["mgmt_ip"], "10.0.0.12")
        self.assertEqual(data["mac"], "")
        self.assertIsNone(data["claimed_at"])
        self.assertEqual(data["source"], "manual")
        r = client.get("/api/ct/ztp/positions", params={"template_id": tid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["position"] for x in r.json()], ["A01-03-U12"])
        r = client.get("/api/ct/ztp/positions")
        self.assertEqual(len(r.json()), 1)

    def test_mgmt_ip_invalid_is_422(self):
        client = self._client()
        tid = self._seed_template("T2")
        r = self._create(client, tid, mgmt_ip="999.1.1.1")
        self.assertEqual(r.status_code, 422, r.text)
        # 校验层次变了（外部审查 U7-F8）：现在由 **schemas** 在入参阶段就拒（更早更严），
        # 所以拿到的是 pydantic 的错误结构，字段路径在 loc 里；接口层的中文提示成了第二道。
        detail = r.json()["detail"]
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        self.assertTrue("mgmt_ip" in text or "管理 IP" in text, text)

    def test_mgmt_ip_non_canonical_is_rejected(self):
        """U7-F8：`10.0.0.11 ` / `010.0.0.11` 这类非规范写法必须被拒 ——
        否则它们与规范写法是两个不同字符串，(template_id, mgmt_ip) 唯一约束就被绕过了。"""
        client = self._client()
        tid = self._seed_template("T2b")
        self.assertEqual(self._create(client, tid, position="A01",
                                      mgmt_ip="10.0.0.11").status_code, 200)
        for bad in ("010.0.0.12", "10.0.0.12 ", "10.0.0.12\t"):
            with self.subTest(bad=bad):
                r = self._create(client, tid, position="A02", mgmt_ip=bad)
                self.assertEqual(r.status_code, 422, r.text)

    def test_position_and_hostname_cannot_carry_injection(self):
        """U7-F3/F8：落位/主机名会进 dnsmasq 配置与设备 CLI，不能带换行/引号/空格。"""
        client = self._client()
        tid = self._seed_template("T2c")
        for bad in ("A01\nport=0", 'A01"x', "A01 U12"):
            with self.subTest(bad=bad):
                r = self._create(client, tid, position=bad, mgmt_ip="10.0.0.13")
                self.assertEqual(r.status_code, 422, r.text)
        r = self._create(client, tid, hostname="SW1\nport=0", mgmt_ip="10.0.0.14")
        self.assertEqual(r.status_code, 422, r.text)

    def test_duplicate_position_is_409(self):
        client = self._client()
        tid = self._seed_template("T3")
        self.assertEqual(self._create(client, tid).status_code, 200)
        r = self._create(client, tid, mgmt_ip="10.0.0.99")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("已存在落位 A01-03-U12", r.json()["detail"])

    def test_template_missing_is_404(self):
        client = self._client()
        r = self._create(client, "no-such-template")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["detail"], "模板不存在")
        r = client.post("/api/ct/ztp/positions",
                        json={"template_id": "", "position": "X", "mgmt_ip": "10.0.0.1"})
        self.assertEqual(r.status_code, 404)

    def test_update_and_delete_position(self):
        client = self._client()
        tid = self._seed_template("T8")
        pid = self._create(client, tid, position="A01-03-U51", mgmt_ip="10.0.0.51").json()["id"]
        r = client.put("/api/ct/ztp/positions/" + pid,
                       json={"template_id": tid, "position": "A01-03-U52", "mgmt_ip": "10.0.0.52"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["position"], "A01-03-U52")
        self._create(client, tid, position="A01-03-U53", mgmt_ip="10.0.0.53")
        r = client.put("/api/ct/ztp/positions/" + pid,
                       json={"template_id": tid, "position": "A01-03-U53", "mgmt_ip": "10.0.0.52"})
        self.assertEqual(r.status_code, 409, r.text)
        r = client.delete("/api/ct/ztp/positions/" + pid)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        r = client.get("/api/ct/ztp/positions", params={"template_id": tid})
        self.assertEqual([x["position"] for x in r.json()], ["A01-03-U53"])

    def test_import_create_update_and_errors(self):
        client = self._client()
        tid = self._seed_template("T5")
        csv_text = (
            "落位,管理IP,主机名,序列号,MAC,备注\n"
            "A01-03-U31,10.0.0.31,sw31,SN31,,\n"
            "A01-03-U32,999.9.9.9,sw32,,,\n"
            ",10.0.0.33,sw33,,,\n"
        )
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "csv": csv_text})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["created"], 1)
        self.assertEqual(out["updated"], 0)
        self.assertEqual(out["skipped"], 2)
        self.assertEqual(len(out["errors"]), 2)
        self.assertIn("第3行", out["errors"][0])
        self.assertIn("第4行", out["errors"][1])
        # 相同落位再导 → 更新
        csv2 = "A01-03-U31,10.0.0.31,sw31x,SN31,,改名\n"
        r = client.post("/api/ct/ztp/positions/import", json={"template_id": tid, "csv": csv2})
        out = r.json()
        self.assertEqual(out["created"], 0)
        self.assertEqual(out["updated"], 1)
        # replace=True：先清空再导入
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "csv": csv2, "replace": True})
        out = r.json()
        self.assertEqual(out["created"], 1)
        r = client.get("/api/ct/ztp/positions", params={"template_id": tid})
        self.assertEqual(len(r.json()), 1)
        self.assertEqual(r.json()[0]["hostname"], "sw31x")
        # 模板不存在 → 404
        r = client.post("/api/ct/ztp/positions/import", json={"template_id": "nope", "csv": "a,b"})
        self.assertEqual(r.status_code, 404)

    def test_observations_match_claimed_positions(self):
        """★ 外部审查 U6-F5：这里原来把 `read_leases` 整个换成桩、再断言桩自己返回的字符串
        （`leases_path == "/tmp/leases"` 必然成立）。现在只把**候选路径**指向一个真的临时
        租约文件，`read_leases` 走真实实现：解析、路径回填、说明文字都是被测代码产出的。
        """
        from app.api import ztp as ztp_api
        client = self._client()
        tid = self._seed_template("T6")
        self._create(client, tid, position="A01-03-U41", mgmt_ip="10.0.0.41", hostname="sw41")
        positions = client.get("/api/ct/ztp/positions", params={"template_id": tid}).json()
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": positions[0]["id"], "mac": "aa:bb:cc:dd:ee:41"})
        self.assertEqual(r.status_code, 200, r.text)
        leases = (
            "1700000000 aa:bb:cc:dd:ee:41 10.0.0.77 sw41-lease 01:aa:bb:cc:dd:ee:41\n"
            "1700000000 aa:bb:cc:dd:ee:99 10.0.0.99 *\n"
        )
        fd, lease_path = tempfile.mkstemp(suffix=".leases")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(leases)
        orig = ztp_positions.leases_candidates
        ztp_positions.leases_candidates = lambda: [lease_path]
        try:
            r = client.get("/api/ct/ztp/observations", params={"template_id": tid})
        finally:
            ztp_positions.leases_candidates = orig
            os.unlink(lease_path)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertTrue(out["ok"])
        # 路径与说明来自**真实** read_leases（读到的是我们写的那个文件）
        self.assertEqual(out["leases_path"], lease_path)
        self.assertIn("2 条", out["note"])
        self.assertEqual(len(out["observations"]), 2)
        obs0 = next(o for o in out["observations"] if o["mac"] == "aa:bb:cc:dd:ee:41")
        self.assertEqual(obs0["ip"], "10.0.0.77")
        self.assertEqual(obs0["hostname"], "sw41-lease")
        self.assertEqual(obs0["client_id"], "01:aa:bb:cc:dd:ee:41")
        self.assertEqual(obs0["position_id"], positions[0]["id"])
        self.assertEqual(obs0["position"], "A01-03-U41")
        self.assertEqual(obs0["claimed_hostname"], "sw41")
        obs1 = next(o for o in out["observations"] if o["mac"] == "aa:bb:cc:dd:ee:99")
        self.assertEqual(obs1["position_id"], "")
        self.assertEqual(obs1["position"], "")

    def test_observations_without_leases_file_is_note_not_500(self):
        """★ U6-F5：只指向一个不存在的路径，note 由真实 `read_leases` 生成（不是桩的回显）。"""
        client = self._client()
        self._seed_template("T7")
        missing = os.path.join(tempfile.gettempdir(), "opstk-no-such-leases-%d" % os.getpid())
        orig = ztp_positions.leases_candidates
        ztp_positions.leases_candidates = lambda: [missing]
        try:
            r = client.get("/api/ct/ztp/observations")
        finally:
            ztp_positions.leases_candidates = orig
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["leases_path"], "")
        self.assertEqual(out["observations"], [])
        self.assertIn("读取不到 dnsmasq 租约文件", out["note"])
        self.assertIn(missing, out["note"])          # 真实实现会把尝试过的路径写出来
        self.assertIn("FileNotFoundError", out["note"])
        self.assertEqual(out["observations"], [])


class ZtpDeployBodyTest(_ApiTestBase):
    """U3 第二次审查的附加项：部署体不能覆盖模板里配好的 http_root。"""

    def _seed(self, name, **kw):
        # ZTP 开局必须填设备管理员口令（不代填默认口令）；库里存的是加密值。
        # 华为设备的 SNMP 团体名还要求 8-32 字符（真机实测），默认的 public 会被拒。
        from app.core import crypto
        kw.setdefault("admin_password_enc", crypto.encrypt("TestPw@123"))
        if kw.get("vendor", "huawei") == "huawei":
            kw.setdefault("snmp_community", "Opstk@2026")
        vendor = kw.pop("vendor", "huawei")

        async def _go():
            async with self.SessionLocal() as s:
                t = models.ZtpTemplate(name=name, vendor=vendor, **kw)
                s.add(t)
                await s.commit()
                await s.refresh(t)
                return t.id
        return asyncio.run(_go())

    def _deploy_and_capture(self, tid):
        from unittest import mock

        from app.api import ztp as ztp_api
        seen = {}

        def fake(files, _tid):
            seen.update(files)
            return {"ok": True, "supported": True, "log": [], "errors": [], "files_written": []}

        client = self._client()
        with mock.patch.object(ztp_api.ztp_server, "deploy_files", fake):
            r = client.post("/api/ct/ztp/templates/%s/deploy" % tid, json={})
        return r, seen

    def test_template_http_root_wins_over_derived_default(self):
        """★ 外部审查 U3-2nd-F9：改前 deploy 无条件把派生的
        `http://<server_ip>:8000/ztp` 塞进请求体，而 _gen_ztp_files 见到它就覆盖模板值 ——
        "部署出来的"与模板里配的（以及下载 ZIP 得到的）根本不是一份东西。
        """
        tid = self._seed("T20", http_root="http://10.0.0.250:8080/ztp")
        r, files = self._deploy_and_capture(tid)
        self.assertEqual(r.status_code, 200, r.text)
        blob = "\n".join(files.values())
        self.assertIn("http://10.0.0.250:8080/ztp", blob)      # 模板值必须生效
        self.assertNotIn(":8000/ztp", blob)                    # 派生默认值不得抢班

    def test_derived_default_still_used_when_template_is_empty(self):
        """模板没配 http_root 时才派生（既有行为不能丢）。"""
        tid = self._seed("T21", server_ip="10.9.9.9", http_root="")
        r, files = self._deploy_and_capture(tid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("http://10.9.9.9:8000/ztp", "\n".join(files.values()))

    def test_concurrent_duplicate_position_is_409_not_500(self):
        """★ 外部审查 U3-2nd-F14：应用层是先查再插，并发下会被数据库唯一约束拒 —— 必须是 409。

        模拟方式：把 `_dup_conflicts` 打成"什么都没查出来"（等价于另一个并发请求刚刚写进去、
        这边的检查发生在它之前），再提交同样的落位。
        """
        from unittest import mock

        from app.api import ztp as ztp_api
        client = self._client()
        tid = self._seed_template("T22")
        self._create(client, tid, position="A01-03-U99", mgmt_ip="10.0.0.99")
        with mock.patch.object(ztp_api, "_dup_conflicts", lambda *a, **k: (None, "")):
            r = self._create(client, tid, position="A01-03-U99", mgmt_ip="10.0.0.99")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("唯一约束", r.json()["detail"])
        # 409 之后会话必须还能用（回滚干净），否则后续请求会连不上
        r2 = client.get("/api/ct/ztp/positions", params={"template_id": tid})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(len(r2.json()), 1)


class ZtpClaimApiTest(_ApiTestBase):
    def test_claim_fills_mac_source_and_blocks_second_use(self):
        client = self._client()
        tid = self._seed_template("T4")
        id1 = self._create(client, tid, position="A01-03-U21", mgmt_ip="10.0.0.21").json()["id"]
        id2 = self._create(client, tid, position="A01-03-U22", mgmt_ip="10.0.0.22").json()["id"]
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": id1, "mac": "AA-BB-CC-DD-EE-66"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["position"]["mac"], "aa:bb:cc:dd:ee:66")  # 归一化回填
        self.assertEqual(body["position"]["source"], "claim")
        self.assertIsNotNone(body["position"]["claimed_at"])
        self.assertTrue(any("重新生成并部署" in x for x in body["next"]))
        # 同一 MAC 认领到同模板另一条落位 → 409
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": id2, "mac": "aa:bb:cc:dd:ee:66"})
        self.assertEqual(r.status_code, 409, r.text)
        # MAC 格式不正确 → 422；落位不存在 → 404
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": id2, "mac": "hello"})
        self.assertEqual(r.status_code, 422)
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": "nope", "mac": "aa:bb:cc:dd:ee:66"})
        self.assertEqual(r.status_code, 404)

    def test_claimed_mac_conflicts_with_new_position_too(self):
        client = self._client()
        tid = self._seed_template("T10")
        id1 = self._create(client, tid, position="A01-03-U71", mgmt_ip="10.0.0.71").json()["id"]
        client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": id1, "mac": "aa:bb:cc:dd:ee:77"})
        # 新建落位带同一 MAC（手工登记）→ 409
        r = self._create(client, tid, position="A01-03-U72", mgmt_ip="10.0.0.72",
                         mac="AA:BB:CC:DD:EE:77")
        self.assertEqual(r.status_code, 409, r.text)

    def test_same_position_reclaim_with_another_mac_is_refused(self):
        """★ 外部审查 U6-F7：同一落位**二次认领**换 MAC 不能静默换主。

        改前会直接改写 mac/claimed_at/source —— 先前那台设备已经按 MAC 拿到过自己的配置，
        之后会静默掉回 default.cfg，界面上毫无提示。界面本来就只让选"待认领"的落位
        （claimablePositions 过滤了 p.mac），所以这条路径只可能来自直接调 API / 并发操作。
        """
        client = self._client()
        tid = self._seed_template("T11")
        pid = self._create(client, tid, position="A01-03-U81", mgmt_ip="10.0.0.81").json()["id"]
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": pid, "mac": "aa:bb:cc:dd:ee:81"})
        self.assertEqual(r.status_code, 200, r.text)
        first = r.json()["position"]

        # 换个 MAC 再认领同一条落位 → 409，且**归属没有被改写**
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": pid, "mac": "aa:bb:cc:dd:ee:82"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("aa:bb:cc:dd:ee:82", r.json()["detail"])
        after = client.get("/api/ct/ztp/positions", params={"template_id": tid}).json()[0]
        self.assertEqual(after["mac"], "aa:bb:cc:dd:ee:81")
        self.assertEqual(after["claimed_at"], first["claimed_at"])

        # 同一个 MAC 重复认领 = 幂等（不是冲突）
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": pid, "mac": "AA:BB:CC:DD:EE:81"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_reclaim_after_clearing_mac_is_allowed(self):
        """改归属的**正确做法**：先清空 MAC（回到待认领）再认领 —— 必须走得通。"""
        client = self._client()
        tid = self._seed_template("T12")
        pid = self._create(client, tid, position="A01-03-U91", mgmt_ip="10.0.0.91").json()["id"]
        client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": pid, "mac": "aa:bb:cc:dd:ee:91"})
        r = client.put("/api/ct/ztp/positions/" + pid, json={
            "template_id": tid, "position": "A01-03-U91", "mgmt_ip": "10.0.0.91",
            "hostname": "sw91", "serial": "", "mac": "", "remark": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mac"], "")
        self.assertIsNone(r.json()["claimed_at"])
        r = client.post("/api/ct/ztp/claim", json={
            "template_id": tid, "position_id": pid, "mac": "aa:bb:cc:dd:ee:92"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["position"]["mac"], "aa:bb:cc:dd:ee:92")


class ZtpPositionsGeneratorTest(unittest.TestCase):
    """generate_all(positions=...)：落位设备排在 devices 之后，同名文件后者覆盖前者。"""

    PW = "TestPw@123"

    def _profile(self):
        return ZtpProfile(vendor="h3c", admin_password=self.PW, dhcp_iface="ens19",
                          server_ip="192.168.199.1", tftp_root="/srv/tftp",
                          http_root="http://192.168.199.1:8000/ztp",
                          dhcp_start="192.168.199.210", dhcp_end="192.168.199.240",
                          mgmt_gateway="192.168.199.1")

    def test_generate_all_with_positions(self):
        claimed = {"position": "A01-03-U12", "hostname": "", "mgmt_ip": "192.168.199.30",
                   "serial": "SN012", "mac": "AA:BB:CC:DD:EE:02"}
        pending = {"position": "A01-03-U13", "hostname": "sw13", "mgmt_ip": "192.168.199.31",
                   "serial": "", "mac": ""}
        files = generate_all(self._profile(), [], positions=[claimed, pending])
        conf = files["dnsmasq.conf"]
        # 有 mac 的落位：dhcp-host + bootfile 指向与配置文件一致的名字
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:02,set:pos_aabbccddee02", conf)
        self.assertIn('dhcp-option=tag:pos_aabbccddee02,option:bootfile-name,"ztp/SN012.cfg"', conf)
        # 无 mac 的落位：只有注释，整个文件里不该出现第二台设备的 dhcp-host=
        self.assertIn("A01-03-U13: 待认领", conf)
        hosts = [ln for ln in conf.splitlines() if ln.startswith("dhcp-host=")]
        self.assertEqual(hosts, ["dhcp-host=aa:bb:cc:dd:ee:02,set:pos_aabbccddee02"])
        # 两台落位各自的配置文件都在 files 里；主机名空 → 落位编码兜底；规划 IP 生效
        self.assertIn("ztp/SN012.cfg", files)
        self.assertIn("ztp/sw13.cfg", files)
        self.assertIn("sysname A01-03-U12", files["ztp/SN012.cfg"])
        self.assertIn("sysname sw13", files["ztp/sw13.cfg"])
        self.assertIn("ip address 192.168.199.30 255.255.255.0", files["ztp/SN012.cfg"])
        self.assertIn("ip address 192.168.199.31 255.255.255.0", files["ztp/sw13.cfg"])

    def test_positions_accepted_as_objects(self):
        """API 传的是 ORM 对象 —— 生成器必须同时吃 dict 和属性访问。"""

        class _Pos:
            position = "A01-03-U14"
            hostname = ""
            mgmt_ip = "192.168.199.40"
            serial = ""
            mac = "AA-BB-CC-DD-EE-14"

        files = generate_all(self._profile(), [], positions=[_Pos()])
        conf = files["dnsmasq.conf"]
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:14,set:pos_aabbccddee14", conf)
        self.assertIn('option:bootfile-name,"ztp/A01-03-U14.cfg"', conf)
        self.assertIn("ztp/A01-03-U14.cfg", files)

    def test_position_overrides_same_stem_device(self):
        """落位排在 devices 之后：同文件名（同 stem）时落位规划覆盖手工登记。

        ★ 外部审查 U6-F2：这条用例以前只看 cfg 正文、对 dnsmasq 侧一个字都不比 ——
        于是"覆盖之后那个 MAC 到底拿哪份配置"没有断言。现在把**映射**（该 MAC →
        `ztp/SW01.cfg`）和生成物里的**告警注释**一起钉死：覆盖是有意的兼容行为，
        但运维必须能从 dnsmasq.conf 里看出这台机器拿到的是落位的规划。
        """
        dev = ZtpDevice(hostname="SW01", mac="00:11:22:33:44:55", mgmt_ip="10.9.9.9")
        pos = {"position": "A01-03-U15", "hostname": "SW01", "mgmt_ip": "192.168.199.50",
               "serial": "", "mac": ""}
        files = generate_all(self._profile(), [dev], positions=[pos])
        cfg = files["ztp/SW01.cfg"]
        self.assertIn("ip address 192.168.199.50 255.255.255.0", cfg)
        self.assertNotIn("10.9.9.9", cfg)

        conf = files["dnsmasq.conf"]
        # 该 MAC 的 bootfile 目标就是那份被覆盖的文件（映射一致，不是"指向别处"）
        self.assertIn("dhcp-host=00:11:22:33:44:55,set:set_001122334455", conf)
        self.assertIn('dhcp-option=tag:set_001122334455,option:bootfile-name,"ztp/SW01.cfg"', conf)
        # 落位没有 MAC ⇒ 只出注释，不能再有第二条 dhcp-host 指向同一份文件
        hosts = [ln for ln in conf.splitlines() if ln.startswith("dhcp-host=")]
        self.assertEqual(hosts, ["dhcp-host=00:11:22:33:44:55,set:set_001122334455"])
        self.assertNotIn("set:pos_", conf)
        # 撞名必须在生成物里可见（否则运维核对时只会看到一条正常映射）
        warn_lines = [ln for ln in conf.splitlines() if ln.startswith("# ⚠ 上面这台")]
        self.assertEqual(len(warn_lines), 1, conf)
        self.assertIn("00:11:22:33:44:55", warn_lines[0])
        self.assertIn("ztp/SW01.cfg", warn_lines[0])

    def test_same_mac_in_devices_and_positions_is_rejected(self):
        """★ 外部审查 U3-2nd-F5：跨清单的 MAC 唯一性。

        设备清单里手抄了 MAC，之后又从租约把**同一台**设备认领到某个落位 —— 两条并存时
        dnsmasq 会为这个 MAC 生成两组指向**不同文件**的引导项，哪一份生效取决于合并语义，
        而接口/界面都报成功。必须点名两条来源并拒绝。
        """
        dev = ZtpDevice(hostname="SW9", mac="aa:bb:cc:dd:ee:99")
        pos = {"position": "A01-03-U99", "hostname": "SW99", "mgmt_ip": "192.168.199.60",
               "serial": "", "mac": "AA:BB:CC:DD:EE:99"}
        with self.assertRaises(ValueError) as ctx:
            generate_all(self._profile(), [dev], positions=[pos])
        msg = str(ctx.exception)
        self.assertIn("aa:bb:cc:dd:ee:99", msg)      # 报的是**归一后**的 MAC
        self.assertIn("SW9", msg)
        self.assertIn("A01-03-U99", msg)
        # 对照：MAC 不同就正常生成，且各自一条 dhcp-host
        pos2 = dict(pos, mac="AA:BB:CC:DD:EE:98")
        conf = generate_all(self._profile(), [dev], positions=[pos2])["dnsmasq.conf"]
        hosts = [ln for ln in conf.splitlines() if ln.startswith("dhcp-host=")]
        self.assertEqual(len(hosts), 2, conf)

    def test_mgmt_ip_inside_the_dhcp_pool_is_warned(self):
        """★ 外部审查 U6-F8：落位的规划管理 IP 落在**本模板 DHCP 池内**时必须告警。

        事故形状（与 netconfig 的池冲突同源）：设备首次上电先拿池里的临时地址，
        配置里的静态地址若也在池内，dnsmasq 可能把同一个地址再租给另一台 ⇒ 两台同 IP。
        只告警不拦（池常常是为首次引导临时开的），但必须能在生成物里看见。
        """
        p = self._profile()          # 池 192.168.199.210 - .240
        inside = {"position": "A01-03-U20", "hostname": "sw20", "mgmt_ip": "192.168.199.220",
                  "serial": "", "mac": "aa:bb:cc:dd:ee:20"}
        outside = {"position": "A01-03-U21", "hostname": "sw21", "mgmt_ip": "192.168.199.30",
                   "serial": "", "mac": "aa:bb:cc:dd:ee:21"}
        conf = generate_all(p, [], positions=[inside])["dnsmasq.conf"]
        warn = [ln for ln in conf.splitlines() if "落在本模板 DHCP 池" in ln]
        self.assertEqual(len(warn), 1, conf)
        self.assertIn("192.168.199.220", warn[0])
        self.assertIn("192.168.199.210-192.168.199.240", warn[0])
        # 池外的规划地址不得产生告警
        conf2 = generate_all(p, [], positions=[outside])["dnsmasq.conf"]
        self.assertNotIn("落在本模板 DHCP 池", conf2)
        # proxy/relay 模式没有地址池 ⇒ 不告警
        p2 = self._profile()
        p2.deploy_mode = "proxy"
        conf3 = generate_all(p2, [], positions=[inside])["dnsmasq.conf"]
        self.assertNotIn("落在本模板 DHCP 池", conf3)

    def test_readme_documents_position_flow_and_counts(self):
        claimed = {"position": "A01-03-U12", "hostname": "", "mgmt_ip": "192.168.199.30",
                   "serial": "SN012", "mac": "AA:BB:CC:DD:EE:02"}
        pending = {"position": "A01-03-U13", "hostname": "sw13", "mgmt_ip": "192.168.199.31",
                   "serial": "", "mac": ""}
        files = generate_all(self._profile(), [], positions=[claimed, pending])
        r = files["README.txt"]
        self.assertIn("落位登记 + 认领", r)
        self.assertIn("不需要手抄", r)
        self.assertIn("只会拿到 ztp/default.cfg", r)
        self.assertIn("落位: 2 个（其中已认领 1 个）", r)
        self.assertIn("本批次登记设备: 0 台", r)

    def test_generate_all_without_positions_keeps_old_behavior(self):
        p = self._profile()
        files = generate_all(p, [ZtpDevice(hostname="SW1", mac="00:11:22:33:44:55")])
        conf = files["dnsmasq.conf"]
        self.assertIn("dhcp-host=00:11:22:33:44:55", conf)
        self.assertNotIn("落位登记（认领后按 MAC 下发各自配置）", conf)
        self.assertIn("本批次登记设备: 1 台", files["README.txt"])
        self.assertIn("落位: 0 个（其中已认领 0 个）", files["README.txt"])


if __name__ == "__main__":
    unittest.main()
