"""通行凭证 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_passes_api.py
脚本会清空 regions/region_meta/passes 后跑全套用例（含并发），
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def future(seconds: float) -> str:
    return iso_z(datetime.now(timezone.utc) + timedelta(seconds=seconds))


# 宽生效窗口，覆盖所有动态生成的未来轨迹时刻
WIDE = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE passes RESTART IDENTITY")
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("TRUNCATE region_change_audit")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def pass_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM passes")
            return cur.fetchone()[0]


reset_all()

# ---- 登记区域 A：[0,100]x[0,100] ----
r = client.post("/api/regions", json={
    "name": "A区", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记 A 区 201 revision=1", r.status_code == 201 and r.json()["revision"] == 1, r.text)
id_a = r.json()["region"]["id"]

# ---- 签发安全凭证（轨迹 y=200 从区域上方经过） ----
SAFE_TRAJ = [
    {"x": -50, "y": 200, "t": future(60)},
    {"x": 350, "y": 200, "t": future(120)},
]
r = client.post("/api/passes", json={"points": SAFE_TRAJ})
check("签发安全凭证 201", r.status_code == 201, r.text)
p1 = r.json()
check("返回凭证 id / status=active / revision=1",
      p1["id"] == 1 and p1["status"] == "active" and p1["revision"] == 1, str(p1))
check("回显轨迹（UTC Z 时刻）",
      len(p1["trajectory"]) == 2
      and p1["trajectory"][0]["t"].endswith("Z")
      and p1["trajectory"][0]["x"] == -50, str(p1))
check("issued_at 存在且 cancelled_at 为空",
      bool(p1["issued_at"]) and p1["cancelled_at"] is None, str(p1))
check("签发不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 1)
p1_id = p1["id"]

# ---- 不安全轨迹：422 unsafe_trajectory，附穿越明细，不落库 ----
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 50, "t": future(60)},
    {"x": 350, "y": 50, "t": future(120)},
]})
check("不安全轨迹 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
det = r.json()["error"]["details"]
check("明细列出穿越区域 A 及插值起止时刻",
      len(det) == 1 and det[0]["region_id"] == id_a
      and len(det[0]["crossings"]) == 1
      and det[0]["crossings"][0]["entry_time"].endswith("Z"), str(det))
check("不安全不落库（passes 仍 1 张）", pass_count() == 1)

# ---- 已开始的轨迹：422 invalid_trajectory，不落库 ----
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": future(-10)},
    {"x": 350, "y": 200, "t": future(60)},
]})
check("首点早于签发时刻 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
check("已开始不落库", pass_count() == 1)

# ---- 非法轨迹结构 ----
r = client.post("/api/passes", json={"points": [
    {"x": 0, "y": 0, "t": future(60)},
]})
check("轨迹点不足 422", r.status_code == 422, r.text)
same_t = future(60)
r = client.post("/api/passes", json={"points": [
    {"x": 0, "y": 0, "t": same_t},
    {"x": 1, "y": 1, "t": same_t},
]})
check("时刻不严格递增 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/passes", json={"points": [
    {"x": 0, "y": 0, "t": "2026-10-01T00:00:00"},
    {"x": 1, "y": 1, "t": "2026-10-01T00:01:00"},
]})
check("时刻不带时区 422", r.status_code == 422, r.text)
r = client.post("/api/passes", json={"points": SAFE_TRAJ, "extra": 1})
check("多余字段 422", r.status_code == 422, r.text)
check("非法输入均不落库", pass_count() == 1)

# ---- 查询 ----
r = client.get(f"/api/passes/{p1_id}")
check("查询凭证 200 状态 active",
      r.status_code == 200 and r.json()["status"] == "active", r.text)
r = client.get("/api/passes/9999")
check("未知凭证 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# ---- 未取消凭证约束区域变更 ----
# (a) 单区登记压住凭证轨迹 → 409 pass_conflict
rev_before = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions", json={
    "name": "B区-挡路", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记压住凭证轨迹 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
check("409 明细给出冲突凭证 id",
      [d["pass_id"] for d in r.json()["error"]["details"]] == [p1_id],
      str(r.json()["error"]["details"]))
check("409 后 revision 不变且区域未落库",
      client.get("/api/revision").json()["revision"] == rev_before
      and len(client.get("/api/regions").json()) == 1)

# (b) 修订 A 使其覆盖凭证轨迹 → 409，version/revision 不变
r = client.patch(f"/api/regions/{id_a}", json={
    "polygon": [[0, 0], [200, 0], [200, 300], [0, 300]],
})
check("修订覆盖凭证轨迹 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
ra = client.get(f"/api/regions/{id_a}").json()
check("409 后 A 的 version/revision 不变",
      ra["version"] == 1
      and client.get("/api/revision").json()["revision"] == rev_before, str(ra))

# (c) 无害修订（仅改名）不受限 → 200
r = client.patch(f"/api/regions/{id_a}", json={"name": "A区-改名"})
check("无害修订 200 revision 推进", r.status_code == 200
      and r.json()["revision"] == rev_before + 1, r.text)

# (d) 删除区域（集合变小，不可能危及凭证）→ 200
r = client.post("/api/regions", json={
    "name": "临时区", "polygon": [[500, 500], [510, 500], [510, 510], [500, 510]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
tmp_id = r.json()["region"]["id"]
r = client.delete(f"/api/regions/{tmp_id}")
check("有活跃凭证时删除区域 200", r.status_code == 200, r.text)

# (e) 批量变更压住凭证轨迹 → 409 pass_conflict，整批回滚
rev_before = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_before,
    "operations": [
        {"action": "create", "name": "B区-挡路",
         "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 500, "t": future(60)},
        {"x": 350, "y": 500, "t": future(120)},
    ]],
})
check("批量变更压住凭证轨迹 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
check("批量 409 明细给出冲突凭证 id",
      [d["pass_id"] for d in r.json()["error"]["details"]] == [p1_id])
check("批量 409 整批回滚（revision 不变、区域未落库）",
      client.get("/api/revision").json()["revision"] == rev_before
      and all(x["name"] != "B区-挡路" for x in client.get("/api/regions").json()))

# (f) 批量既有校验保留：revision 过期仍 409 revision_conflict
r = client.post("/api/regions/batch", json={
    "expected_revision": 99,
    "operations": [{"action": "update", "id": id_a, "name": "x"}],
    "trajectories": [[
        {"x": -50, "y": 500, "t": future(60)},
        {"x": 350, "y": 500, "t": future(120)},
    ]],
})
check("批量 revision 冲突校验保留",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)

# ---- 取消 ----
rev_before = client.get("/api/revision").json()["revision"]
r = client.post(f"/api/passes/{p1_id}/cancel")
check("取消凭证 200 状态 cancelled",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["cancelled_at"] is not None, r.text)
cancel_body = r.json()
check("取消不推进区域 revision",
      client.get("/api/revision").json()["revision"] == rev_before)
r = client.post(f"/api/passes/{p1_id}/cancel")
check("重复取消幂等（结果不变）",
      r.status_code == 200 and r.json() == cancel_body, r.text)
r = client.post("/api/passes/9999/cancel")
check("取消未知凭证 404", r.status_code == 404
      and r.json()["error"]["code"] == "not_found", r.text)

# 取消后原冲突变更放行
r = client.post("/api/regions", json={
    "name": "B区-挡路", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("取消后登记原冲突区域 201", r.status_code == 201, r.text)

# ---- 末点时刻已过的凭证不再约束 ----
p2_start = datetime.now(timezone.utc) + timedelta(seconds=2)
p2_end = datetime.now(timezone.utc) + timedelta(seconds=3.5)
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 400, "t": iso_z(p2_start)},
    {"x": 350, "y": 400, "t": iso_z(p2_end)},
]})
check("签发短寿命凭证 201", r.status_code == 201, r.text)
p2_id = r.json()["id"]
r = client.post("/api/regions", json={
    "name": "C区-挡路", "polygon": [[0, 350], [100, 350], [100, 450], [0, 450]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("未过期凭证仍约束 409", r.status_code == 409
      and r.json()["error"]["code"] == "pass_conflict", r.text)
# 等到凭证末点时刻过去
remaining = (p2_end - datetime.now(timezone.utc)).total_seconds()
time.sleep(max(0.0, remaining) + 0.5)
r = client.post("/api/regions", json={
    "name": "C区-挡路", "polygon": [[0, 350], [100, 350], [100, 450], [0, 450]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("末点时刻已过则不再约束 201", r.status_code == 201, r.text)

# ---- 并发：签发与建区串行化，恰好一个成功 ----
barrier = threading.Barrier(2)
outcome: dict = {}


def issue_worker():
    barrier.wait()
    rr = client.post("/api/passes", json={"points": [
        {"x": -50, "y": 600, "t": future(60)},
        {"x": 350, "y": 600, "t": future(120)},
    ]})
    outcome["issue"] = (rr.status_code, rr.json())


def create_worker():
    barrier.wait()
    rr = client.post("/api/regions", json={
        "name": "D区-并发",
        "polygon": [[-100, 550], [400, 550], [400, 650], [-100, 650]],
        "valid_from": WIDE[0], "valid_to": WIDE[1],
    })
    outcome["create"] = (rr.status_code, rr.json())


t1 = threading.Thread(target=issue_worker)
t2 = threading.Thread(target=create_worker)
t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
issue_ok = outcome["issue"][0] == 201
create_ok = outcome["create"][0] == 201
check("签发与建区并发：恰好一个成功（无漏检）",
      issue_ok != create_ok, str(outcome)[:300])
if issue_ok:
    check("签发先成功则建区 409 pass_conflict",
          outcome["create"][0] == 409
          and outcome["create"][1]["error"]["code"] == "pass_conflict",
          str(outcome["create"])[:300])
else:
    check("建区先成功则签发 422 unsafe_trajectory",
          outcome["issue"][0] == 422
          and outcome["issue"][1]["error"]["code"] == "unsafe_trajectory",
          str(outcome["issue"])[:300])

# ---- 并发混合：签发/取消/建区/修订同时发起，全部串行化，无 5xx ----
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 760, "t": future(120)},
    {"x": 350, "y": 760, "t": future(180)},
]})
p_cancel_id = r.json()["id"]
base_rev = client.get("/api/revision").json()["revision"]
K = 8
barrier2 = threading.Barrier(K)
results: list[tuple] = []
rlock = threading.Lock()


def mixed_worker(i):
    barrier2.wait()
    if i < 2:  # 两个线程同时取消同一张凭证：幂等
        rr = client.post(f"/api/passes/{p_cancel_id}/cancel")
    elif i < 5:  # 三条互不相同的未来安全轨迹
        y = 700 + 20 * i
        rr = client.post("/api/passes", json={"points": [
            {"x": -50, "y": y, "t": future(120)},
            {"x": 350, "y": y, "t": future(180)},
        ]})
    elif i < 7:  # 两个不压住任何轨迹的新区域
        y = 900 + 50 * i
        rr = client.post("/api/regions", json={
            "name": f"并发区-{i}",
            "polygon": [[0, y], [100, y], [100, y + 30], [0, y + 30]],
            "valid_from": WIDE[0], "valid_to": WIDE[1],
        })
    else:  # 一个无害修订
        rr = client.patch(f"/api/regions/{id_a}", json={"name": "A区-并发改名"})
    with rlock:
        results.append((i, rr.status_code))


threads = [threading.Thread(target=mixed_worker, args=(i,)) for i in range(K)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)

check("并发混合无 5xx（无死锁/序列化异常泄漏）",
      all(s < 500 for _, s in results) and len(results) == K, str(results))
check("并发取消幂等：两个取消都 200",
      all(s == 200 for i, s in results if i < 2), str(results))
check("被并发取消的凭证最终为 cancelled",
      client.get(f"/api/passes/{p_cancel_id}").json()["status"] == "cancelled")
final_rev = client.get("/api/revision").json()["revision"]
check("revision 恰好推进 3 次（2 建区 + 1 修订；签发/取消不推进）",
      final_rev == base_rev + 3, f"got {final_rev}, base {base_rev}")

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部通行凭证 API 集成用例通过")
