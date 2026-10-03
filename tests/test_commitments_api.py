"""择时通行承诺 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_commitments_api.py
脚本会清空 regions/region_meta/commitments 后跑全套用例（含并发），
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


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def future(seconds: float) -> str:
    return iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def future_dt(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def at(h: int, m: int = 0, s: int = 0) -> datetime:
    """2030 年某日的固定 UTC 时刻（全部远在未来，不受墙钟流逝影响）。"""
    return datetime(2030, 6, 1, h, m, s, tzinfo=timezone.utc)


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
# 覆盖墙钟当前时刻的宽生效窗口（2020-2032）：对 now 附近的候选出发窗口，
# 其禁止出发区间 [vf-45s, vt-15s) 完全覆盖窗口
WIDE = ("2020-01-01T00:00:00Z", "2032-01-01T00:00:00Z")

# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s；
# 对 [0,100]x[0,100] 区域的几何接触为相对 [15s,45s]，
# 区域生效 [vf,vt) → 禁止出发区间 [vf-45s, vt-15s)
TRAJ = [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60},
]
# 从区域上方经过的行程（y=200），不受 SQUARE 区域影响
TRAJ_HIGH = [
    {"x": -50, "y": 200, "t": 0},
    {"x": 150, "y": 200, "t": 60},
]
HIGH_SQUARE = [[0, 150], [100, 150], [100, 250], [0, 250]]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE commitments RESTART IDENTITY")
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("TRUNCATE region_change_audit")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def commitment_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM commitments")
            return cur.fetchone()[0]


def create_region(name, polygon, vf, vt):
    return client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": iso(vf), "valid_to": iso(vt),
    })


reset_all()

# ====================================================================
# 1. 签发：空集合 → 全窗口安全
# ====================================================================
r = client.post("/api/commitments", json={
    "points": TRAJ,
    "window_start": iso(at(10, 0)), "window_end": iso(at(12, 0)),
})
check("空集合签发承诺 201", r.status_code == 201, r.text)
c0 = r.json()
check("返回 id/status=active/revision=0",
      c0["id"] == 1 and c0["status"] == "active" and c0["revision"] == 0, str(c0))
check("回显相对秒数轨迹（首点 t=0）",
      c0["trajectory"][0]["t"] == 0 and c0["trajectory"][1]["t"] == 60, str(c0))
check("全窗口安全：单段区间等于候选窗口（左闭右开）",
      len(c0["intervals"]) == 1
      and c0["intervals"][0]["start"] == c0["window_start"]
      and c0["intervals"][0]["end"] == c0["window_end"]
      and c0["intervals"][0]["start_inclusive"] is True
      and c0["intervals"][0]["end_inclusive"] is False, str(c0))
check("issued_at 存在且 cancelled_at 为空",
      bool(c0["issued_at"]) and c0["cancelled_at"] is None)
check("签发不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 0)
c0_id = c0["id"]

# ====================================================================
# 2. 签发：区域把窗口切成两段 / 完全覆盖 → 精确端点
# ====================================================================
# 区域 R1 生效 [08:00, 09:00)：禁止出发 [07:59:15, 08:59:45)
r = create_region("R1", SQUARE, at(8, 0), at(9, 0))
check("登记 R1 201（与 c0 窗口 10:00-12:00 不相交）", r.status_code == 201, r.text)
id_r1 = r.json()["region"]["id"]

# 窗口 [07:58, 09:01) 跨禁止区间 [07:59:15, 08:59:45) → 两段安全
r = client.post("/api/commitments", json={
    "points": TRAJ,
    "window_start": iso(at(7, 58)), "window_end": iso(at(9, 1)),
})
check("跨禁止区间签发 201", r.status_code == 201, r.text)
c1 = r.json()
ivs = c1["intervals"]
check("两段安全区间，端点精确（vf-b / vt-a，左闭右开）",
      len(ivs) == 2
      and ivs[0]["start"] == iso(at(7, 58))
      and ivs[0]["end"] == iso(at(7, 59, 15))
      and ivs[1]["start"] == iso(at(8, 59, 45))
      and ivs[1]["end"] == iso(at(9, 1))
      and all(i["start_inclusive"] and not i["end_inclusive"] for i in ivs),
      str(ivs))
c1_id = c1["id"]

# 窗口完全落入禁止区间 → 拒签 422 且不落库
before_n = commitment_count()
r = client.post("/api/commitments", json={
    "points": TRAJ,
    "window_start": iso(at(8, 0)), "window_end": iso(at(8, 30)),
})
check("窗口完全被禁止区间覆盖 → 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
check("拒签不落库", commitment_count() == before_n)

# ====================================================================
# 3. 查询：当前可用区间与 /api/schedule 一致；未知 ID 404
# ====================================================================
r = client.get(f"/api/commitments/{c1_id}")
check("查询承诺 200", r.status_code == 200, r.text)
check("查询区间与签发时一致（集合未变）", r.json()["intervals"] == ivs, str(r.json()))
rs = client.post("/api/schedule", json={
    "points": TRAJ,
    "window_start": c1["window_start"], "window_end": c1["window_end"],
})
check("GET 区间与 /api/schedule 一致",
      rs.json()["intervals"] == r.json()["intervals"]
      and rs.json()["revision"] == r.json()["revision"],
      f"{rs.json()['intervals']} vs {r.json()['intervals']}")

r = client.get("/api/commitments/9999")
check("未知承诺 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# ====================================================================
# 4. 非法输入
# ====================================================================
r = client.post("/api/commitments", json={
    "points": [{"x": 0, "y": 0, "t": 1}, {"x": 1, "y": 1, "t": 2}],
    "window_start": future(60), "window_end": future(120),
})
check("首点相对秒数非 0 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/commitments", json={
    "points": [{"x": 0, "y": 0, "t": 0}, {"x": 1, "y": 1, "t": 0}],
    "window_start": future(60), "window_end": future(120),
})
check("相对时刻不严格递增 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/commitments", json={
    "points": [{"x": 0, "y": 0, "t": 0}, {"x": 1, "y": 1, "t": 60}],
    "window_start": future(120), "window_end": future(60),
})
check("窗口倒序 → 422",
      r.status_code == 422 and r.json()["error"]["code"] in ("invalid_input", "validation_error"),
      r.text)

r = client.post("/api/commitments", json={
    "points": TRAJ,
    "window_start": "2030-06-01T10:00:00", "window_end": "2030-06-01T11:00:00",
})
check("窗口不带时区 → 422", r.status_code == 422, r.text)

r = client.post("/api/commitments", json={
    "points": TRAJ, "window_start": future(-10), "window_end": future(60),
})
check("窗口起点早于签发时刻 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/commitments", json={
    "points": TRAJ,
    "window_start": iso(at(10)), "window_end": iso(at(11)), "extra": 1,
})
check("多余字段 → 422", r.status_code == 422, r.text)
check("非法输入均不落库", commitment_count() == before_n)

# ====================================================================
# 5. 承诺约束区域变更：抹掉全部未来安全时刻 → 409 整笔回滚
# ====================================================================
# 活跃约束承诺：高位轨迹（R1 是 SQUARE 不接触 y=200），窗口 [13:00,14:00)
r = client.post("/api/commitments", json={
    "points": TRAJ_HIGH,
    "window_start": iso(at(13)), "window_end": iso(at(14)),
})
check("高位轨迹承诺签发 201（R1 不接触 y=200）", r.status_code == 201, r.text)
c2_id = r.json()["id"]
rev_before = client.get("/api/revision").json()["revision"]

# (a) 登记长期生效的 HIGH_SQUARE：禁止区间覆盖整个 [13:00,14:00)
r = create_region("R2-压高位", HIGH_SQUARE, at(0), at(23))
check("登记抹掉承诺全部安全时刻 → 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict",
      r.text)
check("409 明细列出冲突承诺 id",
      [d["commitment_id"] for d in r.json()["error"]["details"]] == [c2_id],
      str(r.json()["error"]["details"]))
check("409 后 revision 不变且区域未落库",
      client.get("/api/revision").json()["revision"] == rev_before
      and all(x["name"] != "R2-压高位" for x in client.get("/api/regions").json()))

# (b) 修订 R1 使其覆盖高位轨迹（长期生效）→ 409，version/revision 不变
r = client.patch(f"/api/regions/{id_r1}", json={
    "polygon": [[-50, 150], [200, 150], [200, 250], [-50, 250]],
    "valid_from": iso(at(0)), "valid_to": iso(at(23)),
})
check("修订抹掉承诺全部安全时刻 → 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict",
      r.text)
check("修订 409 后 R1 version 不变（=1）",
      client.get(f"/api/regions/{id_r1}").json()["version"] == 1)

# (c) 部分遮挡：禁止区间只覆盖窗口中段，两端仍有安全时刻 → 201
# HIGH_SQUARE 生效 [13:30, 13:31) → 禁止出发 [13:29:15, 13:30:45)
r = create_region("R3-中部短时", HIGH_SQUARE, at(13, 30), at(13, 31))
check("部分遮挡（窗口两端仍安全）→ 登记 201", r.status_code == 201, r.text)
id_r3 = r.json()["region"]["id"]
# 查询承诺应得到两段残余安全区间
r = client.get(f"/api/commitments/{c2_id}")
got = r.json()["intervals"]
check("部分遮挡后查询到两段残余区间 [13:00,13:29:15)/[13:30:45,14:00)",
      len(got) == 2
      and got[0]["start"] == iso(at(13))
      and got[0]["end"] == iso(at(13, 29, 15))
      and got[1]["start"] == iso(at(13, 30, 45))
      and got[1]["end"] == iso(at(14)), str(got))

# 再把剩余的安全时刻也抹掉（长期生效）→ 仍 409
r = create_region("R4-全压高位", HIGH_SQUARE, at(0), at(23))
check("追加区域彻底抹掉未来安全时刻 → 409",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict",
      r.text)

# (d) 批量变更抹掉安全时刻 → 409 commitment_conflict，整批回滚
r = client.post("/api/regions/batch", json={
    "expected_revision": client.get("/api/revision").json()["revision"],
    "operations": [
        {"action": "create", "name": "批-压高位",
         "polygon": HIGH_SQUARE,
         "valid_from": iso(at(0)), "valid_to": iso(at(23))},
    ],
    "trajectories": [[
        {"x": -50, "y": 800, "t": future(60)},
        {"x": 350, "y": 800, "t": future(120)},
    ]],
})
check("批量抹掉承诺安全时刻 → 409 commitment_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict",
      r.text)
check("批量 409 整批回滚（revision 不变）",
      client.get("/api/revision").json()["revision"] == rev_before + 1)

# (e) 修订只改名（集合实质安全集合不变）→ 200
r = client.patch(f"/api/regions/{id_r1}", json={"name": "R1-改名"})
check("无害改名修订 200", r.status_code == 200, r.text)

# (f) 删除约束区域不扩大危险集合 → 201/200
r = client.delete(f"/api/regions/{id_r3}")
check("删除区域 200（不可能收紧承诺安全集合）", r.status_code == 200, r.text)

# (g) 「变更前已无未来安全时刻」的承诺不再约束：
#     再签一张窗口落在 R1 禁止区间内的承诺是不可能的（签发即拒）；
#     该语义用动态场景验证：先在空窗口上签发，再登记区域，随后任何
#     进一步收紧都不再因它冲突 —— 见第 8 组短窗口实验的前置。
#     这里直接验证：c0 的窗口 [10:00,12:00) 当前受 R1 影响吗？
r = client.get(f"/api/commitments/{c0_id}")
# R1 禁止 [07:59:15, 08:59:45) 与 c0 窗口不相交 → c0 仍全程安全
check("c0 区间不受 R1 影响（仍单段全窗口）",
      len(r.json()["intervals"]) == 1
      and r.json()["intervals"][0]["start"] == c0["window_start"], str(r.json()))

# ====================================================================
# 6. 取消：幂等；未知 404；取消后不再约束
# ====================================================================
rev_cb = client.get("/api/revision").json()["revision"]
r = client.post(f"/api/commitments/{c2_id}/cancel")
check("取消承诺 200 status=cancelled intervals 为空",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["intervals"] == [] and r.json()["cancelled_at"] is not None, r.text)
cancel_body = r.json()
check("取消不推进区域 revision",
      client.get("/api/revision").json()["revision"] == rev_cb)
r = client.post(f"/api/commitments/{c2_id}/cancel")
check("重复取消幂等（结果不变）", r.status_code == 200 and r.json() == cancel_body, r.text)
r = client.post("/api/commitments/9999/cancel")
check("取消未知承诺 404",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)
r = client.get(f"/api/commitments/{c2_id}")
check("已取消承诺查询 intervals 为空", r.json()["intervals"] == [])

# 取消后原冲突登记放行
r = create_region("R2-压高位", HIGH_SQUARE, at(0), at(23))
check("取消后登记原冲突区域 201", r.status_code == 201, r.text)

# ====================================================================
# 7. 生效端点：接触恰在 valid_to 的出发安全（禁止区间右端 open）
# ====================================================================
# R1（08:00-09:00）禁止 [07:59:15, 08:59:45)；
# s=08:59:45 时接触右端点 τ=45 落在 s+45=09:00:00=valid_to（开端）→ 安全
r = client.post("/api/schedule", json={
    "points": TRAJ,
    "window_start": iso(at(8, 59, 44)), "window_end": iso(at(8, 59, 46)),
})
eps = r.json()["intervals"]
check("禁止区间右端 open：恰在 valid_to 接触的出发安全",
      len(eps) == 1
      and eps[0]["start"] == iso(at(8, 59, 45))
      and eps[0]["end"] == iso(at(8, 59, 46))
      and eps[0]["start_inclusive"] is True
      and eps[0]["end_inclusive"] is False, str(eps))
# s=07:59:15 时接触左端点 τ=15 落在 s+15=08:00:00=valid_from（闭端）→ 禁止
r = client.post("/api/schedule", json={
    "points": TRAJ,
    "window_start": iso(at(7, 59, 14)), "window_end": iso(at(7, 59, 16)),
})
eps = r.json()["intervals"]
check("禁止区间左端 closed：恰在 valid_from 接触的出发禁止",
      len(eps) == 1
      and eps[0]["start"] == iso(at(7, 59, 14))
      and eps[0]["end"] == iso(at(7, 59, 15)), str(eps))

# ====================================================================
# 8. 窗口结束的承诺不再约束（动态短窗口）
# ====================================================================
w_end_dt = future_dt(2.5)
r = client.post("/api/commitments", json={
    "points": TRAJ_HIGH,
    "window_start": future(1), "window_end": iso(w_end_dt),
})
check("签发短窗口承诺 201", r.status_code == 201, r.text)
short_id = r.json()["id"]
remaining = (w_end_dt - datetime.now(timezone.utc)).total_seconds()
time.sleep(max(0.0, remaining) + 0.5)
r = client.get(f"/api/commitments/{short_id}")
check("窗口结束后：status 仍 active 但 intervals 为空",
      r.json()["status"] == "active" and r.json()["intervals"] == [], r.text)
# 登记一个会完全覆盖该（已结束）窗口的区域：不应因 short_id 冲突
# （c0 的窗口 10:00-12:00 不受影响：轨迹相同但 R 为高位区、生效需避开 c0
#  —— c0 是 TRAJ 而非 TRAJ_HIGH，几何上根本不接触 HIGH_SQUARE，无干扰）
r = create_region("R5-短窗口后", HIGH_SQUARE, at(13, 0), at(14, 0))
# 若 409，明细里也绝不能含 short_id
if r.status_code == 409:
    ids = [d["commitment_id"] for d in r.json()["error"]["details"]]
    check("已结束窗口的承诺不约束（409 明细不含它）", short_id not in ids, str(ids))
else:
    check("已结束窗口的承诺不约束（登记 201）", r.status_code == 201, r.text)

# ====================================================================
# 9. 「变更前仍有、变更后没有」语义的动态直证：
#    承诺窗口在近未来，先安全；登记区域后 GET 区间收缩，再登记到全空 → 409
# ====================================================================
reset_all()
# 先签发一张近未来窗口的高位轨迹承诺（此时无区域，全窗口安全）
r = client.post("/api/commitments", json={
    "points": TRAJ_HIGH,
    "window_start": future(600), "window_end": future(660),
})
check("动态：空集合签发近未来承诺 201", r.status_code == 201, r.text)
dyn_id = r.json()["id"]
# 登记只遮挡窗口中部的区域 → 仍有安全时刻，201
# 要使禁止出发区间 [vf-45, vt-15) 覆盖窗口中部，需 vf = win_start+60、
# vt = win_start+61 → 禁止约 [win_start+15, win_start+46)，两端仍安全
dyn_vf = future_dt(660)
r = create_region("DYN-中", HIGH_SQUARE, dyn_vf, dyn_vf + timedelta(seconds=1))
check("动态：中部短时遮挡 201（两端仍安全）", r.status_code == 201, r.text)
r = client.get(f"/api/commitments/{dyn_id}")
check("动态：GET 区间已收缩为两段", len(r.json()["intervals"]) == 2, str(r.json()))
# 登记长期生效区域彻底覆盖 → 409 整笔回滚
rev_dyn = client.get("/api/revision").json()["revision"]
r = create_region("DYN-全压", HIGH_SQUARE,
                  datetime(2020, 1, 1, tzinfo=timezone.utc),
                  datetime(2032, 1, 1, tzinfo=timezone.utc))
check("动态：彻底抹除 → 409 commitment_conflict 整笔回滚",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_conflict"
      and [d["commitment_id"] for d in r.json()["error"]["details"]] == [dyn_id],
      r.text)
check("动态：409 后 revision 不变",
      client.get("/api/revision").json()["revision"] == rev_dyn)
check("动态：区间仍为两段（未落库）",
      len(client.get(f"/api/commitments/{dyn_id}").json()["intervals"]) == 2)

# ====================================================================
# 10. 并发：签发承诺与抹掉安全时刻的建区串行化，恰好一个成功
# ====================================================================
reset_all()
r = create_region("BASE", SQUARE, at(8), at(9))
check("基线区域 201", r.status_code == 201, r.text)
barrier = threading.Barrier(2)
outcome: dict = {}


def commit_worker():
    barrier.wait()
    rr = client.post("/api/commitments", json={
        "points": TRAJ_HIGH,
        "window_start": future(60), "window_end": future(120),
    })
    outcome["commit"] = (rr.status_code, rr.json())


def create_worker():
    barrier.wait()
    rr = create_region("CONC-压高位", HIGH_SQUARE,
                       datetime(2020, 1, 1, tzinfo=timezone.utc),
                       datetime(2032, 1, 1, tzinfo=timezone.utc))
    outcome["create"] = (rr.status_code, rr.json())


t1 = threading.Thread(target=commit_worker)
t2 = threading.Thread(target=create_worker)
t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
commit_ok = outcome["commit"][0] == 201
create_ok = outcome["create"][0] == 201
check("承诺签发与建区并发：恰好一个成功（无漏检窗口）",
      commit_ok != create_ok, str(outcome)[:400])
if commit_ok:
    check("承诺先成功 → 建区 409 commitment_conflict",
          outcome["create"][0] == 409
          and outcome["create"][1]["error"]["code"] == "commitment_conflict",
          str(outcome["create"])[:300])
else:
    check("建区先成功 → 承诺 422 unsafe_trajectory",
          outcome["commit"][0] == 422
          and outcome["commit"][1]["error"]["code"] == "unsafe_trajectory",
          str(outcome["commit"])[:300])

# ====================================================================
# 11. 并发混合：签发/取消/建区/修订同时发起，全部串行化，无 5xx
# ====================================================================
r = client.post("/api/commitments", json={
    "points": [
        {"x": -50, "y": 900, "t": 0},
        {"x": 150, "y": 900, "t": 60},
    ],
    "window_start": future(120), "window_end": future(300),
})
c_cancel_id = r.json()["id"]
base_rev = client.get("/api/revision").json()["revision"]
K = 8
barrier2 = threading.Barrier(K)
results: list[tuple] = []
rlock = threading.Lock()


def mixed_worker(i):
    barrier2.wait()
    if i < 2:  # 两个线程同时取消同一张承诺：幂等
        rr = client.post(f"/api/commitments/{c_cancel_id}/cancel")
    elif i < 5:  # 三张互不相同的安全高位承诺
        y = 700 + 20 * i
        rr = client.post("/api/commitments", json={
            "points": [{"x": -50, "y": y, "t": 0}, {"x": 150, "y": y, "t": 60}],
            "window_start": future(120), "window_end": future(300),
        })
    elif i < 7:  # 两个不压住任何承诺轨迹的新区域
        y = 950 + 50 * i
        rr = create_region(f"并发区-{i}",
                           [[0, y], [100, y], [100, y + 30], [0, y + 30]],
                           at(0), at(23))
    else:  # 一个无害改名
        regions = client.get("/api/regions").json()
        rr = client.patch(f"/api/regions/{regions[0]['id']}", json={"name": "并发改名"})
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
check("被并发取消的承诺最终为 cancelled",
      client.get(f"/api/commitments/{c_cancel_id}").json()["status"] == "cancelled")
final_rev = client.get("/api/revision").json()["revision"]
check("revision 恰好推进 3 次（2 建区 + 1 修订；签发/取消不推进）",
      final_rev == base_rev + 3, f"got {final_rev}, base {base_rev}")

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部择时通行承诺 API 集成用例通过")
