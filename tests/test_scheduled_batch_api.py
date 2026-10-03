"""预约未来批量变更 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_scheduled_batch_api.py
脚本会清空全部业务表后跑全套用例（含执行顺序、取消/执行竞争、重启恢复
与并发至多一次提交）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, scheduler  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


db.init_pool()
client = TestClient(app)

SAFE_TRAJ = [
    {"x": -50, "y": 200, "t": "2030-06-01T00:00:00Z"},
    {"x": 900, "y": 200, "t": "2030-06-01T00:02:00Z"},
]
CROSS_TRAJ = [
    {"x": -50, "y": 50, "t": "2030-06-01T00:00:00Z"},
    {"x": 150, "y": 50, "t": "2030-06-01T00:01:00Z"},
]
BOX_A = [[0, 0], [100, 0], [100, 100], [0, 100]]
WINDOW = ("2030-01-01T00:00:00Z", "2031-01-01T00:00:00Z")


def truncate_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE observation_events, observations, passes, "
                        "commitments, idempotency_keys, region_change_audit, "
                        "regions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE scheduled_batches RESTART IDENTITY CASCADE")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def schedule(execute_at, expected_revision, operations, trajectories=None,
             client=client):
    return client.post("/api/regions/batch/schedule", json={
        "execute_at": iso(execute_at) if isinstance(execute_at, datetime) else execute_at,
        "expected_revision": expected_revision,
        "operations": operations,
        "trajectories": trajectories or [SAFE_TRAJ],
    })


def backdate(sid: int, delta_seconds: int):
    """把预约执行时刻直接改为相对当前的偏移（仅测试用：绕过受理的未来校验）。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE scheduled_batches SET execute_at = now() + (%s || ' seconds')::interval "
                "WHERE id = %s",
                (delta_seconds, sid),
            )
        conn.commit()


def get_sid(sid):
    return client.get(f"/api/regions/batch/schedule/{sid}").json()


def wait_terminal(sid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        g = get_sid(sid)
        if g["status"] != "pending":
            return g
        time.sleep(0.02)
    return get_sid(sid)


def create_region(name="A区", polygon=BOX_A):
    r = client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": WINDOW[0], "valid_to": WINDOW[1]})
    return r


# ================================================================ 受理校验

truncate_all()

# 非未来执行时刻 → 422 execution_time_not_in_future
r = schedule(utcnow() - timedelta(seconds=1), 0,
             [{"action": "create", "name": "A区", "polygon": BOX_A,
               "valid_from": WINDOW[0], "valid_to": WINDOW[1]}])
check("过去执行时刻 422 execution_time_not_in_future",
      r.status_code == 422 and r.json()["error"]["code"] == "execution_time_not_in_future",
      r.text)

# 恰为当前时刻也拒绝（严格未来）
r = schedule(utcnow(), 0,
             [{"action": "create", "name": "A区", "polygon": BOX_A,
               "valid_from": WINDOW[0], "valid_to": WINDOW[1]}])
check("执行时刻=受理时刻 422",
      r.status_code == 422 and r.json()["error"]["code"] == "execution_time_not_in_future",
      r.text)

# 不带时区 → 422 validation_error（与既有时间字段一致）
r = client.post("/api/regions/batch/schedule", json={
    "execute_at": "2030-01-01T00:00:00",
    "expected_revision": 0,
    "operations": [{"action": "create", "name": "A区", "polygon": BOX_A,
                    "valid_from": WINDOW[0], "valid_to": WINDOW[1]}],
    "trajectories": [SAFE_TRAJ]})
check("执行时刻不带时区 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

# 受理时 revision 不匹配**不**拒绝（不预占、不保证届时可提交）
r = schedule(utcnow() + timedelta(hours=1), 99,
             [{"action": "delete", "id": 1}])
check("受理时不校验 expected_revision（过期也受理）",
      r.status_code == 201 and r.json()["status"] == "pending", r.text)

# 受理时引用不存在区域**不**拒绝（届时才 region_not_found）
r = schedule(utcnow() + timedelta(hours=1), 0,
             [{"action": "update", "id": 9999, "name": "x"}])
check("受理时不校验区域存在性（未知区域也受理）",
      r.status_code == 201 and r.json()["status"] == "pending", r.text)

# 受理时轨迹在当前集合下不安全**不**拒绝（不预演安全性）
r = create_region()
check("准备区域 A（revision=1）", r.status_code == 201)
r = schedule(utcnow() + timedelta(hours=1), 1,
             [{"action": "update", "id": 1, "name": "A区"}], [CROSS_TRAJ])
check("受理时不核验轨迹安全（穿越轨迹也受理）",
      r.status_code == 201 and r.json()["status"] == "pending", r.text)

# 受理格式错误：重复区域 422 duplicate_region
r = schedule(utcnow() + timedelta(hours=1), 1,
             [{"action": "update", "id": 1, "name": "x"},
              {"action": "delete", "id": 1}])
check("受理重复区域 422 duplicate_region",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region", r.text)
check("重复明细带两个操作下标",
      r.json()["error"]["details"][0]["operation_indices"] == [0, 1])

# 受理格式错误：create 非法时段 422 invalid_operation
r = schedule(utcnow() + timedelta(hours=1), 1,
             [{"action": "create", "name": "坏", "polygon": BOX_A,
               "valid_from": "2031-01-01T00:00:00Z",
               "valid_to": "2030-01-01T00:00:00Z"}])
check("受理 create 非法时段 422 invalid_operation",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation"
      and r.json()["error"]["details"][0]["operation_index"] == 0, r.text)

# 受理格式错误：update 合并后非法时段 422 invalid_operation
r = schedule(utcnow() + timedelta(hours=1), 1,
             [{"action": "update", "id": 1,
               "valid_from": "2031-06-01T00:00:00Z"}])
check("受理 update 合并后非法时段 422 invalid_operation",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation", r.text)

# 受理格式错误：自相交多边形 422 validation_error（Pydantic/几何层）
r = schedule(utcnow() + timedelta(hours=1), 1,
             [{"action": "create", "name": "坏多边形",
               "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
               "valid_from": WINDOW[0], "valid_to": WINDOW[1]}])
check("受理自相交多边形 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

# 受理格式错误：轨迹逆序 422 invalid_trajectory
r = schedule(utcnow() + timedelta(hours=1), 1,
             [{"action": "update", "id": 1, "name": "x"}],
             [[{"x": 0, "y": 0, "t": "2030-06-01T00:00:10Z"},
               {"x": 1, "y": 1, "t": "2030-06-01T00:00:00Z"}]])
check("受理逆序轨迹 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

# 受理格式错误：空操作/无轨迹/未知 action/多余字段
r = client.post("/api/regions/batch/schedule", json={
    "execute_at": iso(utcnow() + timedelta(hours=1)),
    "expected_revision": 1, "operations": [], "trajectories": [SAFE_TRAJ]})
check("受理空操作 422", r.status_code == 422)
r = client.post("/api/regions/batch/schedule", json={
    "execute_at": iso(utcnow() + timedelta(hours=1)),
    "expected_revision": 1,
    "operations": [{"action": "update", "id": 1, "name": "x"}],
    "trajectories": []})
check("受理无轨迹 422", r.status_code == 422)
r = client.post("/api/regions/batch/schedule", json={
    "execute_at": iso(utcnow() + timedelta(hours=1)),
    "expected_revision": 1,
    "operations": [{"action": "frob", "id": 1}],
    "trajectories": [SAFE_TRAJ]})
check("受理未知 action 422", r.status_code == 422)
r = client.post("/api/regions/batch/schedule", json={
    "execute_at": iso(utcnow() + timedelta(hours=1)),
    "expected_revision": 1, "bogus": 1,
    "operations": [{"action": "delete", "id": 1}],
    "trajectories": [SAFE_TRAJ]})
check("受理多余字段 422", r.status_code == 422)

# 受理失败不落库：此前受理成功的待执行预约共 3 笔（过期 revision、
# 未知区域、穿越轨迹各一笔），422 之后数量不增加
with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.row_factory = dict_row
        cur.execute("SELECT count(*) AS n FROM scheduled_batches")
        accepted = cur.fetchone()["n"]
check("受理失败不留预约（库内仅 3 笔此前成功受理的预约）", accepted == 3,
      f"got {accepted}")

# ================================================================ 成功受理响应

r = schedule(utcnow() + timedelta(hours=2), 1,
             [{"action": "update", "id": 1, "name": "A区-未来"},
              {"action": "create", "name": "新区", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
               "valid_from": WINDOW[0], "valid_to": WINDOW[1]}])
check("成功受理 201 pending", r.status_code == 201, r.text)
body = r.json()
check("受理返回编号与 pending",
      isinstance(body["id"], int) and body["status"] == "pending")
check("执行时刻归一化为 UTC（Z）",
      body["execute_at"].endswith("Z") or "+00:00" in body["execute_at"], str(body["execute_at"]))
check("回显 expected_revision", body["expected_revision"] == 1)
check("回显有序 operations（顺序与请求一致）",
      [op.get("name") for op in body["operations"]] == ["A区-未来", "新区"])
check("回显 trajectories",
      body["trajectories"][0][0]["t"].endswith("Z"))
check("pending 时 result/error/committed_revision 为空",
      body["result"] is None and body["error"] is None
      and body["committed_revision"] is None)
check("pending 时无 started/finished/cancelled 时间戳",
      body["started_at"] is None and body["finished_at"] is None
      and body["cancelled_at"] is None)
check("created_at 存在", body["created_at"] is not None)

# 时区偏移受理：+08:00 被归一化为 UTC
r = client.post("/api/regions/batch/schedule", json={
    "execute_at": "2030-01-02T08:00:00+08:00",
    "expected_revision": 1,
    "operations": [{"action": "delete", "id": 1}],
    "trajectories": [SAFE_TRAJ]})
check("+08:00 执行时刻可受理并归一化",
      r.status_code == 201 and r.json()["execute_at"] == "2030-01-02T00:00:00Z", r.text)

# 受理不推进 revision
check("受理不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 1)

# 未知编号查询 404
check("未知预约查询 404",
      client.get("/api/regions/batch/schedule/424242").status_code == 404
      and client.get("/api/regions/batch/schedule/424242").json()["error"]["code"] == "not_found")
# 非正整数编号 422（路径校验）
check("编号非正整数 422",
      client.get("/api/regions/batch/schedule/0").status_code == 422)

# ================================================================ 到期成功执行

truncate_all()
create_region()  # revision=1, id=1
rev_before = client.get("/api/revision").json()["revision"]

# 一笔安全的未来批量：删除 A，轨迹从上方安全经过
r = schedule(utcnow() + timedelta(minutes=10), 1,
             [{"action": "delete", "id": 1}])
sid = r.json()["id"]
backdate(sid, -1)
n = scheduler.run_due_scheduled_batches()
check("到期处理 1 笔", n == 1, str(n))
g = wait_terminal(sid)
check("到期成功状态 succeeded", g["status"] == "succeeded", str(g))
check("成功提交后新 revision=2", g["committed_revision"] == 2, str(g))
check("result 与批量成功响应一致（revision/operations/trajectories）",
      g["result"]["revision"] == 2
      and g["result"]["operations"] == [
          {"index": 0, "action": "delete", "region_id": 1, "version": 1}]
      and g["result"]["trajectories"][0]["safe"] is True
      and g["result"]["trajectories"][0]["results"] == [], str(g["result"]))
check("成功后区域确已删除",
      client.get("/api/regions/1").status_code == 404)
check("成功后审计为批量变更一行",
      [ch["change_type"] for ch in client.get("/api/regions/changes").json()["changes"]]
      == ["create", "batch"])
check("成功后 finished_at 置位、cancelled_at 为空",
      g["finished_at"] is not None and g["cancelled_at"] is None)
check("全局 revision 推进 1→2",
      client.get("/api/revision").json()["revision"] == rev_before + 1)

# 成功执行不写第二条审计（幂等：再跑一轮不会重复提交）
n = scheduler.run_due_scheduled_batches()
check("终态预约不再被领取（处理 0 笔）", n == 0)
check("重复执行不重复推进 revision",
      client.get("/api/revision").json()["revision"] == 2)
check("审计仍只有 2 行",
      len(client.get("/api/regions/changes").json()["changes"]) == 2)

# ================================================================ 到期失败：各类批量错误码，区域不变

truncate_all()
create_region()  # rev=1

def fail_case(label, execute_rev, operations, trajectories=None, expect_code=None,
              expect_status="failed"):
    r = schedule(utcnow() + timedelta(minutes=10), execute_rev, operations,
                 trajectories or [SAFE_TRAJ])
    sid = r.json()["id"]
    backdate(sid, -1)
    scheduler.run_due_scheduled_batches()
    g = wait_terminal(sid)
    ok = g["status"] == expect_status and (g["error"] or {}).get("code") == expect_code
    check(label, ok, str(g))
    return g


rev = client.get("/api/revision").json()["revision"]
g = fail_case("到期 revision 过期 → failed/revision_conflict",
              rev + 50, [{"action": "delete", "id": 1}], expect_code="revision_conflict")
check("失败原因 details 带当前 revision",
      g["error"]["details"][0]["current_revision"] == rev)
check("revision_conflict 不改变区域集合",
      client.get("/api/revision").json()["revision"] == rev
      and client.get("/api/regions/1").status_code == 200)
check("失败后 result 为空、committed_revision 为空",
      g["result"] is None and g["committed_revision"] is None)

g = fail_case("到期区域不存在 → failed/region_not_found",
              rev, [{"action": "delete", "id": 7777}], expect_code="region_not_found")
check("region_not_found details 带操作下标与 id",
      g["error"]["details"][0]["operation_index"] == 0
      and g["error"]["details"][0]["region_id"] == 7777)

g = fail_case("到期轨迹不安全 → failed/unsafe_trajectory",
              rev, [{"action": "update", "id": 1, "name": "A区"}],
              trajectories=[CROSS_TRAJ], expect_code="unsafe_trajectory")
check("unsafe details 定位轨迹下标 0 与区域 1",
      g["error"]["details"][0]["trajectory_index"] == 0
      and {x["region_id"] for x in g["error"]["details"][0]["results"]} == {1})
check("unsafe 不改变区域集合（名称未改）",
      client.get("/api/regions/1").json()["name"] == "A区"
      and client.get("/api/revision").json()["revision"] == rev)

# 凭证保护：登记一个未来凭证沿 y=200 通行；预约把 A 向北扩大到 y=300
# （凭证将被挡住），但批量自带轨迹走 y=400，本身仍安全——此时才到
# pass_conflict 检查
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2030-06-01T00:00:00Z"},
    {"x": 900, "y": 200, "t": "2030-06-01T00:02:00Z"}]})
check("准备有效凭证（y=200 当前安全）", r.status_code == 201, r.text)
pass_id = r.json()["id"]
batch_safe_above = [
    {"x": -50, "y": 400, "t": "2030-06-01T00:00:00Z"},
    {"x": 900, "y": 400, "t": "2030-06-01T00:02:00Z"}]
g = fail_case("到期凭证冲突 → failed/pass_conflict",
              rev,
              [{"action": "update", "id": 1,
                "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]]}],
              trajectories=[batch_safe_above],
              expect_code="pass_conflict")
check("pass_conflict details 列出凭证 id",
      [d["pass_id"] for d in g["error"]["details"]] == [pass_id])
check("pass_conflict 不改变区域集合（几何/版本未改）",
      client.get("/api/regions/1").json()["version"] == 1
      and client.get("/api/regions/1").json()["name"] == "A区"
      and client.get("/api/revision").json()["revision"] == rev)

# 承诺保护：签发窗口承诺；预约抹除全部安全出发时刻
client.post(f"/api/passes/{pass_id}/cancel")
r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 50, "t": 0}, {"x": 150, "y": 50, "t": 60}],
    "window_start": "2030-06-01T00:00:00Z",
    "window_end": "2030-06-01T01:00:00Z"})
# 当前 A=[0,100]×[0,100]、生效 2030 全年：该行程在窗口内全程穿越 → 承诺会拒签。
# 先删掉 A 再签发承诺，然后预约重新登记覆盖全部时刻的区域来清空承诺。
if r.status_code == 422:
    client.delete("/api/regions/1")
    rev = client.get("/api/revision").json()["revision"]
    r = client.post("/api/commitments", json={
        "points": [{"x": -50, "y": 50, "t": 0}, {"x": 150, "y": 50, "t": 60}],
        "window_start": "2030-06-01T00:00:00Z",
        "window_end": "2030-06-01T01:00:00Z"})
check("准备有效承诺", r.status_code == 201, r.text)
commitment_id = r.json()["id"]
g = fail_case("到期承诺冲突 → failed/commitment_conflict",
              rev,
              [{"action": "create", "name": "清空承诺区",
                "polygon": [[-1000, -1000], [1000, -1000], [1000, 1000], [-1000, 1000]],
                "valid_from": "2030-01-01T00:00:00Z",
                "valid_to": "2031-01-01T00:00:00Z"}],
              trajectories=[[
                  {"x": -50, "y": 5000, "t": "2030-06-01T00:00:00Z"},
                  {"x": 900, "y": 5000, "t": "2030-06-01T00:02:00Z"}]],
              expect_code="commitment_conflict")
check("commitment_conflict details 列出承诺 id",
      [d["commitment_id"] for d in g["error"]["details"]] == [commitment_id])
check("commitment_conflict 不改变区域集合",
      client.get("/api/revision").json()["revision"] == rev)

# 未知区域 + 重复区域在到期也按批量错误码失败（受理时区域不存在被放行）
truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]
r = schedule(utcnow() + timedelta(minutes=10), rev,
             [{"action": "update", "id": 4242, "name": "x"}])
sid = r.json()["id"]
backdate(sid, -1)
scheduler.run_due_scheduled_batches()
g = wait_terminal(sid)
check("受理后区域被删 → 到期 region_not_found",
      g["status"] == "failed" and g["error"]["code"] == "region_not_found", str(g))

# ================================================================ 观察翻转事件随成功执行同事务

truncate_all()
# 区域 A 覆盖 CROSS_TRAJ（y=50）：登记观察为 unsafe；到期批量删除 A → 翻转为 safe
create_region()
rev = client.get("/api/revision").json()["revision"]
r = client.post("/api/observations", json={"points": CROSS_TRAJ})
check("登记观察（当前 unsafe）",
      r.status_code == 201 and r.json()["conclusion"] == "unsafe", r.text)
obs_id = r.json()["id"]
r = schedule(utcnow() + timedelta(minutes=10), rev,
             [{"action": "delete", "id": 1}])
sid = r.json()["id"]
backdate(sid, -1)
scheduler.run_due_scheduled_batches()
g = wait_terminal(sid)
check("删除 A 的预约成功（观察翻转随之提交）", g["status"] == "succeeded", str(g))
ev = client.get(f"/api/observations/{obs_id}/events").json()
check("成功执行在同一事务产生一条 unsafe→safe 翻转事件",
      len(ev["events"]) == 1
      and ev["events"][0]["old_conclusion"] == "unsafe"
      and ev["events"][0]["new_conclusion"] == "safe"
      and ev["events"][0]["revision"] == g["committed_revision"], str(ev))
check("观察最近结论已更新为 safe",
      client.get(f"/api/observations/{obs_id}").json()["conclusion"] == "safe")

# 失败执行不产生观察事件
truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]
r = client.post("/api/observations", json={"points": CROSS_TRAJ})
obs_id = r.json()["id"]
r = schedule(utcnow() + timedelta(minutes=10), 99,
             [{"action": "delete", "id": 1}])
sid = r.json()["id"]
backdate(sid, -1)
scheduler.run_due_scheduled_batches()
wait_terminal(sid)
check("失败执行不产生观察事件、结论不变",
      client.get(f"/api/observations/{obs_id}/events").json()["events"] == []
      and client.get(f"/api/observations/{obs_id}").json()["conclusion"] == "unsafe")
check("失败执行不写区域审计",
      [ch["change_type"] for ch in client.get("/api/regions/changes").json()["changes"]]
      == ["create"])

# ================================================================ 执行顺序：按执行时刻、编号

truncate_all()
# 三笔到期预约：按 (execute_at, id) 处理，全部为 create 互不冲突区域。
# 各笔 expected_revision 与其提交顺序一致（0→1→2），到期重验才能全部成功，
# 这也顺带验证了处理顺序：顺序错了后续笔必然 revision_conflict。
base = utcnow() + timedelta(minutes=10)
specs = [
    (base + timedelta(seconds=30), "晚", 500, 2),
    (base + timedelta(seconds=10), "早A", 600, 0),
    (base + timedelta(seconds=10), "早B", 700, 1),
]
ids = []
for execute_at, name, x, expected_rev in specs:
    r = schedule(execute_at, expected_rev,
                 [{"action": "create", "name": name,
                   "polygon": [[x, 0], [x + 10, 0], [x + 10, 10], [x, 10]],
                   "valid_from": WINDOW[0], "valid_to": WINDOW[1]}])
    ids.append((r.json()["id"], name))
# 统一按期望处理顺序回填到期时刻：早A、早B 同一时刻（再按 id 排），晚更晚；
# 若逐行用 now() 回填会因时钟推移破坏设定的先后关系
sid_early_a = next(sid for sid, name in ids if name == "早A")
sid_early_b = next(sid for sid, name in ids if name == "早B")
sid_late = next(sid for sid, name in ids if name == "晚")
with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduled_batches SET execute_at = now() - interval '2 second' "
            "WHERE id = ANY(%s)",
            ([sid_early_a, sid_early_b],))
        cur.execute(
            "UPDATE scheduled_batches SET execute_at = now() - interval '1 second' "
            "WHERE id = %s",
            (sid_late,))
    conn.commit()
# 记录处理顺序（按 finished_at 与 committed_revision 都是提交顺序）
scheduler.run_due_scheduled_batches()
order = []
for sid, name in ids:
    g = wait_terminal(sid)
    order.append((g["committed_revision"], name, g["status"]))
check("全部成功", all(status == "succeeded" for _, _, status in order), str(order))
order.sort(key=lambda item: (item[0] is None, item[0]))
# revision 从 1 开始（首笔 create 区域）
check("按（执行时刻、编号）顺序提交：早A→早B→晚",
      [name for _, name, _ in order] == ["早A", "早B", "晚"], str(order))
check("revision 连续为 1,2,3",
      [rev for rev, _, _ in order] == [1, 2, 3], str(order))

# ================================================================ 取消

truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]

# 取消待执行
r = schedule(utcnow() + timedelta(hours=1), rev, [{"action": "delete", "id": 1}])
sid = r.json()["id"]
r = client.post(f"/api/regions/batch/schedule/{sid}/cancel")
check("取消待执行 200 cancelled/cancelled=true",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["cancelled"] is True, r.text)
check("取消置 cancelled_at", r.json()["cancelled_at"] is not None)
# 重复取消幂等
r2 = client.post(f"/api/regions/batch/schedule/{sid}/cancel")
check("重复取消幂等 200 且 cancelled=false",
      r2.status_code == 200 and r2.json()["status"] == "cancelled"
      and r2.json()["cancelled"] is False, r2.text)
# 已取消的预约到期不再执行
backdate(sid, -10)
n = scheduler.run_due_scheduled_batches()
check("已取消预约不被执行", n == 0)
g = get_sid(sid)
check("取消后仍为 cancelled、区域与 revision 不变",
      g["status"] == "cancelled" and g["result"] is None and g["error"] is None
      and client.get("/api/revision").json()["revision"] == rev)
# 取消未知编号 404
r = client.post("/api/regions/batch/schedule/555555/cancel")
check("取消未知编号 404",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found")

# 成功/失败终态取消 → 409 scheduled_batch_terminal（可区分于未知 404）
r = schedule(utcnow() + timedelta(minutes=10), rev, [{"action": "delete", "id": 1}])
sid_ok = r.json()["id"]
backdate(sid_ok, -1)
scheduler.run_due_scheduled_batches()
wait_terminal(sid_ok)
r = client.post(f"/api/regions/batch/schedule/{sid_ok}/cancel")
check("取消已成功预约 409 scheduled_batch_terminal",
      r.status_code == 409 and r.json()["error"]["code"] == "scheduled_batch_terminal"
      and r.json()["error"]["details"][0]["status"] == "succeeded", r.text)

# 再建区域以制造一笔失败
create_region(name="B区", polygon=[[200, 0], [300, 0], [300, 100], [200, 100]])
rev = client.get("/api/revision").json()["revision"]
r = schedule(utcnow() + timedelta(minutes=10), 9999, [{"action": "delete", "id": 2}])
sid_fail = r.json()["id"]
backdate(sid_fail, -1)
scheduler.run_due_scheduled_batches()
wait_terminal(sid_fail)
r = client.post(f"/api/regions/batch/schedule/{sid_fail}/cancel")
check("取消已失败预约 409 且 details 状态为 failed",
      r.status_code == 409
      and r.json()["error"]["details"][0]["status"] == "failed", r.text)

# ================================================================ 取消/执行竞争：只产生一个终态

truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]

olock = threading.Lock()


# 多轮竞争（成功路径）：终态要么 cancelled 要么 succeeded，二者一致
races = 0
for _ in range(8):
    # 每轮基于当时的 revision/区域集合构造一笔删除预约
    cur_rev = client.get("/api/revision").json()["revision"]
    regions_now = client.get("/api/regions").json()
    if not regions_now:
        rr = client.post("/api/regions", json={
            "name": "A区", "polygon": BOX_A,
            "valid_from": WINDOW[0], "valid_to": WINDOW[1]})
        cur_rev = rr.json()["revision"]
        regions_now = [rr.json()["region"]]
    target_id = regions_now[0]["id"]
    r = schedule(
        utcnow() + timedelta(minutes=10),
        cur_rev,
        [{"action": "delete", "id": target_id}])
    sid = r.json()["id"]
    backdate(sid, -1)
    barrier = threading.Barrier(2)
    results = {}

    def execute():
        barrier.wait()
        scheduler.run_due_scheduled_batches()

    def cancel():
        barrier.wait()
        rr = client.post(f"/api/regions/batch/schedule/{sid}/cancel")
        with olock:
            if rr.status_code == 200:
                results["cancel"] = ("cancelled", rr.json()["cancelled"])
            else:
                results["cancel"] = (rr.json()["error"]["code"],
                                     rr.json()["error"]["details"][0].get("status"))

    t1 = threading.Thread(target=execute)
    t2 = threading.Thread(target=cancel)
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)
    # 两个事务都已结束后再读终态（避免在线程提交前抢先读到 pending）
    g = get_sid(sid)
    with olock:
        results["exec"] = g["status"]
    exec_status = results["exec"]
    cancel_kind = results["cancel"]
    final = g["status"]
    consistent = exec_status == final
    if final == "succeeded":
        # 执行先提交：取消拿到 409 terminal(succeeded)，区域已删
        ok = cancel_kind == ("scheduled_batch_terminal", "succeeded")
    else:
        # 取消先到：执行器看不到该预约（处理 0 笔），取消 200
        ok = cancel_kind == ("cancelled", True) and exec_status == "cancelled"
    races += 1
    check(f"竞争轮 {races} 只有一个终态（最终={final}）", consistent and ok,
          f"{results} final={final}")

# ================================================================ 并发执行：同一预约至多提交一次

truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]
r = schedule(utcnow() + timedelta(minutes=10), rev, [{"action": "delete", "id": 1}])
sid = r.json()["id"]
backdate(sid, -1)

barrier = threading.Barrier(5)
exec_results: list[str] = []
elock = threading.Lock()


def concurrent_exec():
    barrier.wait()
    scheduler.run_due_scheduled_batches()
    with elock:
        exec_results.append(get_sid(sid)["status"])


threads = [threading.Thread(target=concurrent_exec) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=15)

check("并发执行后终态唯一 succeeded",
      get_sid(sid)["status"] == "succeeded", str(exec_results))
check("区域只被删除一次（revision 恰好 +1）",
      client.get("/api/revision").json()["revision"] == rev + 1)
check("批量审计恰好一行",
      len([ch for ch in client.get("/api/regions/changes").json()["changes"]
           if ch["change_type"] == "batch"]) == 1)

# ================================================================ 重启恢复：pending 到期后仍会执行

truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]
r = schedule(utcnow() + timedelta(minutes=10), rev, [{"action": "delete", "id": 1}])
sid = r.json()["id"]
backdate(sid, -10)

# 模拟重启：不经过任何进程内状态，直接在全新连接/新的执行轮里领取
# （run_due_scheduled_batches 不依赖内存；再叠加一个新的 worker 实例）
fresh_worker = scheduler.ScheduledBatchWorker()
n = scheduler.run_due_scheduled_batches()
check("重启后到期 pending 被重新领取执行（仍只一次）", n == 1)
g = wait_terminal(sid)
check("重启恢复后 succeeded 且 revision+1",
      g["status"] == "succeeded"
      and client.get("/api/revision").json()["revision"] == rev + 1, str(g))
fresh_worker.stop(timeout=0.1)  # 未 start，无副作用

# ================================================================ 失败后不阻塞后续到期预约

truncate_all()
create_region()
rev = client.get("/api/revision").json()["revision"]
# 两笔都到期：第一笔必失败（revision 过期），第二笔可成功（删除区域）
r1 = schedule(utcnow() + timedelta(minutes=10), 9999,
              [{"action": "update", "id": 1, "name": "x"}])
id_early = r1.json()["id"]
r2 = schedule(utcnow() + timedelta(minutes=20), rev,
              [{"action": "delete", "id": 1}])
id_late = r2.json()["id"]
backdate(id_early, -2)
backdate(id_late, -1)
n = scheduler.run_due_scheduled_batches()
check("一轮处理两笔（失败不阻塞后续）", n == 2, str(n))
g1 = wait_terminal(id_early)
g2 = wait_terminal(id_late)
check("早笔失败、晚笔成功",
      g1["status"] == "failed" and g1["error"]["code"] == "revision_conflict"
      and g2["status"] == "succeeded", f"{g1['status']} {g2['status']}")

# ================================================================ 查询字段完整性

truncate_all()
r = schedule(utcnow() + timedelta(hours=1), 0,
             [{"action": "create", "name": "查询区", "polygon": BOX_A,
               "valid_from": WINDOW[0], "valid_to": WINDOW[1]}])
g = r.json()
fields = {"id", "status", "execute_at", "expected_revision", "operations",
          "trajectories", "created_at", "started_at", "finished_at",
          "cancelled_at", "committed_revision", "result", "error"}
check("查询响应含全部约定字段", fields <= set(g.keys()),
      str(sorted(set(g.keys()) - fields)))

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部预约未来批量变更 API 集成用例通过")
