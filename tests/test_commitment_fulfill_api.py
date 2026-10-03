"""承诺兑现（POST /api/commitments/{id}/fulfill）API 集成测试，
需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict:restrict@localhost:5432/restrict \
        python3 tests/test_commitment_fulfill_api.py
脚本会清空全部业务表后跑全套用例（含并发），结束时再次清空。
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


def at(h: int, m: int = 0, s: int = 0) -> datetime:
    """2030 年某日的固定 UTC 时刻（全部远在未来，不受墙钟流逝影响）。"""
    return datetime(2030, 6, 1, h, m, s, tzinfo=timezone.utc)


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s；
# 对 SQUARE 的几何接触为相对 [15s,45s]；
# 区域生效 [vf,vt) → 禁止出发区间 [vf-45s, vt-15s)
TRAJ = [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60},
]
HIGH_SQUARE = [[0, 150], [100, 150], [100, 250], [0, 250]]
TRAJ_HIGH = [
    {"x": -50, "y": 200, "t": 0},
    {"x": 150, "y": 200, "t": 60},
]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE observation_events, observations, passes, "
                "commitments, regions, region_change_audit, idempotency_keys "
                "RESTART IDENTITY CASCADE"
            )
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def count(table: str) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            return cur.fetchone()[0]


def create_region(name, polygon, vf, vt):
    return client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": iso(vf), "valid_to": iso(vt),
    })


def fulfill(cid, dep_str):
    return client.post(f"/api/commitments/{cid}/fulfill",
                       json={"departure_time": dep_str})


def issue(traj, ws, we):
    return client.post("/api/commitments", json={
        "points": traj, "window_start": ws, "window_end": we,
    })


reset_all()

# ====================================================================
# 1. 成功兑现：安全出发时刻 → 201，只生成一张凭证；revision 不变
# ====================================================================
r = create_region("R1", SQUARE, at(8, 0), at(9, 0))
check("登记 R1 201", r.status_code == 201, r.text)
rev = client.get("/api/revision").json()["revision"]

# 窗口 [07:58, 09:01)：禁止 [07:59:15, 08:59:45)，两端安全
r = issue(TRAJ, iso(at(7, 58)), iso(at(9, 1)))
check("签发承诺 201，两段安全区间",
      r.status_code == 201 and len(r.json()["intervals"]) == 2, r.text)
cid = r.json()["id"]
n_passes_before = count("passes")
n_audit_before = count("region_change_audit")

dep = at(7, 58)  # 恰为窗口起点（闭端）→ 安全
r = fulfill(cid, iso(dep))
check("安全时刻兑现 → 201", r.status_code == 201, r.text)
body = r.json()
check("响应含 commitment_id / revision / pass",
      body["commitment_id"] == cid and body["revision"] == rev
      and set(body) == {"commitment_id", "revision", "pass"}, str(body))
p = body["pass"]
check("凭证 status=active 且 revision=当前区域 revision",
      p["status"] == "active" and p["revision"] == rev, str(p))
check("绝对时刻轨迹：首点=出发时刻，次点=出发时刻+60s",
      p["trajectory"][0]["t"] == iso(dep)
      and p["trajectory"][1]["t"] == iso(dep + timedelta(seconds=60))
      and p["trajectory"][0]["x"] == -50.0 and p["trajectory"][0]["y"] == 50.0,
      str(p["trajectory"]))
check("issued_at/cancelled_at 字段齐全（cancelled_at=null）",
      bool(p["issued_at"]) and p["cancelled_at"] is None)
pass_id = p["id"]

check("只新增一张凭证", count("passes") == n_passes_before + 1)
check("兑现不推进区域 revision",
      client.get("/api/revision").json()["revision"] == rev)
check("兑现不写区域变更审计",
      count("region_change_audit") == n_audit_before)

# 凭证可经既有凭证接口查询，内容一致
rg = client.get(f"/api/passes/{pass_id}")
check("GET /api/passes/{id} 与兑现响应一致",
      rg.status_code == 200 and rg.json() == p, f"{rg.text} vs {p}")

# 承诺查询：按既有取消语义失效；fulfilled_pass_id 指向新凭证
rg = client.get(f"/api/commitments/{cid}")
crow = rg.json()
check("承诺 GET：status=cancelled、intervals 空、fulfilled_pass_id 指向凭证",
      rg.status_code == 200 and crow["status"] == "cancelled"
      and crow["intervals"] == [] and crow["fulfilled_pass_id"] == pass_id
      and crow["cancelled_at"] is not None, str(crow))

# ====================================================================
# 2. 重复兑现 → 409 commitment_already_fulfilled（与普通取消区分）
# ====================================================================
r = fulfill(cid, iso(at(8, 59, 45)))
check("重复兑现 → 409 commitment_already_fulfilled",
      r.status_code == 409
      and r.json()["error"]["code"] == "commitment_already_fulfilled",
      r.text)
check("409 details 带已生成 pass_id",
      r.json()["error"]["details"][0]["pass_id"] == pass_id,
      str(r.json()["error"]["details"]))
check("重复兑现不再生成凭证", count("passes") == n_passes_before + 1)
check("409 后 revision 仍不变",
      client.get("/api/revision").json()["revision"] == rev)

# 兑现后的承诺再取消：既有取消接口幂等，仍返回 cancelled 与兑现标记
r = client.post(f"/api/commitments/{cid}/cancel")
check("已兑现承诺取消幂等 200，保留 fulfilled_pass_id",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["fulfilled_pass_id"] == pass_id, r.text)

# ====================================================================
# 3. 不安全 / 端点 / 越界 / 非未来：均 422 且承诺不变、无新凭证
# ====================================================================
r = issue(TRAJ, iso(at(7, 58)), iso(at(9, 1)))
check("第二张承诺 201", r.status_code == 201, r.text)
c2 = r.json()["id"]
np0 = count("passes")

# 禁止区间内部 → unsafe_trajectory，details 回显当前安全区间
r = fulfill(c2, iso(at(8, 0)))
check("禁止区间内部出发 → 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory",
      r.text)
safe_ivs = next(
    (d.get("safe_intervals") for d in r.json()["error"]["details"]
     if "safe_intervals" in d),
    None,
)
check("unsafe 明细回显当前两段安全区间（端点精确）",
      safe_ivs is not None and len(safe_ivs) == 2
      and safe_ivs[0]["start"] == iso(at(7, 58))
      and safe_ivs[0]["end"] == iso(at(7, 59, 15))
      and safe_ivs[1]["start"] == iso(at(8, 59, 45))
      and safe_ivs[1]["end"] == iso(at(9, 1)), str(safe_ivs))
check("unsafe 后承诺仍 active、区间不变、无新凭证",
      client.get(f"/api/commitments/{c2}").json()["status"] == "active"
      and count("passes") == np0)

# 禁止区间左闭端点 s=07:59:15（恰在 valid_from 接触）→ 不安全
r = fulfill(c2, iso(at(7, 59, 15)))
check("禁止区间闭端点出发 → 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory",
      r.text)

# 禁止区间右端开 s=08:59:45（恰在 valid_to 接触）→ 安全，兑现成功
r = fulfill(c2, iso(at(8, 59, 45)))
check("安全开端点 08:59:45 出发 → 201", r.status_code == 201, r.text)
pid2 = r.json()["pass"]["id"]
check("凭证首点恰为 08:59:45、次点 09:00:45",
      r.json()["pass"]["trajectory"][0]["t"] == iso(at(8, 59, 45))
      and r.json()["pass"]["trajectory"][1]["t"] == iso(at(9, 0, 45)),
      str(r.json()["pass"]["trajectory"]))
check("两张承诺各兑现一张凭证", count("passes") == np0 + 1)
del pid2

# 新承诺：越界 / 非未来
r = issue(TRAJ, iso(at(7, 58)), iso(at(9, 1)))
c3 = r.json()["id"]
r = fulfill(c3, iso(at(9, 1)))  # 恰为窗口右端（开端）→ 越界
check("出发=window_end → 422 departure_out_of_window",
      r.status_code == 422
      and r.json()["error"]["code"] == "departure_out_of_window", r.text)
r = fulfill(c3, iso(at(7, 57, 59)))  # 窗口起点之前
check("出发<window_start → 422 departure_out_of_window",
      r.status_code == 422
      and r.json()["error"]["code"] == "departure_out_of_window", r.text)
r = fulfill(c3, iso(at(10, 0)))  # 窗口之后
check("出发>=window_end → 422 departure_out_of_window",
      r.status_code == 422
      and r.json()["error"]["code"] == "departure_out_of_window", r.text)
# 恰为窗口起点（闭端）且安全 → 201，顺带验证 c3 未被前面的失败改变
r = fulfill(c3, iso(at(7, 58)))
check("窗口起点（闭端）安全 → 201，前面失败未改变承诺",
      r.status_code == 201, r.text)

# 非未来时刻（即使在窗口与安全区间外也优先判定时间先后顺序在此不做要求，
# 这里用一张近未来窗口承诺，提交不晚于受理时刻的出发时刻）
r = issue(TRAJ_HIGH, future(600), future(900))
check("近未来承诺签发 201（高位轨迹不触 R1）", r.status_code == 201, r.text)
c4 = r.json()["id"]
r = fulfill(c4, iso(datetime.now(timezone.utc)))
check("出发=受理时刻 → 422 departure_not_in_future",
      r.status_code == 422
      and r.json()["error"]["code"] == "departure_not_in_future", r.text)
r = fulfill(c4, future(-5))
check("出发早于受理时刻 → 422 departure_not_in_future",
      r.status_code == 422
      and r.json()["error"]["code"] == "departure_not_in_future", r.text)
check("时间类失败后承诺仍 active",
      client.get(f"/api/commitments/{c4}").json()["status"] == "active")

# 不带时区 / 多余字段 → 422 validation_error
r = client.post(f"/api/commitments/{c4}/fulfill",
                json={"departure_time": "2030-06-01T08:00:00"})
check("出发时刻不带时区 → 422", r.status_code == 422, r.text)
r = client.post(f"/api/commitments/{c4}/fulfill",
                json={"departure_time": iso(at(7, 58)), "x": 1})
check("多余字段 → 422", r.status_code == 422, r.text)

# 带非 UTC 时区（+08:00）归一化：07:58Z = 15:58+08:00（需要该时刻安全）
r = issue(TRAJ, iso(at(7, 58)), iso(at(9, 1)))
c5 = r.json()["id"]
r = fulfill(c5, "2030-06-01T15:58:00+08:00")
check("+08:00 时区归一化为 07:58Z 安全时刻 → 201",
      r.status_code == 201
      and r.json()["pass"]["trajectory"][0]["t"] == iso(at(7, 58)), r.text)

# ====================================================================
# 4. 已取消承诺不能兑现（422 commitment_cancelled）；未知承诺 404
# ====================================================================
r = issue(TRAJ_HIGH, future(600), future(900))
c6 = r.json()["id"]
r = client.post(f"/api/commitments/{c6}/cancel")
check("取消 c6 200", r.status_code == 200 and r.json()["fulfilled_pass_id"] is None,
      r.text)
r = fulfill(c6, future(660))
check("已普通取消 → 422 commitment_cancelled",
      r.status_code == 422
      and r.json()["error"]["code"] == "commitment_cancelled", r.text)
check("普通取消不生成凭证", count("passes") == np0 + 3)

r = fulfill(999999, future(60))
check("未知承诺 → 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# ====================================================================
# 5. 窗口结束 → 422 commitment_expired（即使提交窗口内时刻）
# ====================================================================
w_end = datetime.now(timezone.utc) + timedelta(seconds=1.5)
r = issue(TRAJ_HIGH, future(0.2), iso(w_end))
check("极短窗口承诺 201", r.status_code == 201, r.text)
c7 = r.json()["id"]
time.sleep(1.8)  # 等窗口结束
r = fulfill(c7, iso(w_end - timedelta(seconds=0.2)))
check("窗口结束后兑现 → 422 commitment_expired",
      r.status_code == 422
      and r.json()["error"]["code"] == "commitment_expired", r.text)
check("过期承诺不生成凭证且仍非 cancelled（status=active）",
      client.get(f"/api/commitments/{c7}").json()["status"] == "active"
      and client.get(f"/api/commitments/{c7}").json()["fulfilled_pass_id"] is None)

# ====================================================================
# 6. 区域在签发后变化：兑现以「当前区域集合」重验
# ====================================================================
reset_all()
# 区域只在 10:30-10:31 生效：对 TRAJ（接触相对 15s-45s）的禁止出发区间
# 为 [10:29:15,10:30:45)；用低位轨迹的承诺验证「按当前集合重算」
r = create_region("R-中", SQUARE, at(10, 30), at(10, 31))
check("登记中部短时区域 201", r.status_code == 201, r.text)
r = issue(TRAJ, iso(at(10)), iso(at(11)))
check("两段安全区间承诺 201", r.status_code == 201 and len(r.json()["intervals"]) == 2,
      r.text)
c8 = r.json()["id"]
rev6 = client.get("/api/revision").json()["revision"]
# 被压的中段时刻 → 422，且所据 revision 为新集合
r = fulfill(c8, iso(at(10, 30)))
check("区域变化后原安全时刻不再安全 → 422",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory",
      r.text)
# 仍安全的窗口起点 → 201，凭证 revision 为当前（新）revision；
# 凭证行程 10:00:00-10:01:00，在区域 10:30 生效之前
r = fulfill(c8, iso(at(10)))
check("当前安全时刻兑现 201，凭证 revision=新 revision",
      r.status_code == 201 and r.json()["revision"] == rev6
      and r.json()["pass"]["revision"] == rev6, r.text)

# 一张新的 active 高位承诺：长期 HIGH_SQUARE 会清空其窗口 → 409
r = issue(TRAJ_HIGH, iso(at(12)), iso(at(13)))
c9 = r.json()["id"]
r = create_region("R-全压", HIGH_SQUARE,
                  datetime(2020, 1, 1, tzinfo=timezone.utc),
                  datetime(2032, 1, 1, tzinfo=timezone.utc))
check("活跃承诺仍受 commitment_conflict 保护",
      r.status_code == 409
      and [d["commitment_id"] for d in r.json()["error"]["details"]] == [c9],
      r.text)
# active 承诺窗口已被「尝试变更」之外的因素影响了吗？没有——409 整笔回滚，
# 集合仍只有 R-中（y=0 侧 SQUARE），不接触高位轨迹 → c9 当前全窗口安全，
# 但 12:00-13:00 窗口与 R-中（10:30-10:31）不相交，故可正常兑现
r = fulfill(c9, iso(at(12)))
check("回滚后高位承诺在当前集合下安全 → 兑现 201",
      r.status_code == 201, r.text)

# ====================================================================
# 7. 兑现产生的凭证即刻受 pass_conflict 保护
# ====================================================================
reset_all()
# 区域只在凭证行程时刻之外生效 → 凭证安全
trip_vf = at(12, 0)
trip_vt = at(13, 0)
r = create_region("R1", SQUARE, trip_vf, trip_vt)
rev7 = r.json()["revision"]
# 承诺窗口 10:00-11:00：禁止区间 = [vf-45, vt-15) = [11:59:15, 12:59:45)
# 与窗口完全不相交 → 全窗口安全
r = issue(TRAJ, iso(at(10)), iso(at(11)))
c10 = r.json()["id"]
dep = at(10, 30)
r = fulfill(c10, iso(dep))
check("凭证行程在区域生效前 → 兑现 201", r.status_code == 201, r.text)
new_pass_id = r.json()["pass"]["id"]
# 新凭证的行程为 10:30:00~10:31:00；把 R1 修订为长期生效 → 轨迹穿越 → 409
r = client.patch(f"/api/regions/1", json={
    "valid_from": iso(datetime(2020, 1, 1, tzinfo=timezone.utc)),
    "valid_to": iso(datetime(2032, 1, 1, tzinfo=timezone.utc)),
})
check("区域修订危及兑现凭证 → 409 pass_conflict（details 为该凭证）",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict"
      and [d["pass_id"] for d in r.json()["error"]["details"]] == [new_pass_id],
      r.text)
check("409 后 revision 不变",
      client.get("/api/revision").json()["revision"] == rev7)
# 已兑现承诺不以 commitment 身份再出现在冲突里
check("409 明细不含 commitment_conflict 语义（仅凭证保护）",
      all("commitment_id" not in d for d in r.json()["error"]["details"]))
# 凭证取消后区域变更放行
r = client.post(f"/api/passes/{new_pass_id}/cancel")
check("取消兑现凭证 200", r.status_code == 200, r.text)
r = client.patch(f"/api/regions/1", json={
    "valid_from": iso(datetime(2020, 1, 1, tzinfo=timezone.utc)),
    "valid_to": iso(datetime(2032, 1, 1, tzinfo=timezone.utc)),
})
check("凭证取消后修订 200", r.status_code == 200, r.text)

# ====================================================================
# 8. 并发兑现：至多一次成功，败者得 409 已兑现
# ====================================================================
reset_all()
r = issue(TRAJ_HIGH, iso(at(10)), iso(at(11)))
c11 = r.json()["id"]
barrier = threading.Barrier(2)
outcome: dict = {}


def worker(tag):
    barrier.wait()
    rr = fulfill(c11, iso(at(10, 15)))
    with lock:
        outcome[tag] = (rr.status_code, rr.json())


lock = threading.Lock()
t1 = threading.Thread(target=worker, args=("a",))
t2 = threading.Thread(target=worker, args=("b",))
t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
codes = sorted(outcome[k][0] for k in outcome)
check("并发兑现：一个 201、一个 409", codes == [201, 409], str(outcome)[:400])
loser = next(v for v in outcome.values() if v[0] == 409)
winner = next(v for v in outcome.values() if v[0] == 201)
check("败者 409 码为 commitment_already_fulfilled 且 pass_id 指向胜者凭证",
      loser[1]["error"]["code"] == "commitment_already_fulfilled"
      and loser[1]["error"]["details"][0]["pass_id"] == winner[1]["pass"]["id"],
      str(loser)[:300])
check("并发后只生成一张凭证", count("passes") == 1)
check("并发后区域 revision 仍为 0",
      client.get("/api/revision").json()["revision"] == 0)

# ====================================================================
# 9. 并发「兑现 vs 取消」：互斥，至多一张凭证，两种失败原因可区分
# ====================================================================
r = issue(TRAJ_HIGH, iso(at(10)), iso(at(11)))
c12 = r.json()["id"]
barrier2 = threading.Barrier(2)
out2: dict = {}


def fulfill_w():
    barrier2.wait()
    rr = fulfill(c12, iso(at(10, 15)))
    out2["fulfill"] = (rr.status_code, rr.json())


def cancel_w():
    barrier2.wait()
    rr = client.post(f"/api/commitments/{c12}/cancel")
    out2["cancel"] = (rr.status_code, rr.json())


t1 = threading.Thread(target=fulfill_w)
t2 = threading.Thread(target=cancel_w)
t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
fc, fj = out2["fulfill"]
cc, cj = out2["cancel"]
if fc == 201:
    check("兑现先成功：取消 200 幂等且保留 fulfilled_pass_id",
          cc == 200 and cj["status"] == "cancelled"
          and cj["fulfilled_pass_id"] == fj["pass"]["id"], str(out2)[:400])
    check("此情形只生成一张凭证", count("passes") == 2)  # 上组 1 + 本组 1
else:
    check("取消先成功：兑现 422 commitment_cancelled",
          fc == 422 and fj["error"]["code"] == "commitment_cancelled",
          str(out2)[:400])
    check("取消先成功时不生成凭证", count("passes") == 1)
# 再兑现在两种终态下都不会成功
r = fulfill(c12, iso(at(10, 15)))
check("终态后再兑现：409 已兑现或 422 已取消",
      (r.status_code == 409
       and r.json()["error"]["code"] == "commitment_already_fulfilled")
      or (r.status_code == 422
          and r.json()["error"]["code"] == "commitment_cancelled"), r.text)

# ====================================================================
# 10. 并发「兑现 vs 区域变更」：同一写锁串行，无漏检窗口
# ====================================================================
reset_all()
# R1 在 08:00-09:00 生效；承诺窗口 07:58-09:01，s=08:30 为禁止时刻
create_region("R1", SQUARE, at(8), at(9))
r = issue(TRAJ_HIGH, iso(at(10)), iso(at(11)))
# 高位轨迹不触 R1，用另一区域在并发中落下：长期生效的 HIGH_SQUARE
r = issue(TRAJ, iso(at(7, 58)), iso(at(9, 1)))
c13 = r.json()["id"]
rev_before = client.get("/api/revision").json()["revision"]
barrier3 = threading.Barrier(2)
out3: dict = {}


def fulfill_w2():
    barrier3.wait()
    # 07:58 安全、08:00 不安全；兑现线程取安全的 07:58
    rr = fulfill(c13, iso(at(7, 58)))
    out3["fulfill"] = (rr.status_code, rr.json())


def create_w2():
    barrier3.wait()
    # 长期生效的 SQUARE 会同时：抹不掉 c13？SQUARE 全时段则 c13 窗口全被压
    # → 若承诺仍 active 则 409 commitment_conflict；若已兑现则凭证轨迹
    # 10:.. 不，这里只验证无漏检：两种结局都自洽
    rr = create_region("R2-全压", SQUARE,
                       datetime(2020, 1, 1, tzinfo=timezone.utc),
                       datetime(2032, 1, 1, tzinfo=timezone.utc))
    out3["create"] = (rr.status_code, rr.json())


t1 = threading.Thread(target=fulfill_w2)
t2 = threading.Thread(target=create_w2)
t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
fs, fb = out3["fulfill"]
cs, cb = out3["create"]
if fs == 201:
    # 兑现先提交：承诺已失效，新凭证轨迹为 07:58-07:59，长期 SQUARE 会
    # 压住该凭证 → 建区必须 409 pass_conflict
    check("兑现先成功 → 建区 409 pass_conflict",
          cs == 409 and cb["error"]["code"] == "pass_conflict"
          and [d["pass_id"] for d in cb["error"]["details"]] == [fb["pass"]["id"]],
          str(out3)[:400])
else:
    # 建区先提交：active 承诺窗口被清空 → 409 commitment_conflict，
    # 兑现随后必失败（unsafe）
    check("建区先成功 → 409 commitment_conflict 挡住，兑现失败",
          cs == 409 and cb["error"]["code"] == "commitment_conflict"
          and fs == 422 and fb["error"]["code"] == "unsafe_trajectory",
          str(out3)[:400])
check("串行化后无 5xx", fs < 500 and cs < 500)

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部承诺兑现 API 集成用例通过")
