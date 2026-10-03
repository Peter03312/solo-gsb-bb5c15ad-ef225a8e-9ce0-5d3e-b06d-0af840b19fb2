"""区域集合变更审计历史 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_region_changes_api.py
脚本会清空 regions/region_meta/passes/commitments/observations/
observation_events/idempotency_keys/region_change_audit 后跑全套用例，
结束时再次清空，避免影响其它测试脚本。

覆盖：
  * 空历史返回空数组与 null；四种变更类型（create/update/delete/batch）
    在同一事务各产生恰好一条审计记录，字段（revision/occurred_at/
    change_type/region_ids/summary）正确，按 revision 升序；
  * 失败事务（422/404/409）不新增记录；幂等重试回放不新增；
  * 凭证/承诺/观察的签发与取消不产生记录；
  * 游标升序分页（limit/next_cursor）无重复无遗漏，越界游标空页；
  * 游标非法（负数/非整数）、limit 越界返回统一 422 validation_error；
  * 并发写串行提交后，分页遍历与查询期间持续新增写入均不重不漏。
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
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
            cur.execute(
                "TRUNCATE region_change_audit, observation_events, observations, "
                "idempotency_keys, commitments, passes, regions RESTART IDENTITY"
            )
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def changes(**params):
    r = client.get("/api/regions/changes", params=params or {})
    assert r.status_code == 200, r.text
    return r.json()


def fetch_all(limit: int = 100) -> list[dict]:
    """从首页开始按 next_cursor 遍历全部审计记录。"""
    out: list[dict] = []
    cursor = 0
    while True:
        page = changes(cursor=cursor, limit=limit)
        out.extend(page["changes"])
        if page["next_cursor"] is None:
            break
        check("分页游标严格前进", page["next_cursor"] > cursor, str(page["next_cursor"]))
        cursor = page["next_cursor"]
    return out


def audit_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM region_change_audit")
            return cur.fetchone()[0]


reset_all()

# ---- 1. 空历史：空数组与 null ----
page = changes()
check("空历史返回 changes=[] 且 next_cursor=null",
      page == {"changes": [], "next_cursor": None}, str(page))
check("空历史时全局 revision=0",
      client.get("/api/revision").json()["revision"] == 0)
# 无 cursor/limit 参数即首页（可省略游标）
r = client.get("/api/regions/changes")
check("省略游标与页大小 200 且默认页为空白页",
      r.status_code == 200 and r.json()["changes"] == [], r.text)

# ---- 2. 单区登记：create 审计记录 ----
r = client.post("/api/regions", json={
    "name": "A区", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记 A 201 revision=1", r.status_code == 201 and r.json()["revision"] == 1, r.text)
id_a = r.json()["region"]["id"]
page = changes()
check("登记后一条审计记录", len(page["changes"]) == 1 and page["next_cursor"] is None, str(page))
c = page["changes"][0]
check("create 记录字段（revision/type/region_ids）",
      c["revision"] == 1 and c["change_type"] == "create"
      and c["region_ids"] == [id_a], str(c))
check("create 摘要：单操作 version=1 与计数",
      c["summary"] == {
          "operations": [{"index": 0, "action": "create",
                          "region_id": id_a, "version": 1}],
          "total": 1, "create_count": 1, "update_count": 0, "delete_count": 0,
      }, str(c["summary"]))
check("create 记录带发生时间（UTC Z）",
      c["occurred_at"].endswith("Z") and "T" in c["occurred_at"], str(c))

# ---- 3. 单区修订：update，version 与删除前版本保留 ----
r = client.patch(f"/api/regions/{id_a}", json={"name": "A区-改名"})
check("修订 A 200 revision=2", r.status_code == 200 and r.json()["revision"] == 2, r.text)
r = client.patch(f"/api/regions/{id_a}", json={
    "polygon": [[0, 0], [50, 0], [50, 50], [0, 50]],
})
check("再修订 A 200 revision=3", r.status_code == 200 and r.json()["revision"] == 3
      and r.json()["region"]["version"] == 3, r.text)
all_rows = fetch_all()
check("两次修订各一条记录（共 3 条）",
      [c["change_type"] for c in all_rows] == ["create", "update", "update"]
      and [c["revision"] for c in all_rows] == [1, 2, 3], str(all_rows))
check("update 摘要 version 递增（2/3）",
      all_rows[1]["summary"]["operations"][0]["version"] == 2
      and all_rows[2]["summary"]["operations"][0]["version"] == 3
      and all_rows[2]["summary"]["update_count"] == 1,
      str([c["summary"] for c in all_rows[1:]]))
check("update region_ids 为被修订区域",
      all_rows[1]["region_ids"] == [id_a] and all_rows[2]["region_ids"] == [id_a])

# ---- 4. 批量变更：整批一行，涉及编号按请求顺序，摘要含逐操作与计数 ----
rev = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions/batch", json={
    "expected_revision": rev,
    "operations": [
        {"action": "create", "name": "B区",
         "polygon": [[400, 0], [500, 0], [500, 100], [400, 100]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
        {"action": "update", "id": id_a, "name": "A区-批内修订"},
        {"action": "create", "name": "C区",
         "polygon": [[600, 0], [700, 0], [700, 100], [600, 100]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 300, "t": future(60)},
        {"x": 900, "y": 300, "t": future(120)},
    ]],
})
check("批量 create+update+create 200 revision=4",
      r.status_code == 200 and r.json()["revision"] == 4, r.text)
id_b = r.json()["operations"][0]["region_id"]
id_c = r.json()["operations"][2]["region_id"]
batch_row = changes(cursor=3)["changes"][0]
check("批量整批一行 change_type=batch、revision=4",
      batch_row["revision"] == 4 and batch_row["change_type"] == "batch", str(batch_row))
check("批量涉及区域编号按请求操作顺序（含 create 新 id）",
      batch_row["region_ids"] == [id_b, id_a, id_c], str(batch_row["region_ids"]))
check("批量摘要逐操作保留下标/动作/编号/版本",
      batch_row["summary"]["operations"] == [
          {"index": 0, "action": "create", "region_id": id_b, "version": 1},
          {"index": 1, "action": "update", "region_id": id_a, "version": 4},
          {"index": 2, "action": "create", "region_id": id_c, "version": 1},
      ], str(batch_row["summary"]))
check("批量摘要计数 total=3 / 2 create / 1 update / 0 delete",
      batch_row["summary"]["total"] == 3
      and batch_row["summary"]["create_count"] == 2
      and batch_row["summary"]["update_count"] == 1
      and batch_row["summary"]["delete_count"] == 0, str(batch_row["summary"]))

# ---- 5. 删除：delete，摘要回显删除前 version ----
r = client.delete(f"/api/regions/{id_c}")
check("删除 C 200 revision=5", r.status_code == 200 and r.json()["revision"] == 5, r.text)
del_row = changes(cursor=4)["changes"][0]
check("delete 记录 revision=5 / region_ids=[C]",
      del_row["revision"] == 5 and del_row["change_type"] == "delete"
      and del_row["region_ids"] == [id_c], str(del_row))
check("delete 摘要回显删除前 version=1 与计数",
      del_row["summary"]["operations"] == [
          {"index": 0, "action": "delete", "region_id": id_c, "version": 1}]
      and del_row["summary"]["delete_count"] == 1, str(del_row["summary"]))

# ---- 6. 失败事务不产生审计记录 ----
n_before = audit_count()
rev_before = client.get("/api/revision").json()["revision"]
# 6a. 校验失败（时段倒序）422
r = client.post("/api/regions", json={
    "name": "非法", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": WIDE[1], "valid_to": WIDE[0],
})
check("非法时段登记 422", r.status_code == 422, r.text)
# 6b. 删除不存在区域 404
r = client.delete("/api/regions/999999")
check("删除不存在区域 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)
# 6c. 批量 revision 冲突 409
r = client.post("/api/regions/batch", json={
    "expected_revision": 999999,
    "operations": [{"action": "update", "id": id_a, "name": "过期修订"}],
    "trajectories": [[
        {"x": -50, "y": 300, "t": future(60)},
        {"x": 900, "y": 300, "t": future(120)},
    ]],
})
check("过期 revision 批量 409 revision_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)
# 6d. 凭证保护导致整笔回滚 409
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 500, "t": future(120)},
    {"x": 900, "y": 500, "t": future(180)},
]})
check("签发凭证 201", r.status_code == 201, r.text)
active_pass_id = r.json()["id"]
r = client.post("/api/regions", json={
    "name": "挡路区", "polygon": [[-100, 450], [1000, 450], [1000, 550], [-100, 550]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("凭证保护登记 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
# 6e. 批量内轨迹不安全 422 整批回滚
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_before,
    "operations": [
        {"action": "create", "name": "应回滚",
         "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": 0, "y": 5, "t": future(60)},
        {"x": 5, "y": 5, "t": future(120)},
    ]],
})
check("批量轨迹不安全 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
check("所有失败事务均不新增审计记录、不推进 revision",
      audit_count() == n_before
      and client.get("/api/revision").json()["revision"] == rev_before,
      f"{n_before} -> {audit_count()}")

# ---- 7. 幂等重试回放不新增记录 ----
body = {
    "name": "幂等区", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
}
r1 = client.post("/api/regions", json=body, headers={"Idempotency-Key": "audit-create-0001"})
check("带键登记 201", r1.status_code == 201, r1.text)
n_after = audit_count()
rev_after = r1.json()["revision"]
r2 = client.post("/api/regions", json=body, headers={"Idempotency-Key": "audit-create-0001"})
check("同键重试回放原 201 响应", r2.status_code == 201 and r2.json() == r1.json(), r2.text)
check("幂等重试不新增审计记录、不重复推进 revision",
      audit_count() == n_after
      and client.get("/api/revision").json()["revision"] == rev_after,
      f"{n_after} -> {audit_count()}")
# 带键批量与带键删除重试同样不新增
id_idem = r1.json()["region"]["id"]
# 登记已推进 revision，批量需携带推进后的当前 revision（重试必须原样重发同一请求体）
rev_for_batch = client.get("/api/revision").json()["revision"]
batch_body = {
    "expected_revision": rev_for_batch,
    "operations": [{"action": "delete", "id": id_idem}],
    "trajectories": [[
        {"x": -50, "y": 300, "t": future(60)},
        {"x": 900, "y": 300, "t": future(120)},
    ]],
}
rb1 = client.post("/api/regions/batch", json=batch_body,
                  headers={"Idempotency-Key": "audit-batch-0001"})
check("带键批量 200", rb1.status_code == 200, rb1.text)
n_after_b = audit_count()
rb2 = client.post("/api/regions/batch", json=batch_body,
                  headers={"Idempotency-Key": "audit-batch-0001"})
check("带键批量重试回放原响应（含已过期的 expected_revision）",
      rb2.status_code == 200 and rb2.json() == rb1.json(), rb2.text)
rd1 = client.delete(f"/api/regions/{id_b}", headers={"Idempotency-Key": "audit-del-0001"})
n_after_d = audit_count()
rd2 = client.delete(f"/api/regions/{id_b}", headers={"Idempotency-Key": "audit-del-0001"})
check("带键删除重试回放原 200（非 404）",
      rd2.status_code == 200 and rd2.json() == rd1.json(), rd2.text)
check("批量/删除幂等重试均不新增审计记录",
      audit_count() == n_after_d and n_after_d == n_after_b + 1,
      f"{n_after_b} -> {n_after_d} -> {audit_count()}")

# ---- 8. 凭证/承诺/观察的签发与取消不产生区域变更记录 ----
n_before = audit_count()
rev_before = client.get("/api/revision").json()["revision"]
# 观察登记（安全/不安全均可）
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 700, "t": future(300)},
    {"x": 900, "y": 700, "t": future(360)},
]})
check("登记观察 201", r.status_code == 201, r.text)
obs_id = r.json()["id"]
# 承诺签发
r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 900, "t": 0}, {"x": 900, "y": 900, "t": 60}],
    "window_start": future(300), "window_end": future(900),
})
check("签发承诺 201", r.status_code == 201, r.text)
commitment_id = r.json()["id"]
# 取消凭证 / 承诺 / 观察
rr = client.post(f"/api/passes/{active_pass_id}/cancel")
check("取消凭证 200", rr.status_code == 200, rr.text)
check("取消承诺 200",
      client.post(f"/api/commitments/{commitment_id}/cancel").status_code == 200)
check("取消观察 200",
      client.post(f"/api/observations/{obs_id}/cancel").status_code == 200)
check("凭证/承诺/观察的签发与取消不产生审计记录、不推进 revision",
      audit_count() == n_before
      and client.get("/api/revision").json()["revision"] == rev_before,
      f"{n_before} -> {audit_count()}")

# ---- 9. 游标分页：limit / next_cursor，升序无重复无遗漏 ----
# 当前历史已知连续 revision 1..N
rows = fetch_all(limit=1)
revs = [c["revision"] for c in rows]
check("limit=1 遍历全集：revision 从 1 连续递增",
      revs == list(range(1, len(revs) + 1)), str(revs[:20]))
# 小页逐页核对 next_cursor
p1 = changes(limit=2)
check("首页 limit=2 两条，next_cursor=2",
      [c["revision"] for c in p1["changes"]] == [1, 2]
      and p1["next_cursor"] == 2, str(p1))
p2 = changes(cursor=p1["next_cursor"], limit=2)
check("第二页 revision 3,4 衔接",
      [c["revision"] for c in p2["changes"]] == [3, 4], str(p2))
# 末页：总数不整除页大小时最后一页不足 limit 且 next_cursor=null
pages: list[dict] = []
cursor = 0
while True:
    pg = changes(cursor=cursor, limit=3)
    pages.extend(pg["changes"])
    if pg["next_cursor"] is None:
        break
    cursor = pg["next_cursor"]
check("limit=3 遍历全集无重复无遗漏、末页 null",
      [c["revision"] for c in pages] == revs,
      str([c["revision"] for c in pages]))
# 游标超过最大 revision：空页 null
tail = changes(cursor=999999)
check("越界游标返回空页 next_cursor=null",
      tail == {"changes": [], "next_cursor": None}, str(tail))
# limit 取上界 500 也合法
check("limit=500 合法 200", client.get("/api/regions/changes?limit=500").status_code == 200)
# occurred_at 按 revision 升序（同秒内也不逆序）
times = [c["occurred_at"] for c in rows]
check("occurred_at 随 revision 单调不降",
      times == sorted(times), str(times))

# ---- 10. 游标非法 / limit 越界：统一 422 validation_error ----
for qs in ("cursor=-1", "limit=0", "limit=501", "cursor=abc", "limit=1.5",
           "cursor=-1&limit=0"):
    r = client.get(f"/api/regions/changes?{qs}")
    check(f"非法参数 {qs} → 422 validation_error",
          r.status_code == 422
          and r.json()["error"]["code"] == "validation_error"
          and r.json()["error"]["details"], r.text)

# 只读语义：审计接口不接受写方法
r = client.post("/api/regions/changes", json={})
check("POST /api/regions/changes 返回 405（只读）", r.status_code == 405, r.text)

# ---- 11. 并发写后与查询期间持续写入：分页不重不漏 ----
reset_all()

config = uvicorn.Config(app, host="127.0.0.1", port=8011, log_level="warning")
server = uvicorn.Server(config)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
for _ in range(100):
    try:
        httpx.get("http://127.0.0.1:8011/health", timeout=1.0)
        break
    except Exception:
        threading.Event().wait(0.1)

n_workers = 32
barrier = threading.Barrier(n_workers)
errors: list[str] = []
created_ids: list[int] = []
lock = threading.Lock()


def worker(i: int) -> None:
    try:
        barrier.wait()
        rr = httpx.post(
            "http://127.0.0.1:8011/api/regions",
            json={
                "name": f"并发区-{i}",
                "polygon": [[0, 1000 + i], [10, 1000 + i], [10, 1010 + i], [0, 1010 + i]],
                "valid_from": WIDE[0], "valid_to": WIDE[1],
            },
            headers={"Idempotency-Key": f"audit-concurrency-{i}"},
            timeout=30,
        )
        rr.raise_for_status()
        with lock:
            created_ids.append(rr.json()["region"]["id"])
    except Exception as exc:  # noqa: BLE001
        with lock:
            errors.append(f"worker {i}: {exc}")


threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("32 个并发登记全部成功", not errors and len(created_ids) == n_workers,
      f"{errors[:3]} / {len(created_ids)}")

with httpx.Client(base_url="http://127.0.0.1:8011", timeout=30) as hc:
    # 并发写串行提交：审计行 revision 恰好 1..32，每个 create 区域出现一次
    got: list[dict] = []
    cur_rev = 0
    while True:
        pg = hc.get("/api/regions/changes", params={"cursor": cur_rev, "limit": 7})
        data = pg.json()
        got.extend(data["changes"])
        if data["next_cursor"] is None:
            break
        cur_rev = data["next_cursor"]
    check("并发提交后审计 revision 恰为 1..32",
          [c["revision"] for c in got] == list(range(1, n_workers + 1)),
          str([c["revision"] for c in got]))
    check("全部为 create 且每个新区域恰好出现一次",
          sorted(c["change_type"] for c in got) == ["create"] * n_workers
          and sorted(rid for c in got for rid in c["region_ids"]) == sorted(created_ids),
          "mismatch")
    check("并发写摘要均为单操作 create/version=1",
          all(c["summary"]["total"] == 1 and c["summary"]["create_count"] == 1
              and c["summary"]["operations"][0]["version"] == 1 for c in got))

    # 查询期间持续新增：已遍历的页在后续遍历中不重复出现、最终全集不漏
    early = hc.get("/api/regions/changes", params={"cursor": 0, "limit": 5}).json()
    early_revs = [c["revision"] for c in early["changes"]]
    for i in range(4):
        hc.post("/api/regions", json={
            "name": f"翻页期间新增-{i}",
            "polygon": [[0, 2000 + i], [10, 2000 + i], [10, 2010 + i], [0, 2010 + i]],
            "valid_from": WIDE[0], "valid_to": WIDE[1],
        }, headers={"Idempotency-Key": f"audit-during-page-{i}"})
    # 用首页快照的 next_cursor 继续翻页：老页内容不会变（快照），
    # 从头完整重扫恰好得到 32+4 条连续记录，无重复无遗漏
    full: list[dict] = []
    cur_rev = 0
    while True:
        pg = hc.get("/api/regions/changes", params={"cursor": cur_rev, "limit": 10})
        data = pg.json()
        full.extend(data["changes"])
        if data["next_cursor"] is None:
            break
        cur_rev = data["next_cursor"]
    expected_total = n_workers + 4
    check("查询期间新增后全量重扫不重不漏（1..36）",
          [c["revision"] for c in full] == list(range(1, expected_total + 1))
          and len({c["revision"] for c in full}) == expected_total,
          str([c["revision"] for c in full]))
    check("首页快照内容在新增后保持稳定",
          [c["revision"] for c in
           hc.get("/api/regions/changes", params={"cursor": 0, "limit": 5}).json()["changes"]]
          == early_revs)

server.should_exit = True
thread.join(timeout=10)
# 内置 uvicorn 关闭时其 lifespan 会关闭共享连接池；重新初始化后再清理
db.init_pool()

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部区域集合变更审计历史 API 集成用例通过")
