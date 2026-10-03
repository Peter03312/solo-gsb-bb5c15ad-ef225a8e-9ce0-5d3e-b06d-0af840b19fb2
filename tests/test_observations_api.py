"""安全状态观察 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_observations_api.py
脚本会清空 regions/region_meta/passes/commitments/observations/
observation_events/idempotency_keys 后跑全套用例，结束时再次清空，
避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
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


def events(obs_id, **params):
    r = client.get(f"/api/observations/{obs_id}/events", params=params or {})
    assert r.status_code == 200, r.text
    return r.json()


# 宽生效窗口，覆盖所有动态生成的未来轨迹时刻
WIDE = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")

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


def event_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM observation_events")
            return cur.fetchone()[0]


def observation_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM observations")
            return cur.fetchone()[0]


reset_all()

# ---- 无区域时登记观察：安全（空集合） ----
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 50, "t": future(60)},
    {"x": 350, "y": 50, "t": future(120)},
]})
check("无区域时登记观察 201", r.status_code == 201, r.text)
o0 = r.json()
check("观察初始结论 safe / revision=0",
      o0["conclusion"] == "safe" and o0["revision"] == 0 and o0["status"] == "active",
      str(o0))
check("登记不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 0)
o0_id = o0["id"]

# ---- 登记区域 A：[0,100]x[0,100]，长期生效（revision 1） ----
r = client.post("/api/regions", json={
    "name": "A区", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记 A 区 201 revision=1", r.status_code == 201 and r.json()["revision"] == 1, r.text)
id_a = r.json()["region"]["id"]

# 观察 o0 应已产生 safe→unsafe 翻转事件
ev = events(o0_id)
check("登记 A 后观察 o0 一条事件 safe→unsafe@1",
      len(ev["events"]) == 1 and ev["events"][0]["seq"] == 1
      and ev["events"][0]["old_conclusion"] == "safe"
      and ev["events"][0]["new_conclusion"] == "unsafe"
      and ev["events"][0]["revision"] == 1
      and ev["next_cursor"] is None, str(ev))
g = client.get(f"/api/observations/{o0_id}").json()
check("查询观察 o0 最近结论 unsafe/revision=1",
      g["conclusion"] == "unsafe" and g["revision"] == 1, str(g))

# ---- 安全观察：轨迹 y=200 从 A 上方经过（不触碰） ----
SAFE_TRAJ = [
    {"x": -50, "y": 200, "t": future(60)},
    {"x": 350, "y": 200, "t": future(120)},
]
r = client.post("/api/observations", json={"points": SAFE_TRAJ})
check("登记安全观察 201 conclusion=safe revision=1",
      r.status_code == 201 and r.json()["conclusion"] == "safe"
      and r.json()["revision"] == 1, r.text)
o_safe = r.json()
o_safe_id = o_safe["id"]
check("安全观察回显轨迹（UTC Z 时刻）",
      o_safe["trajectory"][0]["t"].endswith("Z") and o_safe["trajectory"][0]["x"] == -50,
      str(o_safe))
check("registered_at 存在且 cancelled_at 为空",
      bool(o_safe["registered_at"]) and o_safe["cancelled_at"] is None, str(o_safe))
check("安全观察初始无事件", events(o_safe_id)["events"] == [])
check("登记观察不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 1)

# ---- 不安全观察：y=50 直穿 A；不安全也可登记 ----
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 50, "t": future(60)},
    {"x": 350, "y": 50, "t": future(120)},
]})
check("不安全观察同样登记 201 conclusion=unsafe revision=1",
      r.status_code == 201 and r.json()["conclusion"] == "unsafe"
      and r.json()["revision"] == 1, r.text)
o_unsafe_id = r.json()["id"]
check("观察总数为 3", observation_count() == 3)

# ---- 非法输入：422，不落库 ----
r = client.post("/api/observations", json={"points": [
    {"x": 0, "y": 0, "t": future(60)},
]})
check("轨迹点不足 422", r.status_code == 422, r.text)
same_t = future(60)
r = client.post("/api/observations", json={"points": [
    {"x": 0, "y": 0, "t": same_t},
    {"x": 1, "y": 1, "t": same_t},
]})
check("时刻不严格递增 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/observations", json={"points": [
    {"x": 0, "y": 0, "t": "2026-10-01T00:00:00"},
    {"x": 1, "y": 1, "t": "2026-10-01T00:01:00"},
]})
check("时刻不带时区 422", r.status_code == 422, r.text)
r = client.post("/api/observations", json={"points": [
    {"x": 0, "y": 0, "t": future(-10)},
    {"x": 1, "y": 1, "t": future(60)},
]})
check("首点不晚于登记时刻 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/observations", json={"points": SAFE_TRAJ, "extra": 1})
check("多余字段 422", r.status_code == 422, r.text)
check("非法输入均不落库（仍 3 条）", observation_count() == 3)

# ---- 查询未知观察 / 取消未知观察：404 ----
r = client.get("/api/observations/9999")
check("查询未知观察 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)
r = client.post("/api/observations/9999/cancel")
check("取消未知观察 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)
r = client.get("/api/observations/9999/events")
check("未知观察事件 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# ---- 结论不变的区域变更不产生事件：建一个碰不到任何观察轨迹的小区域 ----
r = client.post("/api/regions", json={
    "name": "偏远区", "polygon": [[900, 900], [910, 900], [910, 910], [900, 910]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("偏远区登记 201 revision=2",
      r.status_code == 201 and r.json()["revision"] == 2, r.text)
check("结论不变：o0/o_safe 事件数不增加",
      len(events(o0_id)["events"]) == 1 and events(o_safe_id)["events"] == [])

# ---- 单区修订压住安全观察 o_safe → 翻转 unsafe，观察不阻止变更（200） ----
r = client.patch(f"/api/regions/{id_a}", json={
    "polygon": [[0, 0], [400, 0], [400, 300], [0, 300]],
})
check("修订 A 覆盖安全观察 200（观察不阻止变更）",
      r.status_code == 200 and r.json()["revision"] == 3, r.text)
ev_safe = events(o_safe_id)
check("o_safe 一条事件 safe→unsafe@3",
      len(ev_safe["events"]) == 1
      and ev_safe["events"][0]["old_conclusion"] == "safe"
      and ev_safe["events"][0]["new_conclusion"] == "unsafe"
      and ev_safe["events"][0]["revision"] == 3, str(ev_safe))
# o0（原 unsafe）与 o_unsafe（unsafe）结论仍 unsafe，不新增事件
check("o0 仍只有 1 条事件", len(events(o0_id)["events"]) == 1)
check("o_unsafe 无事件", events(o_unsafe_id)["events"] == [])

# ---- 单区删除让 o0/o_safe/o_unsafe 全部变 safe：一次变更各一条翻转事件 ----
rev_before = client.get("/api/revision").json()["revision"]
n_events_before = event_count()
r = client.delete(f"/api/regions/{id_a}")
check("删除 A 200 revision 推进",
      r.status_code == 200 and r.json()["revision"] == rev_before + 1, r.text)
new_rev = rev_before + 1
for oid, old in ((o0_id, "unsafe"), (o_safe_id, "unsafe"), (o_unsafe_id, "unsafe")):
    evs = events(oid)["events"]
    last = evs[-1]
    check(f"观察 {oid} 删除后追加 {old}→safe@{new_rev} 且此前事件保留",
          last["old_conclusion"] == old and last["new_conclusion"] == "safe"
          and last["revision"] == new_rev, str(evs))
    g = client.get(f"/api/observations/{oid}").json()
    check(f"观察 {oid} 查询最近结论 safe/revision={new_rev}",
          g["conclusion"] == "safe" and g["revision"] == new_rev, str(g))
check("删除 A 恰好新增 3 条事件", event_count() == n_events_before + 3)

# ---- 批量变更：同批 create+delete 净效果不安全，同一观察整批至多一条事件 ----
# 先恢复 A（revision 继续推进），让三条观察回到 unsafe
r = client.post("/api/regions", json={
    "name": "A区", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
rev_a = r.json()["revision"]
id_a = r.json()["region"]["id"]
o0_before_batch = len(events(o0_id)["events"])
o_safe_before_batch = len(events(o_safe_id)["events"])

r = client.post("/api/regions/batch", json={
    "expected_revision": rev_a,
    "operations": [
        # 先删 A（中间态变 safe）
        {"action": "delete", "id": id_a},
        # 同批再建一个覆盖 y=50/y=200 的大区域（最终 unsafe）
        {"action": "create", "name": "批内大区",
         "polygon": [[0, 0], [400, 0], [400, 300], [0, 300]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 350, "y": 900, "t": future(120)},
    ]],
})
check("批量净效果 unsafe 提交成功 200", r.status_code == 200, r.text)
rev_batch = r.json()["revision"]
# 比较的是变更前 vs 变更后：o0（y=50）净效果 unsafe→unsafe 不翻转；
# o_safe（y=200）在小 A 下本是 safe，大区下变 unsafe，整批恰好一条事件
# （而不是按批内中间态产生多条）
check("整批对同一观察至多一条事件",
      len(events(o0_id)["events"]) == o0_before_batch
      and len(events(o_safe_id)["events"]) == o_safe_before_batch + 1
      and client.get(f"/api/observations/{o0_id}").json()["conclusion"] == "unsafe"
      and client.get(f"/api/observations/{o_safe_id}").json()["conclusion"] == "unsafe",
      f"rev={rev_batch}")
o_safe_batch_last = events(o_safe_id)["events"][-1]
check("o_safe 批量事件为 safe→unsafe@rev_batch 且仅此一条",
      o_safe_batch_last["old_conclusion"] == "safe"
      and o_safe_batch_last["new_conclusion"] == "unsafe"
      and o_safe_batch_last["revision"] == rev_batch
      and all(e["revision"] != rev_batch for e in events(o_safe_id)["events"][:-1]),
      str(events(o_safe_id)["events"]))

# 净效果翻转（unsafe→safe）的批量：同批「建更宽大区 + 删现有大区」，
# 中间态一度变化，但最终安全——整批恰好一条事件而非多条
id_big = [x for x in client.get("/api/regions").json()
          if x["name"] == "批内大区"][0]["id"]
n_events_before = event_count()
o0_events_mid = len(events(o0_id)["events"])
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_batch,
    "operations": [
        # 先建一个更宽的大区（中间态：仍然 unsafe）
        {"action": "create", "name": "批内更宽区",
         "polygon": [[-100, -100], [500, -100], [500, 400], [-100, 400]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
        # 再删掉原有大区；本批后只剩更宽区（仍 unsafe，净效果不翻转）
        {"action": "delete", "id": id_big},
    ],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 350, "y": 900, "t": future(120)},
    ]],
})
check("批量（建更宽+删旧，净仍 unsafe）200", r.status_code == 200, r.text)
check("净效果不翻转：不新增事件",
      event_count() == n_events_before
      and len(events(o0_id)["events"]) == o0_events_mid)

# 真正净安全的批量：同批删掉更宽区并在远处建一个无关小区域
id_wide = [x for x in client.get("/api/regions").json()
           if x["name"] == "批内更宽区"][0]["id"]
rev_w = client.get("/api/revision").json()["revision"]
n_events_before = event_count()
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_w,
    "operations": [
        {"action": "delete", "id": id_wide},
        {"action": "create", "name": "远处小区域",
         "polygon": [[900, 900], [910, 900], [910, 910], [900, 910]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 350, "y": 900, "t": future(120)},
    ]],
})
check("批量净效果 unsafe→safe 200", r.status_code == 200, r.text)
rev_safe_batch = r.json()["revision"]
# 三张活跃观察各恰一条 unsafe→safe 事件（整批至多一条/观察）
check("净翻转批量恰好新增 3 条事件",
      event_count() == n_events_before + 3)
for oid in (o0_id, o_safe_id, o_unsafe_id):
    last = events(oid)["events"][-1]
    check(f"观察 {oid} 净翻转 unsafe→safe@{rev_safe_batch}",
          last["old_conclusion"] == "unsafe"
          and last["new_conclusion"] == "safe"
          and last["revision"] == rev_safe_batch,
          str(events(oid)["events"][-2:]))

# ---- 失败的区域变更不产生事件 ----
# 恢复一个大区，再制造会因通行凭证保护而回滚的登记
r = client.post("/api/regions", json={
    "name": "临时大区", "polygon": [[0, 0], [400, 0], [400, 300], [0, 300]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
rev_tmp = r.json()["revision"]
id_tmp = r.json()["region"]["id"]
# 签发一张 y=500 的安全凭证
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 500, "t": future(60)},
    {"x": 350, "y": 500, "t": future(120)},
]})
check("签发凭证 201", r.status_code == 201, r.text)
pass_id = r.json()["id"]
n_events_before = event_count()
r = client.post("/api/regions", json={
    "name": "挡路区", "polygon": [[0, 450], [400, 450], [400, 550], [0, 550]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("凭证保护：挡路区登记 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
check("409 回滚：不产生事件、revision 不变",
      event_count() == n_events_before
      and client.get("/api/revision").json()["revision"] == rev_tmp)

# 批量内轨迹不安全 → 422 整批回滚，不产生事件
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_tmp,
    "operations": [
        {"action": "delete", "id": id_tmp},
        {"action": "create", "name": "再来",
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
check("422 回滚：不产生事件、revision 不变",
      event_count() == n_events_before
      and client.get("/api/revision").json()["revision"] == rev_tmp)

# 批量 revision 冲突 → 409，不产生事件
r = client.post("/api/regions/batch", json={
    "expected_revision": 99999,
    "operations": [{"action": "update", "id": id_tmp, "name": "x"}],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 350, "y": 900, "t": future(120)},
    ]],
})
check("批量 revision 冲突 409",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)
check("revision 冲突回滚：不产生事件",
      event_count() == n_events_before
      and client.get("/api/revision").json()["revision"] == rev_tmp)

# ---- 幂等回放不重复产生事件 ----
# 删除临时大区（成功，三观察 safe→unsafe 各一条）
r = client.delete(f"/api/regions/{id_tmp}",
                  headers={"Idempotency-Key": "obs-del-tmp-0001"})
check("带键删除 200", r.status_code == 200, r.text)
rev_after_del = r.json()["revision"]
n_after_del = event_count()
r2 = client.delete(f"/api/regions/{id_tmp}",
                   headers={"Idempotency-Key": "obs-del-tmp-0001"})
check("同键重试回放原 200（非 404）", r2.status_code == 200 and r2.json() == r.json(), r2.text)
check("幂等回放不重复产生事件、不重复推进 revision",
      event_count() == n_after_del
      and client.get("/api/revision").json()["revision"] == rev_after_del)

# 批量幂等回放
r = client.post("/api/regions", json={
    "name": "幂等批目标", "polygon": [[0, 0], [400, 0], [400, 300], [0, 300]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
id_target = r.json()["region"]["id"]
rev_before_batch = client.get("/api/revision").json()["revision"]
n_before_batch = event_count()
body = {
    "expected_revision": rev_before_batch,
    "operations": [{"action": "delete", "id": id_target}],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 350, "y": 900, "t": future(120)},
    ]],
}
r = client.post("/api/regions/batch", json=body,
                headers={"Idempotency-Key": "obs-batch-del-0001"})
check("带键批量删除 200", r.status_code == 200, r.text)
first_body = r.json()
n_after_batch = event_count()
r2 = client.post("/api/regions/batch", json=body,
                 headers={"Idempotency-Key": "obs-batch-del-0001"})
check("批量同键重试回放原响应（逐字节一致）",
      r2.status_code == 200 and r2.json() == first_body, r2.text)
check("批量幂等回放不重复产生事件",
      event_count() == n_after_batch,
      f"{n_before_batch} -> {n_after_batch} / {event_count()}")

# ---- 取消：幂等；已取消观察不再产生事件 ----
rev_before = client.get("/api/revision").json()["revision"]
r = client.post(f"/api/observations/{o0_id}/cancel")
check("取消观察 200 cancelled",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["cancelled_at"] is not None, r.text)
cancel_body = r.json()
check("取消不推进区域 revision",
      client.get("/api/revision").json()["revision"] == rev_before)
r = client.post(f"/api/observations/{o0_id}/cancel")
check("重复取消幂等（结果不变）", r.status_code == 200 and r.json() == cancel_body, r.text)
# 取消后：建大区只影响仍 active 的 o_safe/o_unsafe，不影响 o0
n_events_before = event_count()
o0_events_before = len(events(o0_id)["events"])
r = client.post("/api/regions", json={
    "name": "取消后大区", "polygon": [[0, 0], [400, 0], [400, 300], [0, 300]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("取消后建大区 201", r.status_code == 201, r.text)
check("已取消观察 o0 不再产生事件",
      len(events(o0_id)["events"]) == o0_events_before,
      f"{o0_events_before} -> {len(events(o0_id)['events'])}")
# 取消不改变已记录的最近结论
g = client.get(f"/api/observations/{o0_id}").json()
check("已取消观察仍可查询（状态 cancelled、最近结论保留）",
      g["status"] == "cancelled" and g["conclusion"] in ("safe", "unsafe"), str(g))
id_big2 = r.json()["region"]["id"]
check("仍 active 的观察照常产生翻转事件",
      event_count() == n_events_before + 2)

# ---- 末点时刻已过的观察不再产生事件 ----
short_start = datetime.now(timezone.utc) + timedelta(seconds=2)
short_end = datetime.now(timezone.utc) + timedelta(seconds=3.5)
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 50, "t": iso_z(short_start)},
    {"x": 350, "y": 50, "t": iso_z(short_end)},
]})
check("登记短寿命观察 201（当前 unsafe）",
      r.status_code == 201 and r.json()["conclusion"] == "unsafe", r.text)
o_short_id = r.json()["id"]
# 删除大区：短寿命观察若未过期也会翻转；等待末点过后再删
remaining = (short_end - datetime.now(timezone.utc)).total_seconds()
time.sleep(max(0.0, remaining) + 0.5)
n_before = event_count()
r = client.delete(f"/api/regions/{id_big2}")
check("过期后删除大区 200", r.status_code == 200, r.text)
short_evs = events(o_short_id)["events"]
check("过期观察不产生事件（初始无事件，删除后仍无）",
      short_evs == [], str(short_evs))

# ---- 事件分页：游标升序、limit、next_cursor ----
# 为 o_safe 造足够多事件：反复建/删覆盖其轨迹的区域
for i in range(5):
    rr = client.post("/api/regions", json={
        "name": f"翻页区-{i}",
        "polygon": [[0, 150], [300, 150], [300, 250], [0, 250]],
        "valid_from": WIDE[0], "valid_to": WIDE[1],
    })
    rid = rr.json()["region"]["id"]
    client.delete(f"/api/regions/{rid}")
all_ev = events(o_safe_id, limit=500)["events"]
seqs = [e["seq"] for e in all_ev]
check("事件 seq 从 1 连续递增",
      seqs == list(range(1, len(seqs) + 1)), str(seqs[:20]))
check("事件按升序且结论必翻转",
      all(e["new_conclusion"] != e["old_conclusion"] for e in all_ev)
      and all(e["revision"] >= 1 for e in all_ev))

# 首页 limit=2
p1 = events(o_safe_id, limit=2)
check("首页 limit=2 返回 2 条，next_cursor=2",
      [e["seq"] for e in p1["events"]] == [1, 2] and p1["next_cursor"] == 2,
      str(p1))
p2 = events(o_safe_id, cursor=p1["next_cursor"], limit=2)
check("第二页 cursor=2 返回 seq 3,4，游标衔接",
      [e["seq"] for e in p2["events"]] == [3, 4], str(p2))
# 翻到末页
pages = []
cur_seq = 0
while True:
    pg = events(o_safe_id, cursor=cur_seq, limit=3)
    pages.extend(pg["events"])
    if pg["next_cursor"] is None:
        break
    cur_seq = pg["next_cursor"]
check("分页遍历事件全集、无重复无遗漏、末页 next_cursor=null",
      [e["seq"] for e in pages] == seqs, str([e["seq"] for e in pages]))
# cursor 超过最大 seq：空页且 next_cursor=null
tail = events(o_safe_id, cursor=999999)
check("越界游标返回空页 next_cursor=null",
      tail["events"] == [] and tail["next_cursor"] is None, str(tail))
# 无事件的观察返回空页（不是 404）
# 新建一个无区域覆盖且之后不再变更的场景：先清空所有区域
for x in client.get("/api/regions").json():
    client.delete(f"/api/regions/{x['id']}")
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 200, "t": future(120)},
    {"x": 350, "y": 200, "t": future(180)},
]})
o_quiet_id = r.json()["id"]
check("空集合下观察 safe", r.json()["conclusion"] == "safe", r.text)
quiet = events(o_quiet_id)
check("从未翻转的观察事件为空白页（非 404）",
      quiet["events"] == [] and quiet["next_cursor"] is None, str(quiet))

# 分页参数非法 → 422
r = client.get(f"/api/observations/{o_safe_id}/events", params={"cursor": -1})
check("cursor<0 返回 422", r.status_code == 422, r.text)
r = client.get(f"/api/observations/{o_safe_id}/events", params={"limit": 0})
check("limit=0 返回 422", r.status_code == 422, r.text)
r = client.get(f"/api/observations/{o_safe_id}/events", params={"limit": 501})
check("limit>500 返回 422", r.status_code == 422, r.text)

# ---- 观察不改变通行凭证/择时承诺的保护 ----
# 当前无区域：为 y=50 签发凭证，再建压住该凭证的区域 → 仍 409
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 50, "t": future(60)},
    {"x": 350, "y": 50, "t": future(120)},
]})
check("无区域时签发凭证 201", r.status_code == 201, r.text)
pass2 = r.json()["id"]
n_before = event_count()
r = client.post("/api/regions", json={
    "name": "凭证挡路", "polygon": [[0, 0], [400, 0], [400, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("观察存在时凭证保护依旧：409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict"
      and [d["pass_id"] for d in r.json()["error"]["details"]] == [pass2], r.text)
check("409 回滚观察事件不增加", event_count() == n_before)
# 承诺保护同样有效
r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 700, "t": 0}, {"x": 350, "y": 700, "t": 60}],
    "window_start": future(120), "window_end": future(600),
})
check("签发承诺 201", r.status_code == 201, r.text)
cid = r.json()["id"]
# 覆盖 y=700 的长期大区会抹掉窗口内全部安全时刻 → 409 commitment_conflict
r = client.post("/api/regions", json={
    "name": "承诺挡路", "polygon": [[-100, 650], [400, 650], [400, 750], [-100, 750]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("观察存在时承诺保护依旧：409 commitment_conflict",
      r.status_code == 409
      and r.json()["error"]["code"] == "commitment_conflict"
      and [d["commitment_id"] for d in r.json()["error"]["details"]] == [cid],
      r.text)

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部安全状态观察 API 集成用例通过")
