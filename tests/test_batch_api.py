"""POST /api/regions/batch 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_batch_api.py
脚本会清空 regions/region_meta 后跑全套用例（含并发）。
"""
from __future__ import annotations

import os
import sys
import threading

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def batch(client, expected_revision, operations, trajectories):
    return client.post("/api/regions/batch", json={
        "expected_revision": expected_revision,
        "operations": operations,
        "trajectories": trajectories,
    })


SAFE_TRAJ = [
    {"x": -50, "y": 200, "t": "2026-09-30T00:00:00Z"},
    {"x": 900, "y": 200, "t": "2026-09-30T00:02:00Z"},
]
CROSS_TRAJ = [
    {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
    {"x": 250, "y": 50, "t": "2026-09-30T00:01:00Z"},
]
# 5 单位/秒：x=0@10s，x=100@30s，x=150@40s，x=180@46s

# 只擦过独立方区角点 (300,100) 的轨迹（相对 5s 时刻接触，方向从角点外侧过），
# 与 A区[0,100]、C区[400,500] 均无接触
TOUCH_TRAJ = [
    {"x": 250, "y": 50, "t": "2026-09-30T00:00:00Z"},
    {"x": 350, "y": 150, "t": "2026-09-30T00:00:10Z"},
]
TOUCH_BOX = [[300, 0], [400, 0], [400, 100], [300, 100]]

# 5 单位/秒，从 -50 到 450：x=0@10s、300@70s、400@90s、450@100s
CLIP_TRAJ = [
    {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
    {"x": 450, "y": 50, "t": "2026-09-30T00:01:40Z"},
]

db.init_pool()
client = TestClient(app)

with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE passes RESTART IDENTITY")
        cur.execute("TRUNCATE regions RESTART IDENTITY")
        cur.execute("TRUNCATE region_change_audit")
        cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
    conn.commit()

# ---- 批次登记两个区域 + 一条安全轨迹：整批成功，revision 只 +1 ----
r = batch(client, 0, [
    {"action": "create", "name": "A区",
     "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
     "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
    {"action": "create", "name": "B区",
     "polygon": [[200, 0], [300, 0], [300, 100], [200, 100]],
     "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("首批成功 200", r.status_code == 200, r.text)
b = r.json()
check("revision=1（整批只 +1）", b["revision"] == 1, str(b))
check("两个新区域 id 为 1、2，version=1",
      [(o["action"], o["region_id"], o["version"]) for o in b["operations"]]
      == [("create", 1, 1), ("create", 2, 1)], str(b["operations"]))
check("回传轨迹 safe=true 且逐区域列出",
      b["trajectories"][0]["safe"] is True
      and len(b["trajectories"][0]["results"]) == 2
      and all(x["crossings"] == [] for x in b["trajectories"][0]["results"]))

# ---- 不安全轨迹：整批拒绝并附穿越明细，不落库 ----
r = batch(client, 1, [
    {"action": "update", "id": 1, "name": "A区-改名"},
], [CROSS_TRAJ])
check("不安全 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
det = r.json()["error"]["details"]
check("明细定位轨迹下标 0", det[0]["trajectory_index"] == 0)
violated_regions = {x["region_id"] for x in det[0]["results"]}
check("明细列出穿越区域 A(1) 与 B(2)", violated_regions == {1, 2}, str(det))
a_cross = next(x for x in det[0]["results"] if x["region_id"] == 1)["crossings"]
check("A 区插值进入 00:00:10 / 离开 00:00:30（5 单位/秒）",
      a_cross[0]["entry_time"] == "2026-09-30T00:00:10Z"
      and a_cross[0]["exit_time"] == "2026-09-30T00:00:30Z", str(a_cross))
check("不安全不落库：revision 仍=1", client.get("/api/revision").json()["revision"] == 1)
check("不安全不落库：A 区名称未改、version 仍=1",
      client.get("/api/regions/1").json()["name"] == "A区"
      and client.get("/api/regions/1").json()["version"] == 1)

# ---- 不安全原因来自变更后集合：删除挡路区域后同一条轨迹即安全 ----
r = batch(client, 1, [
    {"action": "delete", "id": 1},
    {"action": "delete", "id": 2},
], [CROSS_TRAJ])
check("删除挡路区域后同轨迹安全 200", r.status_code == 200, r.text)
check("删除批次 revision=2", r.json()["revision"] == 2)
check("删除结果回显 version",
      [(o["action"], o["region_id"], o["version"]) for o in r.json()["operations"]]
      == [("delete", 1, 1), ("delete", 2, 1)])
check("两个区域确已删除",
      client.get("/api/regions/1").status_code == 404
      and client.get("/api/regions/2").status_code == 404)

# ---- 混合批次：修订 + 删除 + 新建一次提交 ----
# 先重建 A、B
r = batch(client, 2, [
    {"action": "create", "name": "A区",
     "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
     "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
    {"action": "create", "name": "B区",
     "polygon": [[200, 0], [300, 0], [300, 100], [200, 100]],
     "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("重建 A/B 成功 revision=3", r.status_code == 200 and r.json()["revision"] == 3, r.text)
id_a, id_b = 3, 4

r = batch(client, 3, [
    {"action": "update", "id": id_a, "name": "A区-改名"},
    {"action": "delete", "id": id_b},
    {"action": "create", "name": "C区",
     "polygon": [[400, 0], [500, 0], [500, 100], [400, 100]],
     "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("混合批次 200 revision=4", r.status_code == 200 and r.json()["revision"] == 4, r.text)
ops = {(o["action"], o["region_id"]): o["version"] for o in r.json()["operations"]}
check("修订 version=2、删除 version=1 回显、新建 id=5 version=1",
      ops == {("update", id_a): 2, ("delete", id_b): 1, ("create", 5): 1}, str(ops))
check("核验只针对变更后集合（B 已删，不出现在结果中）",
      {x["region_id"] for x in r.json()["trajectories"][0]["results"]} == {id_a, 5})
check("集合落库为 A、C 两个区域",
      {x["id"] for x in client.get("/api/regions").json()} == {id_a, 5})

# ---- revision 过期 ----
r = batch(client, 99, [
    {"action": "update", "id": id_a, "name": "x"},
], [SAFE_TRAJ])
check("revision 过期 409 revision_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)
check("409 携带当前 revision",
      r.json()["error"]["details"][0]["current_revision"] == 4)
check("409 不落库", client.get("/api/revision").json()["revision"] == 4)

# ---- 区域不存在 ----
r = batch(client, 4, [
    {"action": "update", "id": 9999, "name": "x"},
], [SAFE_TRAJ])
check("修订不存在 404 region_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "region_not_found", r.text)
d0 = r.json()["error"]["details"][0]
check("404 定位操作下标与区域 id",
      d0["operation_index"] == 0 and d0["region_id"] == 9999, str(d0))
r = batch(client, 4, [{"action": "delete", "id": 9998}], [SAFE_TRAJ])
check("删除不存在 404", r.status_code == 404
      and r.json()["error"]["code"] == "region_not_found", r.text)
check("404 不落库", client.get("/api/revision").json()["revision"] == 4)

# ---- 同一现有区域一批中出现多次 ----
r = batch(client, 4, [
    {"action": "update", "id": id_a, "name": "x"},
    {"action": "delete", "id": id_a},
], [SAFE_TRAJ])
check("同区域 update+delete 422 duplicate_region",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region", r.text)
check("重复明细给出两个操作下标",
      r.json()["error"]["details"][0]["operation_indices"] == [0, 1])
r = batch(client, 4, [
    {"action": "update", "id": id_a, "name": "x"},
    {"action": "update", "id": id_a, "name": "y"},
], [SAFE_TRAJ])
check("同区域两次 update 422",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region")
check("重复拒绝不落库", client.get("/api/revision").json()["revision"] == 4)

# ---- 非法操作数据 ----
r = batch(client, 4, [
    {"action": "update", "id": id_a,
     "valid_from": "2026-11-01T00:00:00Z",
     "valid_to": "2026-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("修订出非法时段 422 invalid_operation",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation", r.text)
check("非法操作定位下标 0",
      r.json()["error"]["details"][0]["operation_index"] == 0)
r = batch(client, 4, [
    {"action": "create", "name": "坏时段",
     "polygon": [[0, 0], [1, 0], [1, 1]],
     "valid_from": "2026-11-01T00:00:00Z",
     "valid_to": "2026-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("新建非法时段 422",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation", r.text)
check("非法操作不落库", client.get("/api/revision").json()["revision"] == 4)

# ---- 结构校验阶段：非法多边形 / 轨迹 / 缺字段 ----
r = batch(client, 4, [
    {"action": "create", "name": "坏多边形",
     "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
     "valid_from": "2026-09-01T00:00:00Z",
     "valid_to": "2026-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("自相交多边形 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

r = batch(client, 4, [{"action": "update", "id": id_a, "name": "x"}], [
    [{"x": 0, "y": 0, "t": "2026-09-30T00:00:10Z"},
     {"x": 1, "y": 1, "t": "2026-09-30T00:00:00Z"}],
])
check("逆序轨迹 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/regions/batch", json={
    "expected_revision": 4,
    "operations": [{"action": "update", "id": id_a, "name": "x"}],
    "trajectories": [[{"x": 0, "y": 0, "t": "2026-09-30T00:00:00"},
                      {"x": 1, "y": 1, "t": "2026-09-30T00:01:00Z"}]],
})
check("轨迹无时区 422", r.status_code == 422, r.text)

r = client.post("/api/regions/batch", json={
    "expected_revision": 4, "operations": [], "trajectories": [SAFE_TRAJ]})
check("空操作 422", r.status_code == 422)
r = client.post("/api/regions/batch", json={
    "expected_revision": 4,
    "operations": [{"action": "update", "id": id_a, "name": "x"}],
    "trajectories": []})
check("无轨迹 422", r.status_code == 422)
r = client.post("/api/regions/batch", json={
    "expected_revision": 4,
    "operations": [{"action": "frobnicate", "id": id_a}],
    "trajectories": [SAFE_TRAJ]})
check("未知 action 422", r.status_code == 422)
r = client.post("/api/regions/batch", json={
    "expected_revision": 4,
    "operations": [{"action": "update", "id": id_a, "bogus": 1}],
    "trajectories": [SAFE_TRAJ]})
check("多余字段 422", r.status_code == 422)
check("结构校验失败不落库", client.get("/api/revision").json()["revision"] == 4)

# ---- 边界接触即穿越 / 左闭右开规则在批量核验中同样生效 ----
# 当前集合：id=3 A区[0,100]（version=2）、id=5 C区[400,500]，revision=4。
# 以下用独立方区 [300,400]×[0,100] 与 CROSS_TRAJ/TOUCH_TRAJ 验证窗口端点。

# (a) 正长度穿越跨过 valid_to：违规，区间在 valid_to 截断，exit 不闭
r = batch(client, 4, [
    {"action": "create", "name": "右端截断区",
     "polygon": TOUCH_BOX,  # CLIP_TRAJ 于 70s 进入、90s 离开该方区
     "valid_from": "2026-09-29T00:00:00Z",
     "valid_to": "2026-09-30T00:01:23Z"},  # 穿越在 83s 处被右端截断
], [CLIP_TRAJ])
check("跨 valid_to 的正长度穿越仍违规",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
clip = next(x for x in r.json()["error"]["details"][0]["results"]
            if x["region_name"] == "右端截断区")["crossings"][0]
check("截断区间 entry=00:01:10 / exit=valid_to=00:01:23 且 exit_inclusive=false",
      clip["entry_time"] == "2026-09-30T00:01:10Z"
      and clip["exit_time"] == "2026-09-30T00:01:23Z"
      and clip["exit_inclusive"] is False, str(clip))
check("截断拒绝不落库 revision=4", client.get("/api/revision").json()["revision"] == 4)

# (b) 点接触恰落在 valid_to（右端开）：该接触不参与 => 安全，提交 revision=5
r = batch(client, 4, [
    {"action": "create", "name": "点接触-valid_to区",
     "polygon": TOUCH_BOX,
     "valid_from": "2026-09-29T00:00:00Z",
     "valid_to": "2026-09-30T00:00:05Z"},  # TOUCH_TRAJ 于 00:00:05 擦角 (300,100)
], [TOUCH_TRAJ])
check("点接触恰在 valid_to：安全并提交 revision=5",
      r.status_code == 200 and r.json()["revision"] == 5, r.text)
touch_id_vt = r.json()["operations"][0]["region_id"]
check("成功回传轨迹全部 safe（无穿越明细）",
      all(t["safe"] for t in r.json()["trajectories"]))

# (c) 点接触恰落在 valid_from（左端闭）：违规，point_contact 退化区间
r = batch(client, 5, [
    {"action": "create", "name": "点接触-valid_from区",
     "polygon": TOUCH_BOX,
     "valid_from": "2026-09-30T00:00:05Z",
     "valid_to": "2026-10-01T00:00:00Z"},
], [TOUCH_TRAJ])
check("点接触恰在 valid_from：违规 422",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
pc = next(x for x in r.json()["error"]["details"][0]["results"]
          if x["region_name"] == "点接触-valid_from区")["crossings"][0]
check("单点接触 point_contact=true 且起止相同=00:00:05Z",
      pc["point_contact"] is True and pc["entry_time"] == pc["exit_time"]
      == "2026-09-30T00:00:05Z", str(pc))
check("违规不落库 revision=5", client.get("/api/revision").json()["revision"] == 5)

# 清理 (b) 提交的区域（valid_to 点接触区），用安全轨迹提交，revision=6
r = batch(client, 5, [
    {"action": "delete", "id": touch_id_vt},
], [SAFE_TRAJ])
check("清理点接触区 revision=6", r.status_code == 200 and r.json()["revision"] == 6, r.text)

# ---- 多条轨迹：全部安全才提交；按轨迹下标分别给结论 ----
r = batch(client, 6, [
    {"action": "update", "id": id_a, "name": "A区-v3"},
], [
    SAFE_TRAJ,
    TOUCH_TRAJ,  # 对当前集合安全（[300,400] 方区已删）
])
check("两条均安全 200 revision=7", r.status_code == 200 and r.json()["revision"] == 7, r.text)
check("回传两条核验结果",
      [t["index"] for t in r.json()["trajectories"]] == [0, 1]
      and all(t["safe"] for t in r.json()["trajectories"]))

r = batch(client, 7, [
    {"action": "update", "id": id_a, "name": "A区-v4"},
], [SAFE_TRAJ, CROSS_TRAJ])
check("两条中第二条不安全仍整批拒绝",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
check("只列出不安全轨迹（下标 1）",
      [d["trajectory_index"] for d in r.json()["error"]["details"]] == [1])
check("不落库 revision=7 且名称未改",
      client.get("/api/revision").json()["revision"] == 7
      and client.get(f"/api/regions/{id_a}").json()["name"] == "A区-v3")

# ---- 并发：N 个同 revision 批次 + M 个单条写同时发起 ----
base_rev = client.get("/api/revision").json()["revision"]
N, M = 10, 3
outcomes: list[tuple] = []
barrier = threading.Barrier(N + M)
olock = threading.Lock()


def batch_worker(i):
    payload = {
        "expected_revision": base_rev,
        "operations": [{
            "action": "create", "name": f"并发批-{i}",
            "polygon": [[700 + i, 0], [705 + i, 0], [705 + i, 5], [700 + i, 5]],
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_to": "2026-10-01T00:00:00Z"}],
        "trajectories": [SAFE_TRAJ],
    }
    barrier.wait()
    rr = client.post("/api/regions/batch", json=payload)
    with olock:
        outcomes.append(("batch", i, rr.status_code,
                         rr.json().get("error", {}).get("code")))


def patch_worker(i):
    barrier.wait()
    rr = client.patch(f"/api/regions/{id_a}", json={"name": f"并发改名-{i}"})
    code = None if rr.status_code < 400 else rr.json().get("error", {}).get("code")
    with olock:
        outcomes.append(("patch", i, rr.status_code, code))


threads = [threading.Thread(target=batch_worker, args=(i,)) for i in range(N)]
threads += [threading.Thread(target=patch_worker, args=(i,)) for i in range(M)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)

batch_ok = [o for o in outcomes if o[0] == "batch" and o[2] == 200]
batch_conflict = [o for o in outcomes if o[0] == "batch" and o[3] == "revision_conflict"]
patch_ok = [o for o in outcomes if o[0] == "patch" and o[2] == 200]
errors5xx = [o for o in outcomes if 500 <= o[2] < 600]
check("成功并发批次数为 0 或 1（写事务串行化）", len(batch_ok) in (0, 1), str(outcomes))
check("其余并发批次全部 409 revision_conflict",
      len(batch_conflict) == N - len(batch_ok), str(outcomes))
check("单条 PATCH 不受 expected_revision 限制，全部成功", len(patch_ok) == M, str(outcomes))
check("无 5xx（无死锁/序列化异常泄漏）", not errors5xx, str(errors5xx))
final_rev = client.get("/api/revision").json()["revision"]
check(f"最终 revision = 基线 + {len(batch_ok)}(批) + {M}(单条) "
      f"= {base_rev + len(batch_ok) + M}",
      final_rev == base_rev + len(batch_ok) + M, f"got {final_rev}")
names = [r["name"] for r in client.get("/api/regions").json() if r["name"].startswith("并发批")]
check("落库并发批次数与成功批次数一致", len(names) == len(batch_ok), str(names))

# ---- 并发场景：A 区被单条 PATCH 推高 revision 后，同 revision 批次必须 409 ----
r1 = client.patch(f"/api/regions/{id_a}", json={"name": "先到先得"})
rev_after_patch = r1.json()["revision"]
r2 = batch(client, rev_after_patch - 1, [
    {"action": "delete", "id": id_a},
], [SAFE_TRAJ])
check("外部先提交后旧批次 409 且不删除区域",
      r2.status_code == 409
      and r2.json()["error"]["details"][0]["current_revision"] == rev_after_patch
      and client.get(f"/api/regions/{id_a}").status_code == 200, r2.text)

# 用新 revision 重放该批次即可成功
r3 = batch(client, rev_after_patch, [
    {"action": "delete", "id": id_a},
], [SAFE_TRAJ])
check("携带新 revision 重放成功", r3.status_code == 200, r3.text)

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部批量变更 API 集成用例通过")
