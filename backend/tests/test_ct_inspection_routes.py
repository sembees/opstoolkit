# -*- coding: utf-8 -*-
"""钉住 CT 巡检的**路由归属**（Help 逐条审计发现的错接 bug）。

### 为什么单独写这一组

原实现里 `@router.delete('/templates/{tid}')` 被错接在 `replay_task` 上面（两个装饰器叠在
同一个函数上），后果是：

  · `delete_template` **没有任何路由**、永远不可达（死代码）；
  · 而前端 `Inspection.vue:383` 的"删除模板"实际调到 `replay_task` —— 拿模板 id 当任务 id 查，
    于是"删除用户模板"在界面上永远失败。Help 里"用户模板可增删改"的说法与实现矛盾的根因就在这里。

★ 这类 bug **常规行为用例抓不到**：路由确实存在、请求也能打通，只是**接到了错的函数上**。
  所以这里直接对着 FastAPI 的路由表断言"哪个路径归哪个函数"，并加一条反查。
"""
import unittest

from app.api import inspection


def _route_owners():
    """{ (METHOD, path): 处理函数名 }。"""
    out = {}
    for r in inspection.router.routes:
        for m in getattr(r, "methods", None) or []:
            out[(m, r.path)] = r.endpoint.__name__
    return out


class InspectionRouteOwnershipTest(unittest.TestCase):
    def test_delete_template_route_points_at_delete_template(self):
        self.assertEqual(_route_owners().get(("DELETE", "/templates/{tid}")), "delete_template")

    def test_replay_route_still_points_at_replay_task(self):
        self.assertEqual(_route_owners().get(("GET", "/tasks/{task_id}/replay")), "replay_task")

    def test_templates_paths_are_not_served_by_the_replay_handler(self):
        """反查：回放函数不许出现在任何 /templates 路径上；删除函数必须出现在 /templates 上。"""
        owners = _route_owners()
        on_templates = [fn for (m, p), fn in owners.items() if "/templates" in p]
        self.assertNotIn("replay_task", on_templates)
        self.assertIn("delete_template", on_templates)

    def test_every_mutating_route_has_a_distinct_handler(self):
        """通用护栏：同一个处理函数**不应该**同时挂在两条不同路径上（这次的 bug 就是这种形状）。"""
        seen = {}
        dupes = []
        for (m, p), fn in _route_owners().items():
            if m in ("POST", "PUT", "DELETE", "PATCH"):
                if fn in seen and seen[fn] != p:
                    dupes.append("%s 同时挂在 %s 与 %s" % (fn, seen[fn], p))
                seen[fn] = p
        self.assertEqual(dupes, [], "有处理函数被复用到多条路径（很可能是装饰器叠错了）")


if __name__ == "__main__":
    unittest.main()
