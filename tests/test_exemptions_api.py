"""最小区域豁免预演 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_exemptions_api.py
脚本会清空 regions/region_meta 等业务表后跑全套用例，结束时再次清空，
避免影响其它测试脚本。
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


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def at(day: int, h: int, m: int = 0, s: int = 0) -> datetime:
    """2030 年 6 月某日的固定 UTC 时刻（全部远在未来，不受墙钟流逝影响）。"""
    return datetime(2030, 6, day, h, m, s, tzinfo=timezone.utc)


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s；
# 对 [0,100]x[0,100] 区域的几何接触为相对 [15s,45s]，
# 区域生效 [vf,vt) → 禁止出发区间 [vf-45s, vt-15s)
TRAJ = [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60},
]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE passes, commitments, observations, observation_events"
                " RESTART IDENTITY"
            )
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("TRUNCATE region_change_audit")
            cur.execute("TRUNCATE idempotency_keys")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def table_count(table: str) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return cur.fetchone()[0]


def create_region(name, polygon, vf, vt):
    return client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": iso(vf), "valid_to": iso(vt),
    })


def preview(points, ws, we, candidates, revision):
    return client.post("/api/exemptions/preview", json={
        "points": points,
        "window_start": iso(ws), "window_end": iso(we),
        "candidate_region_ids": candidates,
        "expected_revision": revision,
    })


reset_all()

# 窗口 W2：2030-06-02 10:00~11:00；窗口 W1：2030-06-01 10:00~11:00
W2S, W2E = at(2, 10), at(2, 11)
W1S, W1E = at(1, 10), at(1, 11)

# ====================================================================
# 1. 空集合：原本安全，豁免集为空，区间为整个窗口
# ====================================================================
r = preview(TRAJ, W2S, W2E, [], 0)
check("空集合预演 200", r.status_code == 200, r.text)
p = r.json()
check("空集合：feasible，豁免集为空，最早=窗口起点，单段区间=整个窗口",
      p["feasible"] is True
      and p["exempted_region_ids"] == []
      and p["earliest_departure"] == iso(W2S)
      and len(p["intervals"]) == 1
      and p["intervals"][0]["start"] == iso(W2S)
      and p["intervals"][0]["end"] == iso(W2E)
      and p["intervals"][0]["start_inclusive"] is True
      and p["intervals"][0]["end_inclusive"] is False, str(p))
check("空集合：所据 revision=0，窗口回显为 UTC",
      p["revision"] == 0 and p["window_start"] == iso(W2S)
      and p["window_end"] == iso(W2E), str(p))

# ====================================================================
# 2. 登记前半区（禁 W2 [10:00,10:30)）：原本安全，豁免集为空
# ====================================================================
r = create_region("前半区", SQUARE, at(2, 10, 0, 45), at(2, 10, 30, 15))
check("登记前半区 201", r.status_code == 201, r.text)
LO = r.json()["region"]["id"]
check("前半区 revision=1", r.json()["revision"] == 1, r.text)

r = preview(TRAJ, W2S, W2E, [LO], 1)
p = r.json()
check("前半区：原本安全，豁免集为空，最早=10:30（后半段起点）",
      p["feasible"] is True
      and p["exempted_region_ids"] == []
      and p["earliest_departure"] == iso(at(2, 10, 30))
      and len(p["intervals"]) == 1
      and p["intervals"][0]["start"] == iso(at(2, 10, 30))
      and p["intervals"][0]["end"] == iso(W2E), str(p))

# ====================================================================
# 3. 登记后半区（禁 W2 [10:30,11:00)）：两半共同覆盖窗口
#    豁免任一即可；取最早安全时刻更早者（豁免前半区 → 前半安全）
# ====================================================================
r = create_region("后半区", SQUARE, at(2, 10, 30, 45), at(2, 11, 0, 15))
check("登记后半区 201", r.status_code == 201, r.text)
HI = r.json()["region"]["id"]

r = preview(TRAJ, W2S, W2E, [LO, HI], 2)
p = r.json()
check("两半区：豁免数最少为 1，取最早更早的方案（豁免前半区）",
      p["feasible"] is True
      and p["exempted_region_ids"] == [LO]
      and p["earliest_departure"] == iso(W2S)
      and len(p["intervals"]) == 1
      and p["intervals"][0]["start"] == iso(W2S)
      and p["intervals"][0]["end"] == iso(at(2, 10, 30)), str(p))

r = preview(TRAJ, W2S, W2E, [HI, LO], 2)
check("候选顺序不影响最优方案", r.json()["exempted_region_ids"] == [LO], r.text)

r = preview(TRAJ, W2S, W2E, [HI], 2)
p = r.json()
check("只豁免后半区：前半被禁，安全区间为后半 [10:30,11:00)",
      p["feasible"] is True
      and p["exempted_region_ids"] == [HI]
      and p["earliest_departure"] == iso(at(2, 10, 30))
      and len(p["intervals"]) == 1
      and p["intervals"][0]["start"] == iso(at(2, 10, 30))
      and p["intervals"][0]["end"] == iso(W2E), str(p))

# ====================================================================
# 4. 登记全覆盖区（2020~2032 生效）：任何窗口都被完全禁止
#    W2 需豁免 2 个：{全覆盖, 前半}（最早 10:00）优于 {全覆盖, 后半}
# ====================================================================
r = create_region("全覆盖区", SQUARE,
                  datetime(2020, 1, 1, tzinfo=timezone.utc),
                  datetime(2032, 1, 1, tzinfo=timezone.utc))
check("登记全覆盖区 201", r.status_code == 201, r.text)
FULL = r.json()["region"]["id"]
check("全覆盖区 revision=3", r.json()["revision"] == 3, r.text)

r = preview(TRAJ, W2S, W2E, [LO, HI, FULL], 3)
p = r.json()
check("全覆盖+两半：豁免数最少为 2，取最早更早的 {前半, 全覆盖}",
      p["feasible"] is True
      and p["exempted_region_ids"] == [LO, FULL]
      and p["earliest_departure"] == iso(W2S)
      and len(p["intervals"]) == 1
      and p["intervals"][0]["start"] == iso(W2S)
      and p["intervals"][0]["end"] == iso(at(2, 10, 30)), str(p))

r = preview(TRAJ, W2S, W2E, [FULL, LO, HI], 3)
check("候选乱序：豁免编号仍升序输出且方案不变",
      r.json()["exempted_region_ids"] == [LO, FULL], r.text)

# 全覆盖区不可豁免 → 不可行
r = preview(TRAJ, W2S, W2E, [LO, HI], 3)
p = r.json()
check("全覆盖区不可豁免：feasible=false，豁免集/最早时刻为 null，区间为空",
      p["feasible"] is False
      and p["exempted_region_ids"] is None
      and p["earliest_departure"] is None
      and p["intervals"] == []
      and p["revision"] == 3, str(p))

# W1 只被全覆盖区禁止：豁免它即全窗口安全
r = preview(TRAJ, W1S, W1E, [FULL], 3)
p = r.json()
check("W1 豁免全覆盖区：单段区间=整个窗口",
      p["feasible"] is True
      and p["exempted_region_ids"] == [FULL]
      and p["earliest_departure"] == iso(W1S)
      and len(p["intervals"]) == 1
      and p["intervals"][0]["start"] == iso(W1S)
      and p["intervals"][0]["end"] == iso(W1E), str(p))

# W1 候选为空 → 不可行
r = preview(TRAJ, W1S, W1E, [], 3)
check("W1 候选为空：不可行",
      r.json()["feasible"] is False
      and r.json()["exempted_region_ids"] is None, r.text)

# ====================================================================
# 5. 拒绝与冲突：重复编号 422、未知编号 404、revision 过期 409、
#    轨迹/窗口非法 422
# ====================================================================
r = preview(TRAJ, W2S, W2E, [LO, LO], 3)
check("候选编号重复 → 422 duplicate_region",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region",
      r.text)

r = preview(TRAJ, W2S, W2E, [999999], 3)
check("候选编号未知 → 404 region_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "region_not_found",
      r.text)

r = preview(TRAJ, W2S, W2E, [LO], 1)
check("revision 过期 → 409 revision_conflict 且带当前值",
      r.status_code == 409
      and r.json()["error"]["code"] == "revision_conflict"
      and r.json()["error"]["details"][0]["current_revision"] == 3, r.text)

r = preview([{"x": -50, "y": 50, "t": 1}, {"x": 150, "y": 50, "t": 60}],
            W2S, W2E, [LO], 3)
check("首点相对秒数非 0 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory",
      r.text)

r = preview(TRAJ, W2E, W2S, [LO], 3)
check("窗口倒序 → 422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input",
      r.text)

r = client.post("/api/exemptions/preview", json={
    "points": TRAJ,
    "window_start": "2030-06-02T10:00:00",  # 不带时区
    "window_end": iso(W2E),
    "candidate_region_ids": [LO],
    "expected_revision": 3,
})
check("窗口不带时区 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error",
      r.text)

r = preview([{"x": -50, "y": 50, "t": 0}], W2S, W2E, [LO], 3)
check("轨迹点少于 2 个 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error",
      r.text)

r = preview(TRAJ, W2S, W2E, [0], 3)
check("候选编号非正整数 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error",
      r.text)

r = client.post("/api/exemptions/preview", json={
    "points": TRAJ,
    "window_start": iso(W2S), "window_end": iso(W2E),
    "expected_revision": 3,
})
check("缺少候选编号字段 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error",
      r.text)

# ====================================================================
# 6. 预演无副作用：revision、区域、审计、凭证/承诺/观察均不变；
#    既有核验与调度结果不受影响
# ====================================================================
r = client.get("/api/revision")
check("多轮预演后 revision 仍为 3", r.json()["revision"] == 3, r.text)

r = client.get("/api/regions")
regions = r.json()
check("区域集合不变（3 个区域，version 均为 1）",
      len(regions) == 3 and all(x["version"] == 1 for x in regions), str(regions))

check("审计记录不变（恰 3 条登记记录）", table_count("region_change_audit") == 3)
check("无凭证/承诺/观察/事件被创建",
      table_count("passes") == 0 and table_count("commitments") == 0
      and table_count("observations") == 0 and table_count("observation_events") == 0)
check("幂等键表无占用", table_count("idempotency_keys") == 0)

r = client.post("/api/schedule", json={
    "points": TRAJ, "window_start": iso(W2S), "window_end": iso(W2E),
})
check("既有调度不受影响：全覆盖区下 W2 无安全区间",
      r.status_code == 200 and r.json()["intervals"] == [] and r.json()["revision"] == 3,
      r.text)

r = client.post("/api/verify", json={
    "points": [
        {"x": -50, "y": 50, "t": iso(W2S)},
        {"x": 150, "y": 50, "t": iso(at(2, 10, 1))},
    ],
})
check("既有核验不受影响：直穿正方形 → unsafe",
      r.status_code == 200 and r.json()["safe"] is False, r.text)

# ====================================================================
# 7. 区域变更后需用新 revision 预演（快照一致性契约）
# ====================================================================
r = client.delete(f"/api/regions/{FULL}")
check("删除全覆盖区 200", r.status_code == 200 and r.json()["revision"] == 4, r.text)

r = preview(TRAJ, W2S, W2E, [LO, HI], 3)
check("删除后用旧 revision 预演 → 409",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict",
      r.text)

r = preview(TRAJ, W2S, W2E, [LO, HI], 4)
check("删除后用新 revision 预演：回到豁免 1 个（前半区）",
      r.status_code == 200
      and r.json()["exempted_region_ids"] == [LO]
      and r.json()["revision"] == 4, r.text)

reset_all()
print()
if failures:
    print(f"失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("全部豁免预演 API 集成用例通过")
