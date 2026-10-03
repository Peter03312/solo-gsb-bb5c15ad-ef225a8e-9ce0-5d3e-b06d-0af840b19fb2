"""幂等键（Idempotency-Key）集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_idempotency_api.py
脚本会清空 regions/region_meta/passes/idempotency_keys 后跑全套用例（含并发），
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
import threading
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


WIDE = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
SAFE_TRAJ = [
    {"x": -50, "y": 200, "t": "2026-09-30T00:00:00Z"},
    {"x": 900, "y": 200, "t": "2026-09-30T00:02:00Z"},
]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE passes RESTART IDENTITY")
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("TRUNCATE idempotency_keys")
            cur.execute("TRUNCATE region_change_audit")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def region_count() -> int:
    return len(client.get("/api/regions").json())


def revision() -> int:
    return client.get("/api/revision").json()["revision"]


def key_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM idempotency_keys")
            return cur.fetchone()[0]


def region_payload(name, x0=0):
    return {
        "name": name,
        "polygon": [[x0, 0], [x0 + 10, 0], [x0 + 10, 10], [x0, 10]],
        "valid_from": WIDE[0],
        "valid_to": WIDE[1],
    }


reset_all()

# ================================================================= 登记 + 幂等重试
# ---- 首次成功提交，重试返回原状态与原响应 ----
K = "retry-create-0001"
r1 = client.post("/api/regions", json=region_payload("A区"), headers={"Idempotency-Key": K})
check("带键登记 201", r1.status_code == 201, r1.text)
b1 = r1.json()
check("首次登记 revision=1", b1["revision"] == 1, str(b1))

r2 = client.post("/api/regions", json=region_payload("A区"), headers={"Idempotency-Key": K})
check("同键同内容重试仍返回 201（原状态）", r2.status_code == 201, r2.text)
check("重试返回原响应体（同区域 id、同 revision）", r2.json() == b1, r2.text)
check("重试未推进 revision", revision() == 1)
check("重试未新增区域", region_count() == 1)
check("幂等键已落库 1 个", key_count() == 1)

# ---- revision 被其它变更推进后，重试仍返回原响应 ----
r = client.post("/api/regions", json=region_payload("B区", 20))
check("无键登记保持既有行为 201 revision=2", r.status_code == 201 and r.json()["revision"] == 2, r.text)
r3 = client.post("/api/regions", json=region_payload("A区"), headers={"Idempotency-Key": K})
check("revision 已变化，重试仍返回原响应（revision=1）",
      r3.status_code == 201 and r3.json() == b1, r3.text)
check("重试后全局 revision 仍=2", revision() == 2)
check("重试后区域仍 2 个", region_count() == 2)

# ---- 规范化：JSON 键序/时区写法不同但语义相同 → 仍判定为同一请求 ----
r4 = client.post(
    "/api/regions",
    content=(
        '{"valid_to": "2030-01-01T08:00:00+08:00", "valid_from": "2020-01-01T08:00:00+08:00",'
        ' "polygon": [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], "name": "A区"}'
    ),
    headers={"Content-Type": "application/json", "Idempotency-Key": K},
)
check("等价请求（键序/时区/显式闭合不同）重放原响应", r4.status_code == 201 and r4.json() == b1, r4.text)
check("等价重试仍未新增区域", region_count() == 2)

# ---- 同键不同内容 → 409，不变更 ----
r = client.post("/api/regions", json=region_payload("A区-改名"), headers={"Idempotency-Key": K})
check("同键不同内容 409", r.status_code == 409, r.text)
check("错误码 idempotency_key_conflict", r.json()["error"]["code"] == "idempotency_key_conflict", r.text)
check("409 后 revision 不变", revision() == 2)
check("409 后区域数不变", region_count() == 2)

# ---- 跨操作类型共用键空间：登记用过的键再用于删除 → 409 ----
regions = client.get("/api/regions").json()
b_id = next(x["id"] for x in regions if x["name"] == "B区")
r = client.delete(f"/api/regions/{b_id}", headers={"Idempotency-Key": K})
check("同键跨操作类型（create→delete）409", r.status_code == 409
      and r.json()["error"]["code"] == "idempotency_key_conflict", r.text)
check("目标区域未被删除", region_count() == 2)

# ================================================================= 非法键
for bad, label in [("", "空键"), ("   ", "纯空白键"), ("x" * 129, "129 字符键")]:
    r = client.post("/api/regions", json=region_payload("坏键区", 100),
                    headers={"Idempotency-Key": bad})
    check(f"{label} 422", r.status_code == 422, r.text)
    check(f"{label} 错误码 invalid_idempotency_key",
          r.json()["error"]["code"] == "invalid_idempotency_key", r.text)
check("非法键未产生任何变更", revision() == 2 and region_count() == 2)
r = client.post("/api/regions", json=region_payload("边界键区", 100),
                headers={"Idempotency-Key": "k" * 128})
check("128 字符键合法 201", r.status_code == 201, r.text)
check("边界键登记推进 revision=3", revision() == 3)

# ================================================================= 失败业务不占键
# ---- 422（非法多边形）不占键 ----
KF = "retry-after-failure"
r = client.post("/api/regions", json={
    "name": "自相交", "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
}, headers={"Idempotency-Key": KF})
check("非法多边形 422", r.status_code == 422, r.text)
check("失败请求不占键", key_count() == 2, f"keys={key_count()}")
r = client.post("/api/regions", json=region_payload("失败后成功区", 120),
                headers={"Idempotency-Key": KF})
check("同键修正后成功 201", r.status_code == 201, r.text)
fix_id = r.json()["region"]["id"]

# ---- 404（删除不存在区域）不占键 ----
KF2 = "retry-delete-404"
r = client.delete("/api/regions/9999", headers={"Idempotency-Key": KF2})
check("删除不存在区域 404", r.status_code == 404, r.text)
r = client.delete(f"/api/regions/{fix_id}", headers={"Idempotency-Key": KF2})
check("404 不占键：同键删除已存在区域 200", r.status_code == 200, r.text)

# ================================================================= 修订 + 幂等重试
KP = "retry-patch-0001"
regions = client.get("/api/regions").json()
a_id = next(x["id"] for x in regions if x["name"] == "A区")
rev_before = revision()
r1 = client.patch(f"/api/regions/{a_id}", json={"name": "A区-修订"},
                  headers={"Idempotency-Key": KP})
check("带键修订 200", r1.status_code == 200, r1.text)
check("修订后 version=2", r1.json()["region"]["version"] == 2, r1.text)
r2 = client.patch(f"/api/regions/{a_id}", json={"name": "A区-修订"},
                  headers={"Idempotency-Key": KP})
check("修订重试返回原响应", r2.status_code == 200 and r2.json() == r1.json(), r2.text)
check("重试未再次推进 version（仍=2）",
      client.get(f"/api/regions/{a_id}").json()["version"] == 2)
check("重试未再次推进 revision", revision() == rev_before + 1)
r = client.patch(f"/api/regions/{a_id}", json={"name": "A区-别的"},
                 headers={"Idempotency-Key": KP})
check("同键不同修订内容 409", r.status_code == 409
      and r.json()["error"]["code"] == "idempotency_key_conflict", r.text)
check("409 未变更名称", client.get(f"/api/regions/{a_id}").json()["name"] == "A区-修订")

# ================================================================= 删除 + 幂等重试
KD = "retry-delete-0001"
r = client.post("/api/regions", json=region_payload("待删区", 200))
del_id = r.json()["region"]["id"]
r1 = client.delete(f"/api/regions/{del_id}", headers={"Idempotency-Key": KD})
check("带键删除 200", r1.status_code == 200, r1.text)
r2 = client.delete(f"/api/regions/{del_id}", headers={"Idempotency-Key": KD})
check("删除重试返回原 200（而非 404）", r2.status_code == 200 and r2.json() == r1.json(), r2.text)
check("删除重试未推进 revision", revision() == r1.json()["revision"])
r = client.delete(f"/api/regions/{a_id}", headers={"Idempotency-Key": KD})
check("同键删除不同目标 409", r.status_code == 409
      and r.json()["error"]["code"] == "idempotency_key_conflict", r.text)
check("不同目标未被删除", client.get(f"/api/regions/{a_id}").status_code == 200)

# ================================================================= 批量变更 + 幂等重试
KB = "retry-batch-0001"
rev_before = revision()
batch_body = {
    "expected_revision": rev_before,
    "operations": [
        {"action": "create", "name": "批-C区",
         "polygon": [[400, 0], [410, 0], [410, 10], [400, 10]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
        {"action": "update", "id": a_id, "name": "A区-批改"},
    ],
    "trajectories": [SAFE_TRAJ],
}
r1 = client.post("/api/regions/batch", json=batch_body, headers={"Idempotency-Key": KB})
check("带键批量 200", r1.status_code == 200, r1.text)
bb = r1.json()
check("整批 revision 只 +1", bb["revision"] == rev_before + 1, str(bb))
created_id = next(o["region_id"] for o in bb["operations"] if o["action"] == "create")

r2 = client.post("/api/regions/batch", json=batch_body, headers={"Idempotency-Key": KB})
check("批量重试返回原响应（即使 expected_revision 已过期）",
      r2.status_code == 200 and r2.json() == bb, r2.text)
check("批量重试未推进 revision", revision() == rev_before + 1)
check("批量重试未新增区域", region_count() == len(client.get("/api/regions").json()))
# 重试没有重新分配区域 ID：下一个登记应拿到 created_id + 1
r = client.post("/api/regions", json=region_payload("ID 探针", 500))
check("重试未占用新的区域 ID", r.json()["region"]["id"] == created_id + 1,
      f"created={created_id}, probe={r.json()['region']['id']}")

# ---- 批量失败不占键：revision 冲突（409）与不安全轨迹（422） ----
KB2 = "retry-batch-conflict"
bad_batch = dict(batch_body, expected_revision=999)
bad_batch["operations"] = [{"action": "create", "name": "批-D区",
                            "polygon": [[600, 0], [610, 0], [610, 10], [600, 10]],
                            "valid_from": WIDE[0], "valid_to": WIDE[1]}]
r = client.post("/api/regions/batch", json=bad_batch, headers={"Idempotency-Key": KB2})
check("批量 revision 冲突 409 revision_conflict", r.status_code == 409
      and r.json()["error"]["code"] == "revision_conflict", r.text)
good_batch = dict(bad_batch, expected_revision=revision())
r = client.post("/api/regions/batch", json=good_batch, headers={"Idempotency-Key": KB2})
check("409 不占键：修正 revision 后同键成功 200", r.status_code == 200, r.text)

KB3 = "retry-batch-unsafe"
cross_traj = [{"x": 595, "y": 5, "t": "2026-09-30T00:00:00Z"},
              {"x": 615, "y": 5, "t": "2026-09-30T00:01:00Z"}]
unsafe_batch = {
    "expected_revision": revision(),
    "operations": [{"action": "create", "name": "批-E区",
                    "polygon": [[700, 0], [710, 0], [710, 10], [700, 10]],
                    "valid_from": WIDE[0], "valid_to": WIDE[1]}],
    "trajectories": [cross_traj],
}
# 轨迹穿过刚登记的 批-D区 [600,610] → 不安全
r = client.post("/api/regions/batch", json=unsafe_batch, headers={"Idempotency-Key": KB3})
check("批量不安全 422 unsafe_trajectory", r.status_code == 422
      and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
safe_batch = dict(unsafe_batch, trajectories=[SAFE_TRAJ])
r = client.post("/api/regions/batch", json=safe_batch, headers={"Idempotency-Key": KB3})
check("422 不占键：换安全轨迹后同键成功 200", r.status_code == 200, r.text)

# ================================================================= 凭证保护仍然适用
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 300, "t": future(60)},
    {"x": 900, "y": 300, "t": future(120)},
]})
check("签发凭证 201", r.status_code == 201, r.text)
pass_id = r.json()["id"]

KP2 = "retry-pass-conflict"
conflict_region = {
    "name": "压住凭证轨迹",
    "polygon": [[0, 250], [100, 250], [100, 350], [0, 350]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
}
keys_before = key_count()
r = client.post("/api/regions", json=conflict_region, headers={"Idempotency-Key": KP2})
check("带键登记危及凭证 409 pass_conflict", r.status_code == 409
      and r.json()["error"]["code"] == "pass_conflict", r.text)
check("pass_conflict 不占键", key_count() == keys_before, f"keys={key_count()}")
r = client.post(f"/api/passes/{pass_id}/cancel")
check("取消凭证 200", r.status_code == 200, r.text)
r1 = client.post("/api/regions", json=conflict_region, headers={"Idempotency-Key": KP2})
check("取消后同键重试成功 201", r1.status_code == 201, r1.text)
r2 = client.post("/api/regions", json=conflict_region, headers={"Idempotency-Key": KP2})
check("成功后同键重试回放原响应", r2.status_code == 201 and r2.json() == r1.json(), r2.text)

# ================================================================= 并发同键：只有一次实际提交
KC = "retry-concurrent"
N = 8
barrier = threading.Barrier(N)
results: list = [None] * N
concurrent_payload = region_payload("并发区", 800)


def worker(i):
    barrier.wait(10)
    results[i] = client.post("/api/regions", json=concurrent_payload,
                             headers={"Idempotency-Key": KC})


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join(30)

codes = [r.status_code for r in results]
check("并发同键全部 201", all(c == 201 for c in codes), str(codes))
ids = {r.json()["region"]["id"] for r in results}
check("并发同键返回同一区域 id（只有一次实际提交）", len(ids) == 1, str(ids))
check("并发后该区域只登记了一次",
      len([x for x in client.get("/api/regions").json() if x["name"] == "并发区"]) == 1)

# 并发同键批量：同样只有一次实际提交
KB4 = "retry-concurrent-batch"
rev_before = revision()
c_batch = {
    "expected_revision": rev_before,
    "operations": [{"action": "create", "name": "并发批区",
                    "polygon": [[900, 0], [910, 0], [910, 10], [900, 10]],
                    "valid_from": WIDE[0], "valid_to": WIDE[1]}],
    "trajectories": [SAFE_TRAJ],
}
barrier2 = threading.Barrier(N)
bresults: list = [None] * N


def bworker(i):
    barrier2.wait(10)
    bresults[i] = client.post("/api/regions/batch", json=c_batch,
                              headers={"Idempotency-Key": KB4})


threads = [threading.Thread(target=bworker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join(30)

ok = [r for r in bresults if r.status_code == 200]
check("并发同键批量全部 200", len(ok) == N, str([r.status_code for r in bresults]))
check("并发批量返回同一响应", len({r.text for r in ok}) == 1)
check("并发批量 revision 只推进一次", revision() == rev_before + 1,
      f"before={rev_before} after={revision()}")
check("并发批量只建了一个区域",
      len([x for x in client.get("/api/regions").json() if x["name"] == "并发批区"]) == 1)

# ---- 并发同键不同内容：至多一个成功，其余 409，且只落库一次 ----
KC2 = "retry-concurrent-divergent"
barrier3 = threading.Barrier(N)
dresults: list = [None] * N


def dworker(i):
    barrier3.wait(10)
    dresults[i] = client.post("/api/regions", json=region_payload(f"分叉区-{i}", 1000 + i * 20),
                              headers={"Idempotency-Key": KC2})


threads = [threading.Thread(target=dworker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join(30)

n_ok = len([r for r in dresults if r.status_code == 201])
n_conflict = len([r for r in dresults if r.status_code == 409
                  and r.json()["error"]["code"] == "idempotency_key_conflict"])
check("并发同键不同内容：恰好 1 个 201", n_ok == 1, str([r.status_code for r in dresults]))
check("其余全部 409 idempotency_key_conflict", n_conflict == N - 1)
check("分叉内容只落库一个区域",
      len([x for x in client.get("/api/regions").json() if x["name"].startswith("分叉区")]) == 1)

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部幂等键 API 集成用例通过")
