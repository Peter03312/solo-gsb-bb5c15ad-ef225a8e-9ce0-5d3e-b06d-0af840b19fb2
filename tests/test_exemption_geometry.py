"""最小区域豁免预演引擎（geometry.minimal_exemption_plan）的离线验证。

直接 `python3 tests/test_exemption_geometry.py` 运行；失败时退出码非 0。
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
# 轨迹：相对 0s 位于 (-5,5)，60s 后到 (15,5)；对 SQUARE 的接触为相对 [15s,45s]
# 区域生效 [vf,vt) → 禁止出发区间 [vf-45s, vt-15s)
TRAJ = [P(0, -5, 5), P(60, 15, 5)]
WS, WE = E("2026-09-30T00:00:00Z"), E("2026-09-30T01:00:00Z")

# 全窗口生效区域：禁止区间覆盖整个候选窗口
R_FULL = region(1, "2026-09-29T00:00:00Z", "2026-10-02T00:00:00Z", SQUARE)
# 只禁窗口中段 [00:10,00:20) 的区域：vf-45s=00:09:15, vt-15s=00:19:45
R_MID = region(2, "2026-09-30T00:10:00Z", "2026-09-30T00:20:00Z", SQUARE)
# 各禁窗口一半的两个区域：R_LO 禁 [00:00,00:30)，R_HI 禁 [00:30,01:00)
R_LO = region(3, "2026-09-30T00:00:45Z", "2026-09-30T00:30:15Z", SQUARE)
R_HI = region(4, "2026-09-30T00:30:45Z", "2026-09-30T01:00:15Z", SQUARE)
# 远离轨迹的区域（永不接触，禁止区间为空）
R_FAR = region(5, "2026-09-29T00:00:00Z", "2026-10-02T00:00:00Z",
               [[100, 100], [110, 100], [110, 110], [100, 110]])

# ---------------------------------------------------------------- 原本安全

# 1) 空区域集合：整个窗口安全，豁免集为空
plan = geometry.minimal_exemption_plan(TRAJ, [], [], WS, WE)
check("空集合：可行且豁免集为空，最早=窗口起点，单段区间=整个窗口",
      plan["feasible"] is True
      and plan["exempted"] == []
      and abs(plan["earliest"] - WS) <= 1e-6
      and len(plan["intervals"]) == 1
      and eqiv(plan["intervals"][0], "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z"),
      str(plan))

# 2) 区域只禁窗口中段：原本安全，豁免集为空，安全区间为两段
plan = geometry.minimal_exemption_plan(TRAJ, [R_MID], [2], WS, WE)
check("区域只禁中段：原本安全，豁免集为空，两段安全区间",
      plan["feasible"] is True
      and plan["exempted"] == []
      and abs(plan["earliest"] - WS) <= 1e-6
      and len(plan["intervals"]) == 2
      and eqiv(plan["intervals"][0], "2026-09-30T00:00:00Z", "2026-09-30T00:09:15Z")
      and eqiv(plan["intervals"][1], "2026-09-30T00:19:45Z", "2026-09-30T01:00:00Z"),
      str(plan))

# ---------------------------------------------------------------- 最少豁免数

# 3) 全覆盖区域在候选中：豁免它即可，区间=整个窗口
plan = geometry.minimal_exemption_plan(TRAJ, [R_FULL], [1], WS, WE)
check("全覆盖区域可豁免：豁免 [1]，安全区间=整个窗口",
      plan["feasible"] is True
      and plan["exempted"] == [1]
      and abs(plan["earliest"] - WS) <= 1e-6
      and len(plan["intervals"]) == 1
      and eqiv(plan["intervals"][0], "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z"),
      str(plan))

# 4) 无关区域不拉低豁免数：候选含永不接触的区域，仍只豁免 1 个
plan = geometry.minimal_exemption_plan(TRAJ, [R_FULL, R_FAR], [1, 5], WS, WE)
check("候选含无关区域：仍只豁免全覆盖区域 [1]",
      plan["feasible"] is True and plan["exempted"] == [1], str(plan))

# 5) 两个半区共同覆盖窗口：豁免任一即可，取最早安全时刻更早者
#    豁免 R_LO → 前半 [00:00,00:30) 安全（最早 00:00）；
#    豁免 R_HI → 后半 [00:30,01:00) 安全（最早 00:30）→ 选 R_LO
plan = geometry.minimal_exemption_plan(TRAJ, [R_LO, R_HI], [3, 4], WS, WE)
check("两个半区：豁免数最少为 1，取最早安全时刻更早的方案 [3]",
      plan["feasible"] is True
      and plan["exempted"] == [3]
      and abs(plan["earliest"] - WS) <= 1e-6
      and len(plan["intervals"]) == 1
      and eqiv(plan["intervals"][0], "2026-09-30T00:00:00Z", "2026-09-30T00:30:00Z"),
      str(plan))

# 6) 候选顺序不影响结果（编号序列按升序比较与输出）
plan = geometry.minimal_exemption_plan(TRAJ, [R_LO, R_HI], [4, 3], WS, WE)
check("候选顺序不影响最优方案", plan["exempted"] == [3], str(plan))

# 7) 需要豁免 2 个：R_A 禁 [00:00,00:40)，R_B 禁 [00:20,01:00)，R_C 禁全程
#    单独豁免任一仍被其余覆盖；豁免 {A,C} → 剩 R_B → 安全 [00:00,00:20)；
#    豁免 {B,C} → 剩 R_A → 安全 [00:40,01:00)；取最早 → {A,C}
R_A = region(6, "2026-09-30T00:00:45Z", "2026-09-30T00:40:15Z", SQUARE)
R_B = region(7, "2026-09-30T00:20:45Z", "2026-09-30T01:00:15Z", SQUARE)
R_C = region(8, "2026-09-29T00:00:00Z", "2026-10-02T00:00:00Z", SQUARE)
plan = geometry.minimal_exemption_plan(TRAJ, [R_A, R_B, R_C], [6, 7, 8], WS, WE)
check("需豁免 2 个：选最早安全时刻更早的 {6,8}，最早=窗口起点",
      plan["feasible"] is True
      and plan["exempted"] == [6, 8]
      and abs(plan["earliest"] - WS) <= 1e-6
      and len(plan["intervals"]) == 1
      and eqiv(plan["intervals"][0], "2026-09-30T00:00:00Z", "2026-09-30T00:20:00Z"),
      str(plan))

# ---------------------------------------------------------------- 不可行

# 8) 全覆盖区域不在候选中：候选只有无关区域 → 不可行
plan = geometry.minimal_exemption_plan(TRAJ, [R_FULL, R_FAR], [5], WS, WE)
check("全覆盖区域不可豁免：不可行，豁免集/最早时刻为 None，区间为空",
      plan["feasible"] is False
      and plan["exempted"] is None
      and plan["earliest"] is None
      and plan["intervals"] == [], str(plan))

# 9) 候选为空且原本不安全 → 不可行
plan = geometry.minimal_exemption_plan(TRAJ, [R_FULL], [], WS, WE)
check("候选为空且原本不安全：不可行",
      plan["feasible"] is False and plan["exempted"] is None
      and plan["earliest"] is None and plan["intervals"] == [], str(plan))

# 10) 窗口非法（起不早于止）→ 不可行空结果（与调度引擎一致的空语义）
plan = geometry.minimal_exemption_plan(TRAJ, [R_FULL], [1], WE, WS)
check("窗口倒序：不可行空结果",
      plan["feasible"] is False and plan["intervals"] == [], str(plan))

# ---------------------------------------------------------------- 端点开闭沿用

# 11) 只豁免 R_HI：前半被 R_LO 禁止，后半 [00:30,01:00) 安全，最早=00:30
plan = geometry.minimal_exemption_plan(TRAJ, [R_LO, R_HI], [4], WS, WE)
check("只豁免 R_HI：安全区间为后半 [00:30,01:00)，最早=00:30",
      plan["feasible"] is True
      and plan["exempted"] == [4]
      and abs(plan["earliest"] - E("2026-09-30T00:30:00Z")) <= 1e-6
      and len(plan["intervals"]) == 1
      and eqiv(plan["intervals"][0], "2026-09-30T00:30:00Z", "2026-09-30T01:00:00Z"),
      str(plan))
plan = geometry.minimal_exemption_plan(TRAJ, [R_LO, R_HI], [3, 4], WS, WE)
check("左闭右开沿用：豁免 [3] 的安全区间为 [窗口起, 00:30)（右端开）",
      plan["feasible"] is True
      and plan["exempted"] == [3]
      and len(plan["intervals"]) == 1
      and eqiv(plan["intervals"][0], "2026-09-30T00:00:00Z", "2026-09-30T00:30:00Z"),
      str(plan))

# 12) 与 /api/schedule 引擎一致：豁免方案的区间 == 对其余区域直接调调度引擎
rest = [R_HI]
expect = geometry.safe_departure_intervals(TRAJ, rest, WS, WE)
plan = geometry.minimal_exemption_plan(TRAJ, [R_LO, R_HI], [3, 4], WS, WE)
check("豁免方案区间与对其余区域直接调度完全一致",
      plan["intervals"] == expect, f"{plan['intervals']} != {expect}")

print()
if failures:
    print(f"失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("全部豁免预演几何用例通过")
