"""POST /api/regions/batch/preview 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_batch_preview_api.py
脚本会清空 regions/region_meta/passes/commitments/observations/
observation_events/idempotency_keys/region_change_audit 后跑全套用例，
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def preview(expected_revision, operations, trajectories):
    return client.post("/api/regions/batch/preview", json={
        "expected_revision": expected_revision,
        "operations": operations,
        "trajectories": trajectories,
    })


def batch(expected_revision, operations, trajectories):
    return client.post("/api/regions/batch", json={
        "expected_revision": expected_revision,
        "operations": operations,
        "trajectories": trajectories,
    })


def table_count(table: str) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return cur.fetchone()[0]


db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE observation_events, observations, idempotency_keys, "
                "commitments, passes, region_change_audit, regions RESTART IDENTITY"
            )
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


reset_all()

# 区域 A：[0,100]×[0,100]；区域 B：[200,300]×[0,100]；均在整个 2030 年生效
POLY_A = [[0, 0], [100, 0], [100, 100], [0, 100]]
POLY_B = [[200, 0], [300, 0], [300, 100], [200, 100]]
VF, VT = "2030-01-01T00:00:00Z", "2030-12-31T00:00:00Z"

# 拟登记区域 D：[0,100]×[120,180]，正好挡住 y=150 的通行路径
POLY_D = [[0, 120], [100, 120], [100, 180], [0, 180]]
# 拟登记区域 C：[400,500]×[0,100]
POLY_C = [[400, 0], [500, 0], [500, 100], [400, 100]]

# 各高度的水平直航轨迹（2030-06-01 10:00 出发，60 秒飞 200 单位）：
# 与 x∈[0,100] 区域的接触时刻为相对 [15s,45s] → 10:00:15 ~ 10:00:45
def traj_y(y, day="2030-06-01"):
    return [
        {"x": -50, "y": y, "t": f"{day}T10:00:00Z"},
        {"x": 150, "y": y, "t": f"{day}T10:01:00Z"},
    ]

SAFE_TRAJ = traj_y(500)     # 远离一切区域
CROSS_A_TRAJ = traj_y(50)   # 穿越区域 A
PASS_TRAJ = traj_y(150)     # 当前安全；拟登记 D 后不再安全
# 穿越拟登记区域 C 的轨迹（x 350→550）
CROSS_C_TRAJ = [
    {"x": 350, "y": 50, "t": "2030-06-01T10:00:00Z"},
    {"x": 550, "y": 50, "t": "2030-06-01T10:01:00Z"},
]

# ====================================================================
# 1. 准备：批量登记区域 A、B（revision 0→1）
# ====================================================================
r = batch(0, [
    {"action": "create", "name": "A区", "polygon": POLY_A,
     "valid_from": VF, "valid_to": VT},
    {"action": "create", "name": "B区", "polygon": POLY_B,
     "valid_from": VF, "valid_to": VT},
], [SAFE_TRAJ])
check("准备批次成功 200", r.status_code == 200, r.text)
ID_A = r.json()["operations"][0]["region_id"]
ID_B = r.json()["operations"][1]["region_id"]
REV = r.json()["revision"]
check("准备批次 revision=1", REV == 1, str(REV))

# ====================================================================
# 2. 基本预演：安全/穿越结论与穿越明细；拟登记区域以操作下标标识
# ====================================================================
t0 = datetime.now(timezone.utc)
r = preview(REV, [{"action": "update", "id": ID_A, "name": "A区-预演改名"}],
            [SAFE_TRAJ])
check("安全轨迹预演 200", r.status_code == 200, r.text)
body = r.json()
check("报告 revision 与预期一致", body["revision"] == REV, str(body))
check("报告无凭证/承诺冲突、无观察翻转",
      body["pass_conflicts"] == [] and body["commitment_conflicts"] == []
      and body["observation_flips"] == [], str(body))
check("安全轨迹结论 safe=true 且逐区域无穿越",
      body["trajectories"][0]["safe"] is True
      and all(x["crossings"] == [] for x in body["trajectories"][0]["results"]),
      str(body["trajectories"]))
check("结果覆盖变更后全部区域（A、B）",
      {x["region_id"] for x in body["trajectories"][0]["results"]} == {ID_A, ID_B})
check("现有区域 operation_index 为 null",
      all(x["operation_index"] is None for x in body["trajectories"][0]["results"]))
evaluated_at = datetime.fromisoformat(body["evaluated_at"].replace("Z", "+00:00"))
check("evaluated_at 为本次预演基准时刻",
      abs((evaluated_at - t0).total_seconds()) < 120, body["evaluated_at"])

r = preview(REV, [{"action": "update", "id": ID_A, "name": "A区-预演改名"}],
            [CROSS_A_TRAJ])
body = r.json()
check("穿越轨迹预演 200（不作为错误）", r.status_code == 200, r.text)
check("穿越轨迹结论 safe=false", body["trajectories"][0]["safe"] is False)
hit = next(x for x in body["trajectories"][0]["results"] if x["region_id"] == ID_A)
check("A 区穿越明细 10:00:15~10:00:45（插值、双闭）",
      len(hit["crossings"]) == 1
      and hit["crossings"][0]["entry_time"] == "2030-06-01T10:00:15Z"
      and hit["crossings"][0]["exit_time"] == "2030-06-01T10:00:45Z"
      and hit["crossings"][0]["entry_inclusive"] is True
      and hit["crossings"][0]["exit_inclusive"] is True
      and hit["crossings"][0]["point_contact"] is False, str(hit))

# 拟登记区域以操作下标标识（create 在下标 1）；删除的区域不出现
r = preview(REV, [
    {"action": "update", "id": ID_B, "name": "B区-预演改名"},
    {"action": "create", "name": "C区", "polygon": POLY_C,
     "valid_from": VF, "valid_to": VT},
    {"action": "delete", "id": ID_A},
], [CROSS_C_TRAJ])
body = r.json()
check("含 create/delete 的预演 200", r.status_code == 200, r.text)
res = body["trajectories"][0]["results"]
check("被删区域不出现在结果中", all(x["region_id"] != ID_A for x in res), str(res))
check("拟登记区域 region_id 为 null、以操作下标 1 标识",
      any(x["region_id"] is None and x["operation_index"] == 1
          and x["region_name"] == "C区" for x in res), str(res))
c_hit = next(x for x in res if x["operation_index"] == 1)
check("拟登记区域穿越明细 10:00:15~10:00:45",
      len(c_hit["crossings"]) == 1
      and c_hit["crossings"][0]["entry_time"] == "2030-06-01T10:00:15Z"
      and c_hit["crossings"][0]["exit_time"] == "2030-06-01T10:00:45Z", str(c_hit))
check("修订区域仍以正式编号标识",
      any(x["region_id"] == ID_B and x["operation_index"] is None for x in res),
      str(res))

# 预演无副作用：revision、区域、审计均不变
check("预演后 revision 仍为 1",
      client.get("/api/revision").json()["revision"] == 1)
region_a = client.get(f"/api/regions/{ID_A}").json()
check("预演后 A 区原名未变、version 未变",
      region_a["name"] == "A区" and region_a["version"] == 1, str(region_a))
check("预演后仍只有 2 个区域",
      len(client.get("/api/regions").json()) == 2)
check("预演不产生审计记录", table_count("region_change_audit") == 1)

# ====================================================================
# 3. 签发约束实体：凭证、两张承诺、四张观察（其中一张随后取消）
# ====================================================================
r = client.post("/api/passes", json={"points": PASS_TRAJ})
check("签发凭证 201", r.status_code == 201, r.text)
PASS_ID = r.json()["id"]

r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 150, "t": 0}, {"x": 150, "y": 150, "t": 60}],
    "window_start": "2030-06-02T10:00:00Z",
    "window_end": "2030-06-02T11:00:00Z",
})
check("签发承诺 1（y=150 路径）201", r.status_code == 201, r.text)
COM1 = r.json()["id"]
r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 250, "t": 0}, {"x": 150, "y": 250, "t": 60}],
    "window_start": "2030-06-02T10:00:00Z",
    "window_end": "2030-06-02T11:00:00Z",
})
check("签发承诺 2（y=250 路径）201", r.status_code == 201, r.text)
COM2 = r.json()["id"]

r = client.post("/api/observations", json={"points": traj_y(150, "2030-06-03")})
check("登记观察 1（当前安全）201", r.status_code == 201, r.text)
OBS1 = r.json()["id"]
check("观察 1 结论 safe", r.json()["conclusion"] == "safe")
r = client.post("/api/observations", json={"points": traj_y(50, "2030-06-03")})
check("登记观察 2（当前不安全）201", r.status_code == 201, r.text)
OBS2 = r.json()["id"]
check("观察 2 结论 unsafe", r.json()["conclusion"] == "unsafe")
r = client.post("/api/observations", json={"points": traj_y(500, "2030-06-03")})
check("登记观察 3（始终安全）201", r.status_code == 201, r.text)
OBS3 = r.json()["id"]
r = client.post("/api/observations", json={"points": traj_y(150, "2030-06-04")})
check("登记观察 4（随后取消）201", r.status_code == 201, r.text)
OBS4 = r.json()["id"]
r = client.post(f"/api/observations/{OBS4}/cancel")
check("取消观察 4", r.status_code == 200 and r.json()["status"] == "cancelled")

# 签发/取消不推进 revision
check("签发约束实体后 revision 仍为 1",
      client.get("/api/revision").json()["revision"] == 1)

# ====================================================================
# 4. 影响预演：凭证冲突、承诺被清空、观察结论净翻转
#    操作：create D（挡住 y=150 路径）+ delete A
# ====================================================================
r = preview(REV, [
    {"action": "create", "name": "D区", "polygon": POLY_D,
     "valid_from": VF, "valid_to": VT},
    {"action": "delete", "id": ID_A},
], [SAFE_TRAJ, PASS_TRAJ])
check("影响预演 200", r.status_code == 200, r.text)
body = r.json()
check("报告 revision=1", body["revision"] == 1)
check("安全轨迹仍 safe=true", body["trajectories"][0]["safe"] is True)
check("y=150 轨迹在拟变更集合下 safe=false",
      body["trajectories"][1]["safe"] is False)
d_hit = next(x for x in body["trajectories"][1]["results"]
             if x["operation_index"] == 0)
check("y=150 轨迹穿越拟登记 D（下标 0）10:00:15~10:00:45",
      d_hit["region_id"] is None and len(d_hit["crossings"]) == 1
      and d_hit["crossings"][0]["entry_time"] == "2030-06-01T10:00:15Z"
      and d_hit["crossings"][0]["exit_time"] == "2030-06-01T10:00:45Z", str(d_hit))
check("将冲突的凭证为凭证 1",
      body["pass_conflicts"] == [{"pass_id": PASS_ID}], str(body["pass_conflicts"]))
check("将被清空的承诺为承诺 1（承诺 2 路径不受影响）",
      body["commitment_conflicts"] == [{"commitment_id": COM1}],
      str(body["commitment_conflicts"]))
flips = {f["observation_id"]: f for f in body["observation_flips"]}
check("观察净翻转恰为观察 1、2",
      set(flips) == {OBS1, OBS2}, str(body["observation_flips"]))
check("观察 1 翻转 safe→unsafe",
      flips.get(OBS1, {}).get("old_conclusion") == "safe"
      and flips.get(OBS1, {}).get("new_conclusion") == "unsafe", str(flips))
check("观察 2 翻转 unsafe→safe（A 被删）",
      flips.get(OBS2, {}).get("old_conclusion") == "unsafe"
      and flips.get(OBS2, {}).get("new_conclusion") == "safe", str(flips))

# 预演无副作用：一切数据与版本不变
check("影响预演后 revision 仍为 1",
      client.get("/api/revision").json()["revision"] == 1)
check("影响预演后 A 区仍在且未变",
      client.get(f"/api/regions/{ID_A}").json()["name"] == "A区")
check("影响预演后审计仍 1 条", table_count("region_change_audit") == 1)
obs1 = client.get(f"/api/observations/{OBS1}").json()
obs2 = client.get(f"/api/observations/{OBS2}").json()
check("观察结论未被预演改写",
      obs1["conclusion"] == "safe" and obs1["revision"] == 1
      and obs2["conclusion"] == "unsafe" and obs2["revision"] == 1,
      f"{obs1['conclusion']}@{obs1['revision']} / {obs2['conclusion']}@{obs2['revision']}")
check("预演不产生观察事件", table_count("observation_events") == 0)
check("凭证仍 active", client.get(f"/api/passes/{PASS_ID}").json()["status"] == "active")
com1 = client.get(f"/api/commitments/{COM1}").json()
check("承诺仍 active 且区间未变",
      com1["status"] == "active" and len(com1["intervals"]) == 1, str(com1))

# ====================================================================
# 5. 错误约定与正式批量一致
# ====================================================================
r = preview(0, [{"action": "update", "id": ID_A, "name": "x"}], [SAFE_TRAJ])
check("过期 revision 409 revision_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)
check("409 details 带当前 revision",
      r.json()["error"]["details"][0]["current_revision"] == REV)

r = preview(REV, [
    {"action": "update", "id": ID_A, "name": "x"},
    {"action": "delete", "id": ID_A},
], [SAFE_TRAJ])
check("同一区域重复操作 422 duplicate_region",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region", r.text)
check("重复明细给出两个操作下标",
      r.json()["error"]["details"][0]["operation_indices"] == [0, 1])

r = preview(REV, [{"action": "delete", "id": 9999}], [SAFE_TRAJ])
check("未知区域 404 region_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "region_not_found", r.text)
d0 = r.json()["error"]["details"][0]
check("404 定位操作下标与区域 id",
      d0["operation_index"] == 0 and d0["region_id"] == 9999, str(d0))

r = preview(REV, [
    {"action": "update", "id": ID_A,
     "valid_from": "2030-11-01T00:00:00Z", "valid_to": "2030-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("修订出非法时段 422 invalid_operation",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation", r.text)
check("非法操作定位下标 0",
      r.json()["error"]["details"][0]["operation_index"] == 0)
r = preview(REV, [
    {"action": "create", "name": "坏时段", "polygon": POLY_C,
     "valid_from": "2030-11-01T00:00:00Z", "valid_to": "2030-10-01T00:00:00Z"},
], [SAFE_TRAJ])
check("新建非法时段 422 invalid_operation",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation", r.text)

r = preview(REV, [{"action": "update", "id": ID_A, "name": "x"}], [
    [{"x": 0, "y": 0, "t": "2030-06-01T10:00:10Z"},
     {"x": 1, "y": 1, "t": "2030-06-01T10:00:00Z"}],
])
check("逆序轨迹 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = preview(999, [{"action": "update", "id": ID_A, "name": "x"}], [
    [{"x": 0, "y": 0, "t": "2030-06-01T10:00:10Z"},
     {"x": 1, "y": 1, "t": "2030-06-01T10:00:00Z"}],
])
check("轨迹非法优先于 revision 检查（与正式批量一致）",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = preview(REV, [
    {"action": "create", "name": "坏多边形",
     "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
     "valid_from": VF, "valid_to": VT},
], [SAFE_TRAJ])
check("自相交多边形 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)
r = client.post("/api/regions/batch/preview", json={
    "expected_revision": REV, "operations": [], "trajectories": [SAFE_TRAJ]})
check("空操作 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)
check("全部错误预演后 revision 仍为 1",
      client.get("/api/revision").json()["revision"] == 1)

# ====================================================================
# 6. 预演不占用区域编号：随后真实登记拿到连续 id
# ====================================================================
r = client.post("/api/regions", json={
    "name": "E区", "polygon": [[1000, 1000], [1100, 1000], [1100, 1100], [1000, 1100]],
    "valid_from": VF, "valid_to": VT})
check("真实登记 E 区 201", r.status_code == 201, r.text)
check("E 区 id 连续（预演的 create 未占用编号）",
      r.json()["region"]["id"] == 3, str(r.json()["region"]["id"]))
check("真实登记后 revision=2", r.json()["revision"] == 2)
REV = 2

r = preview(1, [{"action": "update", "id": ID_A, "name": "x"}], [SAFE_TRAJ])
check("revision 推进后旧预期 409",
      r.status_code == 409
      and r.json()["error"]["details"][0]["current_revision"] == 2, r.text)

# ====================================================================
# 7. 预演结论与正式提交的保护规则一致（快照未变时）
# ====================================================================
BLOCKING_OPS = [
    {"action": "create", "name": "D区", "polygon": POLY_D,
     "valid_from": VF, "valid_to": VT},
]
r = preview(REV, BLOCKING_OPS, [SAFE_TRAJ])
body = r.json()
check("预演：登记 D 将冲突凭证 1、清空承诺 1",
      body["pass_conflicts"] == [{"pass_id": PASS_ID}]
      and body["commitment_conflicts"] == [{"commitment_id": COM1}], str(body))
r = batch(REV, BLOCKING_OPS, [SAFE_TRAJ])
check("正式提交同一批 → 409 pass_conflict（与预演一致）",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict"
      and r.json()["error"]["details"][0]["pass_id"] == PASS_ID, r.text)

r = client.post(f"/api/passes/{PASS_ID}/cancel")
check("取消凭证 1", r.status_code == 200)
r = preview(REV, BLOCKING_OPS, [SAFE_TRAJ])
body = r.json()
check("取消凭证后预演：不再报凭证冲突，仍报承诺被清空",
      body["pass_conflicts"] == []
      and body["commitment_conflicts"] == [{"commitment_id": COM1}], str(body))
r = batch(REV, BLOCKING_OPS, [SAFE_TRAJ])
check("正式提交同一批 → 409 commitment_conflict（与预演一致）",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict"
      and r.json()["error"]["details"][0]["commitment_id"] == COM1, r.text)
check("冲突提交未落库：revision 仍为 2",
      client.get("/api/revision").json()["revision"] == 2)
check("冲突提交不产生观察事件", table_count("observation_events") == 0)

r = preview(REV, [{"action": "update", "id": ID_B, "name": "B区-正式改名"}],
            [SAFE_TRAJ])
body = r.json()
check("无害批次预演：全安全、无冲突、无翻转",
      body["trajectories"][0]["safe"] is True
      and body["pass_conflicts"] == [] and body["commitment_conflicts"] == []
      and body["observation_flips"] == [], str(body))
r = batch(REV, [{"action": "update", "id": ID_B, "name": "B区-正式改名"}],
          [SAFE_TRAJ])
check("无害批次正式提交成功，revision=3",
      r.status_code == 200 and r.json()["revision"] == 3, r.text)

# ====================================================================
# 8. 终态：预演全程无副作用
# ====================================================================
check("审计记录恰 3 条（准备批次/E 区登记/正式改名批次）",
      table_count("region_change_audit") == 3)
check("观察事件始终为 0（预演与失败提交均不产生）",
      table_count("observation_events") == 0)
obs1 = client.get(f"/api/observations/{OBS1}").json()
obs2 = client.get(f"/api/observations/{OBS2}").json()
check("观察结论保持登记时状态",
      obs1["conclusion"] == "safe" and obs2["conclusion"] == "unsafe")
check("承诺 1 仍 active 且区间完整",
      len(client.get(f"/api/commitments/{COM1}").json()["intervals"]) == 1)
check("幂等键表无占用", table_count("idempotency_keys") == 0)

reset_all()
print()
if failures:
    print(f"失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("全部批量变更影响预演 API 集成用例通过")
