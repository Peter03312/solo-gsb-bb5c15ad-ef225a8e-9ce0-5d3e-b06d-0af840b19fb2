"""轨迹-多边形穿越核验核心引擎（纯函数，不依赖数据库/HTTP）。

约定
----
* 平面直角坐标系，坐标用有限浮点数表示。
* 轨迹由至少 2 个按时刻严格递增的点组成，相邻点之间直线匀速运动。
* 交点时刻通过轨迹线性插值计算（不做采样点检查）。
* 区域只在左闭右开的生效时段 [valid_from, valid_to) 内参与核验。
* 边界接触也算穿越；单点接触输出起止时刻相同的退化区间。
* 同一区域相接（端点相同）或重叠的穿越区间合并。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    Polygon,
)
from shapely.geometry.polygon import orient
from shapely.errors import GEOSException
from shapely import is_valid_reason
from shapely.wkt import loads as wkt_loads

# 浮点夹紧/归并容差：约 1 微秒的相对量级，只用于吸收几何计算误差
EPS = 1e-9


class RegionError(ValueError):
    """多边形区域非法。"""


class TrajectoryError(ValueError):
    """轨迹数据非法。"""


@dataclass(frozen=True, slots=True)
class EpochInterval:
    """UTC epoch 秒表示的穿越区间（已裁剪到生效窗口内）。

    start == end 表示单点接触。exit_at_window_end=True 表示区间右端点是
    被区域生效窗口的右端 valid_to（开端点）裁掉的，实际接触在该时刻不成立。
    """

    start: float
    end: float
    exit_at_window_end: bool = False


# ---------------------------------------------------------------- 输入校验

def _is_finite_number(value: object) -> bool:
    # bool 是 int 的子类，必须显式排除
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def validate_polygon(vertices: list) -> list[list[float]]:
    """校验并归一化多边形顶点，返回 [[x, y], ...]（闭合，逆时针）。

    要求：至少 3 个点；每个点是两个有限数组成的列表/元组；
    点之间相邻不重复；构造出的 Polygon 必须合法且为简单多边形。
    """
    if not isinstance(vertices, list) or len(vertices) < 3:
        raise RegionError("多边形至少需要 3 个顶点")

    ring: list[tuple[float, float]] = []
    for i, v in enumerate(vertices):
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise RegionError(f"第 {i} 个顶点必须是 [x, y] 形式")
        x, y = v
        if not (_is_finite_number(x) and _is_finite_number(y)):
            raise RegionError(f"第 {i} 个顶点坐标必须为有限数，不能为 NaN/Infinity")
        ring.append((float(x), float(y)))

    # 允许首尾点显式闭合，也允许不闭合；先统一去掉显式闭合点
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise RegionError("多边形至少需要 3 个互不相同的顶点")

    for i in range(len(ring)):
        a = ring[i]
        b = ring[(i + 1) % len(ring)]
        if a == b:
            raise RegionError(f"第 {i} 条边为零长度，存在相邻重复顶点")

    try:
        polygon = Polygon(ring)
    except (ValueError, GEOSException) as exc:
        raise RegionError(f"无法构造多边形: {exc}") from exc

    if not polygon.is_valid:
        raise RegionError(f"多边形非法（自相交或退化）: {is_valid_reason(polygon)}")
    if not polygon.is_simple:
        raise RegionError("多边形不是简单多边形（边界自相交）")
    if polygon.area <= 0:
        raise RegionError("多边形面积为 0，不是有效的简单多边形")

    # 统一为逆时针外环并显式闭合
    oriented = orient(polygon, sign=1.0)
    coords = [(float(x), float(y)) for x, y in oriented.exterior.coords]
    return [[x, y] for x, y in coords]


def polygon_from_wkt(wkt: str) -> Polygon:
    geom = wkt_loads(wkt)
    if not isinstance(geom, Polygon) or not geom.is_valid:
        raise RegionError("存储的多边形 WKT 无效")
    return geom


def validate_trajectory(points: list, time_getter) -> list[tuple[float, float, float]]:
    """校验轨迹点，返回 [(epoch_seconds, x, y), ...]。

    time_getter: 把点对象映射为 aware datetime 的回调。
    """
    if not isinstance(points, list) or len(points) < 2:
        raise TrajectoryError("轨迹至少需要 2 个坐标点")

    out: list[tuple[float, float, float]] = []
    prev_t: float | None = None
    for i, p in enumerate(points):
        if not _is_finite_number(getattr(p, "x", None)) or not _is_finite_number(
            getattr(p, "y", None)
        ):
            raise TrajectoryError(
                f"第 {i} 个轨迹点坐标必须为有限数，不能为 NaN/Infinity"
            )
        t = time_getter(p)
        epoch = t.timestamp()
        if prev_t is not None and epoch <= prev_t:
            raise TrajectoryError(
                f"第 {i} 个轨迹点时刻必须严格递增（不允许重复或逆序时刻）"
            )
        prev_t = epoch
        out.append((epoch, float(p.x), float(p.y)))
    return out


def validate_relative_trajectory(
    points: list, coord_getter, time_getter
) -> list[tuple[float, float, float]]:
    """校验调度用轨迹点，返回 [(t_seconds, x, y), ...]。

    与 validate_trajectory 的区别：时刻是相对首点的秒数而非墙钟时刻。
    要求首点相对秒数恰好为 0（-0.0 视为 0），其余时刻严格递增；
    坐标与时刻都必须是有限数（不允许 NaN/Infinity）。

    coord_getter: 点 -> (x, y)；time_getter: 点 -> 相对秒数。
    """
    if not isinstance(points, list) or len(points) < 2:
        raise TrajectoryError("轨迹至少需要 2 个坐标点")

    out: list[tuple[float, float, float]] = []
    prev_t: float | None = None
    for i, p in enumerate(points):
        x, y = coord_getter(p)
        if not (_is_finite_number(x) and _is_finite_number(y)):
            raise TrajectoryError(
                f"第 {i} 个轨迹点坐标必须为有限数，不能为 NaN/Infinity"
            )
        t = time_getter(p)
        if not _is_finite_number(t):
            raise TrajectoryError(
                f"第 {i} 个轨迹点相对时刻必须为有限数，不能为 NaN/Infinity"
            )
        t = float(t)
        if i == 0:
            if t != 0.0:
                raise TrajectoryError("首点相对秒数必须为 0")
        elif t <= prev_t:
            raise TrajectoryError(
                f"第 {i} 个轨迹点相对时刻必须严格递增（不允许重复或逆序时刻）"
            )
        prev_t = t
        out.append((t, float(x), float(y)))
    return out


# ---------------------------------------------------------------- 区间运算

def _clamp01(u: float) -> float:
    if u < 0.0:
        return 0.0 if u > -EPS else u
    if u > 1.0:
        return 1.0 if u < 1.0 + EPS else u
    return u


def _parameter_of(px: float, py: float, x0: float, y0: float, dx: float, dy: float,
                  seg_len2: float) -> float:
    """点在有向线段上的插值参数 u∈[0,1]（点已由 GEOS 保证落在该线段上）。"""
    if seg_len2 == 0.0:
        return 0.0
    u = ((px - x0) * dx + (py - y0) * dy) / seg_len2
    u = _clamp01(u)
    # 吸收 GEOS 容差后仍超出的，夹回端点
    if u < 0.0 or u > 1.0:
        u = min(1.0, max(0.0, u))
    return u


def _intervals_from_geometry(geom, x0, y0, dx, dy, seg_len2) -> list[tuple[float, float]]:
    """从 Shapely 交集几何中提取参数 u 的 (u_start, u_end) 列表（含端点）。"""
    intervals: list[tuple[float, float]] = []

    def add_point(x: float, y: float) -> None:
        u = _parameter_of(x, y, x0, y0, dx, dy, seg_len2)
        intervals.append((u, u))

    def add_line(coords) -> None:
        us = [
            _clamp01(_parameter_of(x, y, x0, y0, dx, dy, seg_len2))
            for x, y in coords
        ]
        lo, hi = min(us), max(us)
        if hi - lo <= EPS:
            intervals.append((lo, lo))
        else:
            intervals.append((lo, hi))

    if geom.is_empty:
        return intervals

    if isinstance(geom, Point):
        add_point(geom.x, geom.y)
    elif isinstance(geom, MultiPoint):
        for g in geom.geoms:
            add_point(g.x, g.y)
    elif isinstance(geom, LineString):
        add_line(geom.coords)
    elif isinstance(geom, MultiLineString):
        for g in geom.geoms:
            add_line(g.coords)
    elif isinstance(geom, GeometryCollection):
        for g in geom.geoms:
            intervals.extend(
                _intervals_from_geometry(g, x0, y0, dx, dy, seg_len2)
            )
    else:  # Polygon 理论上不会出现在 1D 与 2D 的交集中，稳妥起见处理
        from shapely.geometry import Polygon as _Polygon

        if isinstance(geom, _Polygon):
            add_line(geom.exterior.coords)
    return intervals


def segment_intersection_params(
    x0: float, y0: float, x1: float, y1: float, polygon: Polygon
) -> list[tuple[float, float]]:
    """单条轨迹段与多边形的交集，返回参数 u∈[0,1] 的区间列表（几何含边界）。"""
    dx, dy = x1 - x0, y1 - y0
    seg_len2 = dx * dx + dy * dy

    # 零长度段：坐标相同、时刻不同，表示原地停留；点在区域内/边界上即整段穿越
    if seg_len2 == 0.0:
        p = Point(x0, y0)
        if polygon.covers(p) or polygon.touches(p):
            return [(0.0, 1.0)]
        return []

    try:
        segment = LineString([(x0, y0), (x1, y1)])
        geom = segment.intersection(polygon)
    except GEOSException:
        # GEOS 异常时退化为端点判定
        result = []
        for x, y, u in ((x0, y0, 0.0), (x1, y1, 1.0)):
            if polygon.touches(Point(x, y)) or polygon.covers(Point(x, y)):
                result.append((u, u))
        return result
    return _intervals_from_geometry(geom, x0, y0, dx, dy, seg_len2)


def _clip_with_window(
    intervals: list[EpochInterval], window_start: float, window_end: float
) -> list[EpochInterval]:
    """把几何上的穿越区间裁剪进左闭右开的生效窗口 [ws, we)。

    区域只在 [ws, we) 内参与核验，因此：
    * 接触/穿越完全落在 we 时刻或之后 → 丢弃；
    * 正长度区间被 we 截断时保留 [.., we)，标记 exit_at_window_end；
    * 退化点恰好落在 we → 丢弃（该时刻区域已失效）。
    """
    clipped: list[EpochInterval] = []
    for iv in intervals:
        if iv.end < window_start or iv.start >= window_end:
            continue
        start = max(iv.start, window_start)
        end = min(iv.end, window_end)
        if start > end:
            continue
        if start == end:
            # 退化点：必须严格落在窗口内（含左端 ws）；恰好等于 we 丢弃
            if not (window_start - EPS <= start < window_end - EPS):
                continue
            clipped.append(EpochInterval(start, start, False))
        else:
            exit_at_end = abs(end - window_end) <= EPS
            clipped.append(EpochInterval(start, end, exit_at_end))
    return clipped


def merge_intervals(intervals: list[EpochInterval]) -> list[EpochInterval]:
    """合并同一区域的穿越区间：重叠或在端点处相接即合并。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged: list[EpochInterval] = []
    cur_s, cur_e, cur_flag = ordered[0].start, ordered[0].end, ordered[0].exit_at_window_end
    for iv in ordered[1:]:
        if iv.start <= cur_e + EPS:
            # 重叠或端点相接 → 合并；右边界推进时沿用新区间的截断标记
            if iv.end > cur_e:
                cur_e = iv.end
                cur_flag = iv.exit_at_window_end
        else:
            merged.append(EpochInterval(cur_s, cur_e, cur_flag))
            cur_s, cur_e, cur_flag = iv.start, iv.end, iv.exit_at_window_end
    merged.append(EpochInterval(cur_s, cur_e, cur_flag))
    return merged


# ---------------------------------------------------------------- 顶层核验

def verify_trajectory(
    traj: list[tuple[float, float, float]],
    regions: list[dict],
) -> list[dict]:
    """对整段连续轨迹核验所有已提交区域。

    traj: [(epoch, x, y), ...]，时刻严格递增。
    regions: [{"id", "name", "valid_from"(epoch), "valid_to"(epoch), "polygon": Polygon}]
    返回每个区域的穿越结果（保持 regions 顺序）。
    """
    traj_start, traj_end = traj[0][0], traj[-1][0]
    results: list[dict] = []

    for region in regions:
        ws = region["valid_from"]
        we = region["valid_to"]

        # 区域生效窗口与轨迹时间范围不相交。
        # we == traj_start、ws == traj_end 时是否成立取决于端点开闭，
        # 交由窗口裁剪统一决定，这里只排除时间上严格不相交的情况。
        if we < traj_start or ws > traj_end:
            results.append({
                "region_id": region["id"],
                "region_name": region["name"],
                "crossings": [],
            })
            continue

        raw: list[EpochInterval] = []
        for (t0, x0, y0), (t1, x1, y1) in zip(traj, traj[1:]):
            for u0, u1 in segment_intersection_params(
                x0, y0, x1, y1, region["polygon"]
            ):
                raw.append(EpochInterval(t0 + u0 * (t1 - t0), t0 + u1 * (t1 - t0)))

        clipped = _clip_with_window(raw, ws, we)
        merged = merge_intervals(clipped)
        results.append({
            "region_id": region["id"],
            "region_name": region["name"],
            "crossings": [
                {
                    "entry_time": iv.start,
                    "exit_time": iv.end,
                    "entry_inclusive": True,
                    "exit_inclusive": not iv.exit_at_window_end,
                    "point_contact": abs(iv.end - iv.start) <= EPS,
                }
                for iv in merged
            ],
        })
    return results


# ---------------------------------------------------------------- 出发时刻调度

def contact_intervals_relative(
    traj: list[tuple[float, float, float]], polygon: Polygon
) -> list[tuple[float, float]]:
    """固定轨迹（相对秒数轴）与单个多边形的全部接触区间。

    返回按起点排序、相接/重叠已合并的 [a, b]（几何含边界）闭区间列表，
    其中 a == b 表示单点接触。时刻为相对首点的秒数。
    """
    raw: list[EpochInterval] = []
    for (t0, x0, y0), (t1, x1, y1) in zip(traj, traj[1:]):
        for u0, u1 in segment_intersection_params(x0, y0, x1, y1, polygon):
            raw.append(EpochInterval(t0 + u0 * (t1 - t0), t0 + u1 * (t1 - t0)))
    merged = merge_intervals(raw)
    return [(iv.start, iv.end) for iv in merged]


def _region_forbidden_departures(
    traj: list[tuple[float, float, float]], region: dict
) -> list[tuple[float, float]]:
    """单个区域导致的禁止出发时刻集合（左闭右开，已合并）。"""
    vf = region["valid_from"]
    vt = region["valid_to"]
    raw: list[EpochInterval] = []
    for a, b in contact_intervals_relative(traj, region["polygon"]):
        lo, hi = vf - b, vt - a
        if hi - lo > EPS:
            raw.append(EpochInterval(lo, hi))
    merged = merge_intervals(raw)
    return [(iv.start, iv.end) for iv in merged]


def forbidden_departure_intervals(
    traj: list[tuple[float, float, float]], regions: list[dict]
) -> list[tuple[float, float]]:
    """所有区域导致的禁止出发时刻集合（UTC epoch 秒轴上的左闭右开区间）。

    设出发时刻为 s，轨迹在相对时刻 τ 位于接触几何区间 [a, b]（闭），
    区域生效窗口为 [vf, vt)（左闭右开），则该接触造成违规当且仅当
        存在 τ∈[a,b] 使 vf <= s+τ < vt
        ⟺ s ∈ [vf - b, vt - a) （半开区间）。
    该公式对正长度接触与单点接触（a == b）统一成立；退化点恰落在
    valid_to 时 (s = vt - a) 为开端点，天然不计违规。

    不同区域、不同接触段产生的半开区间取并集：相接或重叠即合并。
    """
    raw: list[EpochInterval] = []
    for region in regions:
        raw.extend(
            EpochInterval(lo, hi)
            for lo, hi in _region_forbidden_departures(traj, region)
        )
    merged = merge_intervals(raw)
    return [(iv.start, iv.end) for iv in merged]


def _subtract_forbidden(
    forbidden: list[tuple[float, float]],
    window_start: float,
    window_end: float,
) -> list[dict]:
    """候选窗口（左闭右开）扣除已合并的禁止区间（左闭右开），返回安全出发区间。

    结果按起点排序，端点均为「左闭右开」；无可行时返回 []。
    """
    if window_end <= window_start:
        return []

    safe: list[dict] = []
    cursor = window_start
    for lo, hi in forbidden:
        if hi <= cursor + EPS:
            # 整段禁止区间已在游标之前
            continue
        if lo >= window_end - EPS:
            # 禁止区间起点已到/越过窗口右端，后面都是安全的
            break
        if lo > cursor + EPS:
            safe.append({
                "start": cursor,
                "end": min(lo, window_end),
                "start_inclusive": True,
                "end_inclusive": False,
            })
        if hi > cursor:
            cursor = hi

    # 禁止区间可能远跨过窗口右端：尾部安全段是否存在由游标位置统一决定
    if cursor < window_end - EPS:
        safe.append({
            "start": cursor,
            "end": window_end,
            "start_inclusive": True,
            "end_inclusive": False,
        })
    return safe


def safe_departure_intervals(
    traj: list[tuple[float, float, float]],
    regions: list[dict],
    window_start: float,
    window_end: float,
) -> list[dict]:
    """在候选出发时段 [window_start, window_end) 内计算全部安全出发区间。

    安全集合 = 候选窗口（左闭右开）减去禁止出发区间的并集（左闭右开）。
    结果按起点排序，端点均为「左闭右开」；无可行时返回 []。
    """
    if window_end <= window_start:
        return []
    return _subtract_forbidden(
        forbidden_departure_intervals(traj, regions), window_start, window_end
    )


# ---------------------------------------------------------------- 最小区域豁免预演

def minimal_exemption_plan(
    traj: list[tuple[float, float, float]],
    regions: list[dict],
    candidate_ids: list[int],
    window_start: float,
    window_end: float,
) -> dict:
    """最小区域豁免预演：只在本次计算中忽略被选区域，找出使候选窗口内
    至少存在一个安全出发时刻的豁免方案（纯函数，不产生任何副作用）。

    方案选择顺序：豁免区域数最少 → 该方案下最早安全时刻最早 →
    豁免编号升序序列字典序最小。原本安全时豁免集为空集；
    豁免全部候选区域仍无安全时刻时不可行。

    traj: [(t_seconds, x, y), ...]（相对秒数轨迹，已校验）；
    regions: 同一已提交快照中的全部区域（几何 dict）；
    candidate_ids: 可豁免区域编号（调用方保证互异且都存在于 regions）。

    返回 {
      "feasible": bool,
      "exempted": [region_id, ...]（升序；不可行时为 None）,
      "earliest": 该方案最早安全出发时刻 epoch 秒（不可行时为 None）,
      "intervals": 该方案完整安全出发区间（与 safe_departure_intervals 同结构）,
    }
    """
    candidate_set = set(candidate_ids)
    # 逐区域禁止出发区间（与 /api/schedule 同一公式：[vf-b, vt-a)，左闭右开）
    per_region: dict[int, list[tuple[float, float]]] = {
        region["id"]: _region_forbidden_departures(traj, region)
        for region in regions
    }
    # 不在候选中的区域任何方案都无法豁免，其禁止区间先从窗口扣除
    base: list[tuple[float, float]] = []
    for rid, ivs in per_region.items():
        if rid not in candidate_set:
            base.extend(ivs)
    base_merged = [
        (iv.start, iv.end)
        for iv in merge_intervals([EpochInterval(s, e) for s, e in base])
    ]
    workable = _subtract_forbidden(base_merged, window_start, window_end)
    if not workable:
        # 不可豁免区域已覆盖整个窗口：豁免候选区域也无济于事
        return {"feasible": False, "exempted": None, "earliest": None, "intervals": []}

    cand_ids = sorted(candidate_set)
    cand_forbidden = {rid: per_region.get(rid, []) for rid in cand_ids}

    # 用全部区间端点把可工作窗口切成「等覆盖」基本段（半开）：
    # 段内任意时刻被同一组候选区域禁止，豁免代价在段内为常数
    bounds: set[float] = set()
    for seg in workable:
        bounds.add(seg["start"])
        bounds.add(seg["end"])
    for rid in cand_ids:
        for lo, hi in cand_forbidden[rid]:
            bounds.add(lo)
            bounds.add(hi)
    pts = sorted(bounds)

    spans = [(seg["start"], seg["end"]) for seg in workable]
    segments: list[tuple[float, float, frozenset]] = []
    for a, b in zip(pts, pts[1:]):
        if b - a <= EPS:
            # 与安全区间引擎一致：宽度不超过 EPS 的碎段不产生安全时刻
            continue
        if not any(ws <= a < we for ws, we in spans):
            continue
        # 禁止区间左闭右开：段左端点的覆盖即整段覆盖
        cover = frozenset(
            rid
            for rid in cand_ids
            if any(lo <= a < hi for lo, hi in cand_forbidden[rid])
        )
        segments.append((a, b, cover))

    if not segments:
        return {"feasible": False, "exempted": None, "earliest": None, "intervals": []}

    # 方案 S 可行 ⟺ 存在某基本段，其覆盖集 ⊆ S（该段即安全）；
    # 最少豁免数 = 各基本段覆盖集的最小大小，达到该大小的可行方案
    # 恰为最小覆盖集本身（|S|=k* 且覆盖集 ⊆ S ⟹ S 等于该覆盖集）
    k_star = min(len(cover) for _, _, cover in segments)
    plans = sorted(
        {tuple(sorted(cover)) for _, _, cover in segments if len(cover) == k_star}
    )

    best: tuple[tuple[float, tuple], tuple, list[dict]] | None = None
    for plan in plans:
        plan_set = set(plan)
        forbid = list(base_merged)
        for rid in cand_ids:
            if rid not in plan_set:
                forbid.extend(cand_forbidden[rid])
        merged = [
            (iv.start, iv.end)
            for iv in merge_intervals([EpochInterval(s, e) for s, e in forbid])
        ]
        intervals = _subtract_forbidden(merged, window_start, window_end)
        if not intervals:
            continue  # 覆盖论证保证不会发生；防御性跳过
        key = (intervals[0]["start"], plan)
        if best is None or key < best[0]:
            best = (key, plan, intervals)

    if best is None:
        return {"feasible": False, "exempted": None, "earliest": None, "intervals": []}
    (_, plan, intervals) = best
    return {
        "feasible": True,
        "exempted": list(plan),
        "earliest": intervals[0]["start"],
        "intervals": intervals,
    }
