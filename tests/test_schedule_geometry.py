"""出发时刻调度引擎（geometry.safe_departure_intervals）的离线验证。

直接 `python3 tests/test_schedule_geometry.py` 运行；失败时退出码非 0。
不依赖数据库。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import Polygon

from app import geometry


def E(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def P(t: float, x: float, y: float):
    return (t, x, y)


def region(rid, vf, vt, ring, name="R"):
    return {
        "id": rid,
        "name": name,
        "valid_from": E(vf),
        "valid_to": E(vt),
        "polygon": Polygon(ring),
    }


failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def eqiv(iv, s_iso, e_iso, tol=1e-6):
    return (
        abs(iv["start"] - E(s_iso)) <= tol
        and abs(iv["end"] - E(e_iso)) <= tol
        and iv["start_inclusive"] is True
        and iv["end_inclusive"] is False
    )


SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]

# ---------------------------------------------------------------- 基本场景

# 轨迹：相对时刻 0s 位于 (-5,5)，60s 后到 (15,5)；接触 [15s,45s]
TRAJ = [P(0, -5, 5), P(60, 15, 5)]

# 1) 区域生效起点 00:01:00（窗口内）：禁止左端 = vf-45s = 00:00:15
R_FULL = region(1, "2026-09-30T00:01:00Z", "2026-10-02T00:00:00Z", SQUARE)
WS, WE = E("2026-09-30T00:00:00Z"), E("2026-09-30T12:00:00Z")
ivs = geometry.safe_departure_intervals(TRAJ, [R_FULL], WS, WE)
check("区域 00:01:00 才生效：单段安全区间 [窗口起, 00:00:15Z)",
      len(ivs) == 1
      and eqiv(ivs[0], "2026-09-30T00:00:00Z", "2026-09-30T00:00:15Z"),
      str(ivs))

# 1b) 区域跨窗口全天生效：窗口内无安全时刻
R_ALLDAY = region(11, "2026-09-29T00:00:00Z", "2026-10-02T00:00:00Z", SQUARE)
ivs = geometry.safe_departure_intervals(TRAJ, [R_ALLDAY], WS, WE)
check("区域跨窗口持续生效：无安全出发时刻（空）", ivs == [])

# 2) 区域 [00:10:00, 00:20:00) 生效：禁止 [00:09:15, 00:19:45)；窗口 [00:00,01:00)
#    安全两段 [00:00:00,00:09:15) 与 [00:19:45,01:00:00)
R2 = region(2, "2026-09-30T00:10:00Z", "2026-09-30T00:20:00Z", SQUARE)
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2], E("2026-09-30T00:00:00Z"), E("2026-09-30T01:00:00Z"))
check("窗口中段禁止：安全两段",
      len(ivs) == 2
      and eqiv(ivs[0], "2026-09-30T00:00:00Z", "2026-09-30T00:09:15Z")
      and eqiv(ivs[1], "2026-09-30T00:19:45Z", "2026-09-30T01:00:00Z"),
      str(ivs))

# 3) 窗口完全落在禁止区间内 → 空
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2], E("2026-09-30T00:12:00Z"), E("2026-09-30T00:15:00Z"))
check("窗口完全不可行：空数组", ivs == [])

# 4) 窗口与禁止区间完全不相交 → 整个窗口安全
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2], E("2026-09-30T02:00:00Z"), E("2026-09-30T03:00:00Z"))
check("窗口完全安全：整段返回",
      len(ivs) == 1
      and eqiv(ivs[0], "2026-09-30T02:00:00Z", "2026-09-30T03:00:00Z"))

# 4b) 单个禁止区间远跨过窗口右端：窗口右端之后仍被同一禁止区间覆盖，
#     此时不应错误补出尾部安全段
RLONG = region(12, "2026-09-30T00:09:00Z", "2026-10-02T00:00:00Z", SQUARE)
# 禁止 [vf-45, vt-15) = [00:08:15, 远在窗口之后)；窗口 [00:08:00,00:10:00)
ivs = geometry.safe_departure_intervals(
    TRAJ, [RLONG], E("2026-09-30T00:08:00Z"), E("2026-09-30T00:10:00Z"))
check("禁止区间跨过窗口右端：仅前导安全段，不错误补尾段",
      len(ivs) == 1
      and eqiv(ivs[0], "2026-09-30T00:08:00Z", "2026-09-30T00:08:15Z"),
      str(ivs))

# 4c) 窗口完全位于跨右端禁止区间内部：空
ivs = geometry.safe_departure_intervals(
    TRAJ, [RLONG], E("2026-09-30T00:08:30Z"), E("2026-09-30T00:10:00Z"))
check("窗口完全位于跨右端禁止区间内部：空", ivs == [], str(ivs))

# 5) 无区域 → 整窗安全
ivs = geometry.safe_departure_intervals(
    TRAJ, [], E("2026-09-30T00:00:00Z"), E("2026-09-30T01:00:00Z"))
check("无区域：整窗安全",
      len(ivs) == 1
      and eqiv(ivs[0], "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z"))

# ---------------------------------------------------------------- 单点接触

# 轨迹在相对时刻 5s 擦过角点 (10,10)：接触为退化点 [5,5]
TRAJ_TOUCH = [P(0, 5, 15), P(10, 15, 5)]

# 6) 区域生效 [00:00:00, 00:10:00)：禁止 s∈[vf-5, vt-5) = [23:59:55, 00:09:55)
RT = region(3, "2026-09-30T00:00:00Z", "2026-09-30T00:10:00Z", SQUARE)
ivs = geometry.safe_departure_intervals(
    TRAJ_TOUCH, [RT], E("2026-09-30T00:00:00Z"), E("2026-09-30T00:20:00Z"))
check("单点接触：禁止 [23:59:55,00:09:55)，窗口内安全 1 段 [00:09:55,00:20:00)",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-30T00:09:55Z",
                              "2026-09-30T00:20:00Z"), str(ivs))

# 7) 接触时刻（出发后 5s）恰好等于 valid_to：s = vt - 5 = 00:09:55 必须安全
#    即禁止区间右端为开，安全区间左端闭且包含 00:09:55
check("安全区间左端点=valid_to-接触时刻（闭，可出发）",
      abs(ivs[0]["start"] - E("2026-09-30T00:09:55Z")) < 1e-6
      and ivs[0]["start_inclusive"] is True)

# 8) 接触时刻恰好等于 valid_from：s = vf - 5 = 23:59:55 前一天，禁止（左端闭）
ivs = geometry.safe_departure_intervals(
    TRAJ_TOUCH, [RT], E("2026-09-29T23:59:00Z"), E("2026-09-30T00:05:00Z"))
check("valid_from 端点：禁止左端闭，23:59:55 不可出发",
      len(ivs) == 1
      and eqiv(ivs[0], "2026-09-29T23:59:00Z", "2026-09-29T23:59:55Z"),
      str(ivs))

# ---------------------------------------------------------------- 沿边界运动

# 轨迹沿正方形底边从 (0,0) 到 (10,0)，相对 0~10s 全部接触
TRAJ_EDGE = [P(0, 0, 0), P(10, 10, 0)]
RE = region(4, "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z", SQUARE)
# 禁止 s ∈ [00:00:00-10, 01:00:00-0) = [23:59:50, 01:00:00)
ivs = geometry.safe_departure_intervals(
    TRAJ_EDGE, [RE], E("2026-09-30T00:00:00Z"), E("2026-09-30T02:00:00Z"))
check("沿边界全程接触：窗口起即不可行，01:00:00 起恢复（右端开）",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-30T01:00:00Z",
                              "2026-09-30T02:00:00Z"), str(ivs))

# 提前 10 秒整出发，轨迹在 valid_from 恰好开始沿边：s+0 == vf → 违规（闭）
ivs = geometry.safe_departure_intervals(
    TRAJ_EDGE, [RE], E("2026-09-29T23:59:00Z"), E("2026-09-30T00:00:00Z"))
check("提前 10s 出发、valid_from 时刻到达边界：违规，安全段止于 23:59:50",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-29T23:59:00Z",
                              "2026-09-29T23:59:50Z"), str(ivs))

# ---------------------------------------------------------------- 多区域并集

# R2 禁止 [00:09:15,00:19:45)；再加 R3（接触同为 [15,45]）生效至
# [00:10:00, 00:30:00)：禁止 [00:09:15, 00:29:45)，与 R2 重叠，合并后
# 仍为 [00:09:15, 00:29:45)。再造 R4 生效 [00:30:30, 00:40:00) 验证分离。
R3 = region(5, "2026-09-30T00:10:00Z", "2026-09-30T00:30:00Z", SQUARE)
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2, R3], E("2026-09-30T00:00:00Z"), E("2026-09-30T01:00:00Z"))
check("两区域禁止区间重叠：合并为 [00:09:15,00:29:45)",
      len(ivs) == 2
      and eqiv(ivs[0], "2026-09-30T00:00:00Z", "2026-09-30T00:09:15Z")
      and eqiv(ivs[1], "2026-09-30T00:29:45Z", "2026-09-30T01:00:00Z"),
      str(ivs))

# R4 接触 [15,45]、生效 [00:30:00,00:30:30) → 禁止 [00:29:15,00:30:15)，
# 与前段 [00:09:15,00:29:45) 重叠 30 秒，取并集合并为 [00:09:15,00:30:15)。
R4 = region(8, "2026-09-30T00:30:00Z", "2026-09-30T00:30:30Z", SQUARE)
fb = geometry.forbidden_departure_intervals(TRAJ, [R2, R3, R4])
check("三区域禁止区间重叠：合并为 [00:09:15,00:30:15) 一整段",
      len(fb) == 1
      and abs(fb[0][0] - E("2026-09-30T00:09:15Z")) < 1e-6
      and abs(fb[0][1] - E("2026-09-30T00:30:15Z")) < 1e-6, str(fb))

# 端点精确相接：R5 生效 [00:30:30, 00:40:00) → 禁止 [00:29:45,00:39:45)，
# 左端 00:29:45 恰好等于 R3 禁止右端 00:29:45 → 并集相接，中间无安全缝隙
R5 = region(9, "2026-09-30T00:30:30Z", "2026-09-30T00:40:00Z", SQUARE)
fb = geometry.forbidden_departure_intervals(TRAJ, [R3, R5])
check("禁止区间端点精确相接：合并无缝隙",
      len(fb) == 1
      and abs(fb[0][0] - E("2026-09-30T00:09:15Z")) < 1e-6
      and abs(fb[0][1] - E("2026-09-30T00:39:45Z")) < 1e-6, str(fb))
ivs = geometry.safe_departure_intervals(
    TRAJ, [R3, R5], E("2026-09-30T00:00:00Z"), E("2026-09-30T01:00:00Z"))
check("相接点 00:29:45 不属于任一安全区间",
      len(ivs) == 2
      and eqiv(ivs[0], "2026-09-30T00:00:00Z", "2026-09-30T00:09:15Z")
      and eqiv(ivs[1], "2026-09-30T00:39:45Z", "2026-09-30T01:00:00Z"),
      str(ivs))

# 两个地理上分离的区域在不同相对时刻接触，禁止区间分离
SQUARE2 = [[100, 0], [110, 0], [110, 10], [100, 10]]
TRAJ_LONG = [P(0, -5, 5), P(600, 115, 5)]
# 接触 SQUARE: u=5/120..15/120 → t=25..75；SQUARE2: 525..575
RA = region(6, "2026-09-30T00:00:00Z", "2026-09-30T00:01:00Z", SQUARE, "A")
RB = region(7, "2026-09-30T00:00:00Z", "2026-09-30T00:01:00Z", SQUARE2, "B")
# A 禁止 [-75, 35)；B 禁止 [-575, -525)（均为窗口前）；窗口内仅 A 的尾巴 0..35
ivs = geometry.safe_departure_intervals(
    TRAJ_LONG, [RA, RB], E("2026-09-30T00:00:00Z"),
    E("2026-09-30T00:10:00Z"))
check("分离区域：仅窗口内的禁止段生效",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-30T00:00:35Z",
                              "2026-09-30T00:10:00Z"), str(ivs))

# 同一区域被轨迹两次穿过：接触 [15,45] 与 [75,105]；区域 [00:00,01:00)
# 禁止 [-105,3585) 与 [-45,3525) → 重叠合并为一整段（长度 3690s）
TRAJ_TWICE = [P(0, -5, 5), P(60, 15, 5), P(120, -5, 5)]
ci = geometry.contact_intervals_relative(TRAJ_TWICE, Polygon(SQUARE))
check("同一区域两次接触：两个接触区间",
      ci == [(15.0, 45.0), (75.0, 105.0)], str(ci))
RTW = region(10, "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z", SQUARE)
fb = geometry.forbidden_departure_intervals(TRAJ_TWICE, [RTW])
check("两次接触的禁止区间重叠：合并为一整段（3690s）",
      len(fb) == 1 and abs((fb[0][1] - fb[0][0]) - 3690) < 1e-6, str(fb))

# 区域在候选窗口整整一天之前已失效：窗口整段安全
ROLD = region(11, "2026-09-29T00:00:00Z", "2026-09-29T00:01:00Z", SQUARE)
ivs = geometry.safe_departure_intervals(
    TRAJ_TWICE, [ROLD], E("2026-09-30T00:00:00Z"),
    E("2026-09-30T01:00:00Z"))
check("隔日已失效区域：窗口整段安全",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-30T00:00:00Z",
                              "2026-09-30T01:00:00Z"))

# ---------------------------------------------------------------- 边界裁剪

# 禁止区间只在窗口内部一点：禁止 [00:09:15,00:19:45)，窗口 [00:09:30,00:10:00)
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2], E("2026-09-30T00:09:30Z"), E("2026-09-30T00:10:00Z"))
check("窗口完全在禁止内（子集）：空", ivs == [])

# 窗口左端恰为禁止左端 00:09:15（闭）：00:09:15 不可出发 → 空
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2], E("2026-09-30T00:09:15Z"), E("2026-09-30T00:10:00Z"))
check("窗口左端=禁止左端（闭）：空", ivs == [])

# 窗口右端恰为禁止右端 00:19:45（开）：该时刻安全，但窗口本身右端也开
ivs = geometry.safe_departure_intervals(
    TRAJ, [R2], E("2026-09-30T00:19:45Z"), E("2026-09-30T00:30:00Z"))
check("窗口起于禁止右端（开）：整段安全，左端闭",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-30T00:19:45Z",
                              "2026-09-30T00:30:00Z"))

# ---------------------------------------------------------------- 停留段

# 相对 0~10s 停在区域内 (5,5)
TRAJ_STAY = [P(0, 5, 5), P(10, 5, 5)]
ivs = geometry.safe_departure_intervals(
    TRAJ_STAY, [RE], E("2026-09-30T00:00:00Z"), E("2026-09-30T01:00:00Z"))
# 接触 [0,10]，生效 [00:00,01:00) → 禁止 [23:59:50, 01:00:00)；窗口内全禁
check("停留区域内：窗口内无安全时刻", ivs == [])

ivs = geometry.safe_departure_intervals(
    TRAJ_STAY, [RE], E("2026-09-29T23:00:00Z"), E("2026-09-30T00:00:00Z"))
check("停留段：安全止于 23:59:50",
      len(ivs) == 1 and eqiv(ivs[0], "2026-09-29T23:00:00Z",
                              "2026-09-29T23:59:50Z"), str(ivs))

# ---------------------------------------------------------------- 输入校验

class RP:
    def __init__(self, t, x, y):
        self.t, self.x, self.y = t, x, y


def expect_error(label, points):
    try:
        geometry.validate_relative_trajectory(
            points, lambda p: (p.x, p.y), lambda p: p.t)
    except geometry.TrajectoryError as e:
        check(f"{label}（{e}）", True)
        return
    check(label + "：应当报错", False)


expect_error("相对轨迹点不足", [RP(0, 0, 0)])
expect_error("首点相对秒数非 0", [RP(1, 0, 0), RP(10, 1, 1)])
expect_error("相对时刻重复", [RP(0, 0, 0), RP(0, 1, 1)])
expect_error("相对时刻逆序", [RP(0, 0, 0), RP(-1, 1, 1)])
expect_error("相对时刻 NaN", [RP(0, float("nan"), 0), RP(10, 1, 1)])
expect_error("相对时刻 Inf", [RP(0, 0, 0), RP(float("inf"), 1, 1)])
expect_error("坐标 NaN", [RP(0, 0, float("nan")), RP(10, 1, 1)])

# 合法：首点 -0.0
try:
    out = geometry.validate_relative_trajectory(
        [RP(-0.0, 0, 0), RP(10, 1, 1)],
        lambda p: (p.x, p.y), lambda p: p.t)
    check("首点 -0.0 视为合法的 0", len(out) == 2 and out[0][0] == 0.0)
except geometry.TrajectoryError as e:
    check(f"首点 -0.0 合法（异常: {e}）", False)

print()
if failures:
    print(f"{len(failures)} 个用例失败")
    sys.exit(1)
print("全部调度几何用例通过")
