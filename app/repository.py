"""区域数据访问：CRUD、一致性快照读取与批量原子变更。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from shapely.geometry import Polygon
from shapely.wkt import dumps as wkt_dumps

from . import geometry
from .schemas import (
    RegionCreateInBatch,
    RegionDeleteInBatch,
    RegionUpdateInBatch,
)


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


# ---------------------------------------------------------------- 批量变更异常

class BatchRevisionConflict(Exception):
    """expected_revision 与库内当前 revision 不符。"""

    def __init__(self, expected: int, current: int):
        super().__init__(f"revision 已过期：expected={expected}, current={current}")
        self.expected = expected
        self.current = current


class BatchRegionsNotFound(Exception):
    """批内引用了不存在的区域。"""

    def __init__(self, missing: list[dict]):
        super().__init__("批内存在不存在的区域")
        self.missing = missing


class BatchDuplicateRegion(Exception):
    """同一现有区域在一批操作中出现多次。"""

    def __init__(self, region_id: int, indices: list[int]):
        super().__init__(f"区域 {region_id} 在同一批中出现了多次")
        self.region_id = region_id
        self.indices = indices


class BatchInvalidOperation(Exception):
    """批内单条操作的数据不合法（如生效时段不递增）。"""

    def __init__(self, index: int, message: str, field: str | None = None):
        super().__init__(message)
        self.index = index
        self.message = message
        self.field = field


class BatchUnsafeTrajectories(Exception):
    """变更后集合下存在不安全轨迹；携带逐轨迹核验明细。"""

    def __init__(self, trajectory_results: list[dict]):
        super().__init__("存在轨迹在变更后区域集合下发生穿越")
        self.trajectory_results = trajectory_results


class PassUnsafe(Exception):
    """待签发轨迹在当前区域集合下发生穿越；携带逐区域穿越明细。"""

    def __init__(self, results: list[dict]):
        super().__init__("轨迹在当前区域集合下发生穿越，凭证未签发")
        self.results = results


class PassConflict(Exception):
    """区域变更将使未取消且未结束的凭证轨迹不再安全；携带冲突凭证 id。"""

    def __init__(self, pass_ids: list[int]):
        super().__init__(
            f"变更后区域集合与 {len(pass_ids)} 张未取消通行凭证冲突，整笔变更已回滚"
        )
        self.pass_ids = pass_ids


class CommitmentUnsafe(Exception):
    """候选窗口内当前不存在任何安全出发时刻；承诺拒签且不落库。"""


class CommitmentConflict(Exception):
    """区域变更将抹去承诺在窗口结束前的全部安全出发时刻；携带冲突承诺 id。"""

    def __init__(self, commitment_ids: list[int]):
        super().__init__(
            f"变更后区域集合与 {len(commitment_ids)} 张未取消择时通行承诺冲突，"
            "其窗口结束前将不再有任何安全出发时刻，整笔变更已回滚"
        )
        self.commitment_ids = commitment_ids


# ---------------------------------------------------------------- 承诺兑现

class CommitmentAlreadyFulfilled(Exception):
    """承诺已被兑现（与普通取消相区分）；携带已生成的通行凭证 id。"""

    def __init__(self, pass_id: int):
        super().__init__(f"承诺已被兑现为通行凭证 {pass_id}")
        self.pass_id = pass_id


class CommitmentExplicitlyCancelled(Exception):
    """承诺已被普通取消（非兑现），不能兑现。"""


class CommitmentExpired(Exception):
    """候选窗口已结束（window_end 不晚于受理时刻），承诺过期不能兑现。"""


class DepartureNotInFuture(Exception):
    """出发时刻不晚于服务端受理时刻（拿到写锁后的当前时间）。"""


class DepartureOutOfWindow(Exception):
    """出发时刻落在承诺原候选窗口 [window_start, window_end) 之外。"""


class DepartureUnsafe(Exception):
    """出发时刻不在按当前区域集合重算的安全出发区间内；携带当前安全区间。"""

    def __init__(self, safe_intervals: list[dict]):
        super().__init__("出发时刻不在当前安全出发区间内")
        self.safe_intervals = safe_intervals


class IdempotencyKeyConflict(Exception):
    """同一幂等键被内容不同的请求复用（操作类型/目标区域/请求内容不一致）。"""

    def __init__(self, key: str):
        super().__init__("幂等键已被另一份内容不同的请求占用，本次不变更任何数据")
        self.key = key


class ExemptionRevisionConflict(Exception):
    """豁免预演：expected_revision 与库内当前 revision 不符。"""

    def __init__(self, expected: int, current: int):
        super().__init__(f"revision 已过期：expected={expected}, current={current}")
        self.expected = expected
        self.current = current


class ExemptionRegionsNotFound(Exception):
    """豁免预演：候选豁免编号引用了不存在的区域。"""

    def __init__(self, missing: list[int]):
        super().__init__("候选豁免编号包含不存在的区域")
        self.missing = missing


class ExemptionDuplicateRegions(Exception):
    """豁免预演：候选豁免编号存在重复。"""

    def __init__(self, duplicates: list[int]):
        super().__init__("候选豁免编号存在重复")
        self.duplicates = duplicates


def _row_to_region(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "polygon": row["polygon"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _region_row_to_geom(row: dict) -> dict:
    """把含 wkt/valid_from/valid_to 的行转成几何核验用的区域 dict。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "valid_from": _epoch(row["valid_from"]),
        "valid_to": _epoch(row["valid_to"]),
        "polygon": geometry.polygon_from_wkt(row["wkt"]),
    }


def get_revision(cur) -> int:
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    return cur.fetchone()["revision"]


def get_revision_shared(cur) -> int:
    cur.execute("SELECT revision FROM region_meta WHERE id = 1")
    return cur.fetchone()["revision"]


def bump_revision(cur) -> int:
    """在写事务内推进全局版本号（调用方应已持有该行行锁）。"""
    cur.execute(
        "UPDATE region_meta SET revision = revision + 1 WHERE id = 1 "
        "RETURNING revision"
    )
    return cur.fetchone()["revision"]


def create_region(cur, payload, polygon_vertices: list[list[float]]) -> tuple[dict, int]:
    from shapely.geometry import Polygon

    wkt = wkt_dumps(Polygon(polygon_vertices), rounding_precision=-1)
    # 锁定元数据行，使区域写入串行化并拿到稳定的新版本号
    get_revision(cur)
    # 当前完整集合（行锁在 meta 行上，期间不存在任何并发写）；
    # 变更前集合用于判定承诺「原本有、变更后没有」的破坏语义
    current_regions = [
        _region_row_to_geom(r) for r in _load_all_regions_locked(cur).values()
    ]
    # 变更后的完整集合必须仍保证未取消且未结束的凭证安全、
    # 且不得抹去未结束承诺窗口内的全部安全出发时刻
    candidate = list(current_regions)
    candidate.append({
        "id": 0,  # 新区域尚未分配 id；核验只使用几何与生效时段
        "name": payload.name,
        "valid_from": _epoch(payload.valid_from),
        "valid_to": _epoch(payload.valid_to),
        "polygon": Polygon(polygon_vertices),
    })
    now = utcnow()
    check_passes_safe(cur, candidate, now)
    check_commitments_safe(cur, current_regions, candidate, now)
    cur.execute(
        """
        INSERT INTO regions (name, valid_from, valid_to, polygon, wkt)
        VALUES (%(name)s, %(valid_from)s, %(valid_to)s, %(polygon)s::jsonb, %(wkt)s)
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "name": payload.name,
            "valid_from": payload.valid_from,
            "valid_to": payload.valid_to,
            "polygon": json.dumps(polygon_vertices),
            "wkt": wkt,
        },
    )
    row = cur.fetchone()
    revision = bump_revision(cur)
    record_observation_flips(cur, current_regions, candidate, revision, now)
    # 审计记录与区域写入、revision 推进、观察事件同一事务提交
    record_region_change(
        cur, revision, "create", [row["id"]],
        _single_change_summary("create", row["id"], row["version"]),
    )
    return _row_to_region(row), revision


def list_regions(cur) -> list[dict]:
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at
        FROM regions
        ORDER BY id
        """
    )
    return [_row_to_region(r) for r in cur.fetchall()]


def get_region_for_update(cur, region_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at, wkt
        FROM regions WHERE id = %s
        FOR UPDATE
        """,
        (region_id,),
    )
    return cur.fetchone()


def update_region(cur, region_id: int, payload, new_polygon) -> tuple[dict, int] | None:
    # 写路径统一按「先全局 meta 行、后区域行」加锁，与批量变更保持同一加锁顺序，
    # 避免不同写事务之间出现交叉等待（死锁）
    get_revision(cur)
    row = get_region_for_update(cur, region_id)
    if row is None:
        return None

    name = payload.name if payload.name is not None else row["name"]
    valid_from = payload.valid_from if payload.valid_from is not None else row["valid_from"]
    valid_to = payload.valid_to if payload.valid_to is not None else row["valid_to"]
    if valid_from >= valid_to:
        raise ValueError("生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）")

    if new_polygon is not None:
        from shapely.geometry import Polygon

        polygon_json = new_polygon
        wkt = wkt_dumps(Polygon(new_polygon), rounding_precision=-1)
    else:
        polygon_json = row["polygon"]
        wkt = row["wkt"]

    # 变更后的完整集合必须仍保证未取消且未结束的凭证安全，
    # 且不得抹去未结束承诺窗口内的全部安全出发时刻
    current = _load_all_regions_locked(cur)
    current_regions = [
        _region_row_to_geom(current[rid]) for rid in sorted(current)
    ]
    candidate = []
    for rid in sorted(current):
        if rid == region_id:
            candidate.append({
                "id": region_id,
                "name": name,
                "valid_from": _epoch(valid_from),
                "valid_to": _epoch(valid_to),
                "polygon": geometry.polygon_from_wkt(wkt),
            })
        else:
            candidate.append(_region_row_to_geom(current[rid]))
    now = utcnow()
    check_passes_safe(cur, candidate, now)
    check_commitments_safe(cur, current_regions, candidate, now)

    cur.execute(
        """
        UPDATE regions SET
            name = %(name)s,
            valid_from = %(valid_from)s,
            valid_to = %(valid_to)s,
            polygon = %(polygon)s::jsonb,
            wkt = %(wkt)s,
            version = version + 1,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "id": region_id,
            "name": name,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "polygon": json.dumps(polygon_json),
            "wkt": wkt,
        },
    )
    updated = _row_to_region(cur.fetchone())
    revision = bump_revision(cur)
    record_observation_flips(cur, current_regions, candidate, revision, now)
    # 审计记录与区域写入、revision 推进、观察事件同一事务提交
    record_region_change(
        cur, revision, "update", [region_id],
        _single_change_summary("update", region_id, updated["version"]),
    )
    return updated, revision


def delete_region(cur, region_id: int) -> tuple[bool, int | None]:
    # 与其它写路径一致：先锁全局 meta 行，再锁/删区域行
    get_revision(cur)
    cur.execute(
        """
        SELECT id, version FROM regions WHERE id = %s
        FOR UPDATE
        """,
        (region_id,),
    )
    existing = cur.fetchone()
    if existing is None:
        return False, None
    deleted_version = int(existing["version"])
    # 变更后的完整集合（移除该区域）必须仍保证未取消且未结束的凭证安全；
    # 删除只会扩大安全集合，承诺不可能被破坏，但仍沿用同一检查契约
    all_current = _load_all_regions_locked(cur)
    before_regions = [_region_row_to_geom(r) for r in all_current.values()]
    current_regions = [
        _region_row_to_geom(r)
        for rid, r in all_current.items()
        if rid != region_id
    ]
    now = utcnow()
    check_passes_safe(cur, current_regions, now)
    check_commitments_safe(cur, current_regions, current_regions, now)
    cur.execute("DELETE FROM regions WHERE id = %s", (region_id,))
    revision = bump_revision(cur)
    record_observation_flips(cur, before_regions, current_regions, revision, now)
    # 审计记录与删除、revision 推进、观察事件同一事务提交（含删除前 version）
    record_region_change(
        cur, revision, "delete", [region_id],
        _single_change_summary("delete", region_id, deleted_version),
    )
    return True, revision


def load_snapshot(cur) -> tuple[int, list[dict]]:
    """在 REPEATABLE READ 只读事务中读取同一已提交版本的区域集合。

    返回 (revision, regions)；regions 内已构造好 Shapely Polygon。
    """
    revision = get_revision_shared(cur)
    cur.execute(
        """
        SELECT id, name, valid_from, valid_to, wkt
        FROM regions
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    regions = []
    for row in rows:
        regions.append({
            "id": row["id"],
            "name": row["name"],
            "valid_from": _epoch(row["valid_from"]),
            "valid_to": _epoch(row["valid_to"]),
            "polygon": geometry.polygon_from_wkt(row["wkt"]),
        })
    return revision, regions


def configure_cursor(cur) -> None:
    cur.row_factory = dict_row


# ---------------------------------------------------------------- 幂等键

def idempotency_claim(cur, key: str, fingerprint: str) -> dict | None:
    """在写事务内占用幂等键（与后续业务变更同一事务）。

    返回 None：本事务成功占位，调用方应继续执行业务操作，并在成功时
    idempotency_store 回填响应；业务失败回滚则占位行一并消失（不占键）。
    返回 dict（含 status_code/response_body）：同键同内容的请求已成功
    提交过，调用方应直接回放原状态与原响应，不再执行任何变更。
    同键但内容不同：抛 IdempotencyKeyConflict，调用方回滚，不变更任何数据。

    并发同键请求在主键上互斥：后到的 INSERT 会等待先到的提交或回滚，
    因此同一键全库只会有一次实际提交。
    """
    cur.execute(
        """
        INSERT INTO idempotency_keys (key, fingerprint)
        VALUES (%s, %s)
        ON CONFLICT (key) DO NOTHING
        RETURNING key
        """,
        (key, fingerprint),
    )
    if cur.fetchone() is not None:
        return None
    # 键已被占用：占位行与响应回填在同一事务提交，这里读到的必然是完整记录
    cur.execute(
        """
        SELECT fingerprint, status_code, response_body
        FROM idempotency_keys WHERE key = %s
        """,
        (key,),
    )
    row = cur.fetchone()
    if row["fingerprint"] != fingerprint:
        raise IdempotencyKeyConflict(key)
    return row


def idempotency_store(cur, key: str, status_code: int, response_body: str) -> None:
    """把首次成功提交的原 HTTP 状态码与响应体（JSON 原文）写回占位行
    （与业务变更同事务）。"""
    cur.execute(
        """
        UPDATE idempotency_keys
        SET status_code = %s, response_body = %s
        WHERE key = %s
        """,
        (status_code, response_body, key),
    )


# ---------------------------------------------------------------- 批量原子变更

def _load_all_regions_locked(cur) -> dict[int, dict]:
    """读取全部区域（调用方应已持有 region_meta 行锁，保证期间无并发写）。"""
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at, wkt
        FROM regions
        ORDER BY id
        """
    )
    return {r["id"]: r for r in cur.fetchall()}


def _candidate_regions(
    current: dict[int, dict], op_results: list[dict]
) -> list[dict]:
    """根据已计算好的操作结果构造变更后完整区域集合（供轨迹核验）。"""
    regions: list[dict] = []
    for op in op_results:
        if op["action"] == "delete":
            continue
        regions.append(_region_row_to_geom(op["_row"]))  # create/update 均为落库后的候选行
    # 未被本批涉及的现有区域原样保留
    touched = {op["region_id"] for op in op_results}
    for rid in sorted(current):
        if rid not in touched:
            regions.append(_region_row_to_geom(current[rid]))
    regions.sort(key=lambda r: r["id"])
    return regions


def _check_batch_operation_targets(operations) -> dict[int, int]:
    """同一现有区域在一批中只能出现一次（create 不占现有 id）。

    返回 {region_id: 首次出现的操作下标}；重复时抛 BatchDuplicateRegion。
    """
    seen: dict[int, int] = {}
    for i, op in enumerate(operations):
        if isinstance(op, (RegionUpdateInBatch, RegionDeleteInBatch)):
            rid = op.region_id
            if rid in seen:
                raise BatchDuplicateRegion(rid, [seen[rid], i])
            seen[rid] = i
    return seen


def _missing_batch_regions(seen: dict[int, int], known: set[int]) -> list[dict]:
    """按操作下标顺序列出批内引用但并不存在的区域。"""
    return [
        {"index": i, "region_id": rid}
        for rid, i in sorted(seen.items(), key=lambda kv: kv[1])
        if rid not in known
    ]


def _plan_batch_operations(operations, current: dict[int, dict]) -> list[dict]:
    """把每条操作展开为「落库后的行」（纯内存计算，不写库、不分配编号）。

    返回 [{index, action, region_id, version, _row}]：
    * create 的 region_id 与 _row["id"] 暂为 None，由调用方决定去向——
      正式提交从序列预分配真实 id；影响预演保持 None，以操作下标标识；
    * update/delete 的 region_id 为现有区域编号。
    单条操作语义非法（如修订后 valid_from >= valid_to）抛 BatchInvalidOperation。
    """
    op_results: list[dict] = []
    for i, op in enumerate(operations):
        if isinstance(op, RegionCreateInBatch):
            try:
                op.validate_time_order()
            except ValueError as exc:
                raise BatchInvalidOperation(i, str(exc), "valid_from") from exc
            polygon = op.polygon
            wkt = wkt_dumps(Polygon(polygon), rounding_precision=-1)
            op_results.append({
                "index": i,
                "action": "create",
                "region_id": None,
                "version": 1,
                "_row": {
                    "id": None,
                    "name": op.name,
                    "polygon": polygon,
                    "wkt": wkt,
                    "valid_from": op.valid_from,
                    "valid_to": op.valid_to,
                    "version": 1,
                },
            })
        elif isinstance(op, RegionUpdateInBatch):
            row = current[op.region_id]
            name = op.name if op.name is not None else row["name"]
            valid_from = op.valid_from if op.valid_from is not None else row["valid_from"]
            valid_to = op.valid_to if op.valid_to is not None else row["valid_to"]
            if valid_from >= valid_to:
                raise BatchInvalidOperation(
                    i,
                    "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）",
                    "valid_from",
                )
            if op.polygon is not None:
                polygon = op.polygon
                wkt = wkt_dumps(Polygon(polygon), rounding_precision=-1)
            else:
                polygon = row["polygon"]
                wkt = row["wkt"]
            new_version = int(row["version"]) + 1
            op_results.append({
                "index": i,
                "action": "update",
                "region_id": op.region_id,
                "version": new_version,
                "_row": {
                    "id": op.region_id,
                    "name": name,
                    "polygon": polygon,
                    "wkt": wkt,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "version": new_version,
                },
            })
        else:  # delete
            row = current[op.region_id]
            op_results.append({
                "index": i,
                "action": "delete",
                "region_id": op.region_id,
                "version": int(row["version"]),
                "_row": None,
            })
    return op_results


def apply_batch(cur, payload, trajectories: list[list[tuple[float, float, float]]]) -> dict:
    """在一个已开启的读/写事务中原子地：校验 revision → 校验操作 → 核验轨迹 → 落库。

    payload: RegionBatchRequest（多边形/时刻已由 Pydantic 校验归一化）。
    trajectories: 与 payload.trajectories 对应的已校验轨迹
                  [(epoch, x, y), ...]，时刻严格递增。
    成功返回 {"revision", "operations", "trajectory_results"}；
    失败抛 Batch* 异常，调用方负责回滚，整个批次不落库。
    """
    # 1) 锁全局元数据行：与所有单条登记/修订/删除互斥，写操作在此串行化
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    current_revision = cur.fetchone()["revision"]
    if current_revision != payload.expected_revision:
        raise BatchRevisionConflict(payload.expected_revision, current_revision)

    operations = payload.operations

    # 2) 同一现有区域在一批中只能出现一次（create 不占现有 id）
    seen = _check_batch_operation_targets(operations)

    # 3) 按 id 升序锁定本批涉及的现有区域行（与所有单条写路径同一加锁顺序：
    #    先 meta 行、后区域行；区域行一律按 id 升序，杜绝多写事务死锁）
    referenced = sorted(seen)
    if referenced:
        cur.execute(
            "SELECT id FROM regions WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (referenced,),
        )
        locked = {r["id"] for r in cur.fetchall()}
    else:
        locked = set()
    missing = _missing_batch_regions(seen, locked)
    if missing:
        raise BatchRegionsNotFound(missing)

    # 4) 加载当前完整集合（行已锁、meta 行已锁，期间不存在任何并发写）
    current = _load_all_regions_locked(cur)

    # 5) 在内存中构造每条操作落库后的行（先不写库），并给 create 预分配真实 id
    op_results = _plan_batch_operations(operations, current)
    for op in op_results:
        if op["action"] == "create":
            cur.execute("SELECT nextval(pg_get_serial_sequence('regions', 'id')) AS id")
            new_id = int(cur.fetchone()["id"])
            op["region_id"] = new_id
            op["_row"]["id"] = new_id

    # 6) 构造变更后完整集合，按现有直线匀速/边界接触即穿越/左闭右开规则核验
    candidate = _candidate_regions(current, op_results)
    all_checks: list[dict] = []
    unsafe: list[dict] = []
    for ti, traj in enumerate(trajectories):
        results = geometry.verify_trajectory(traj, candidate)
        safe = all(len(r["crossings"]) == 0 for r in results)
        check = {"index": ti, "safe": safe, "results": results}
        all_checks.append(check)
        if not safe:
            unsafe.append(check)
    if unsafe:
        # 不落库；region_meta 行锁随回滚释放
        raise BatchUnsafeTrajectories(all_checks)

    # 6.5) 未取消且末点时刻未过的凭证，其轨迹在变更后完整集合下仍须安全；
    #      任一冲突则整批回滚（409 pass_conflict），version/revision 不变。
    #      未取消且窗口未结束的择时承诺同样约束本批：仅当变更前窗口内仍有
    #      未来安全时刻、变更后一点不剩时才冲突（409 commitment_conflict）
    before_regions = [_region_row_to_geom(current[rid]) for rid in sorted(current)]
    now = utcnow()
    check_passes_safe(cur, candidate, now)
    check_commitments_safe(cur, before_regions, candidate, now)

    # 7) 全部轨迹安全：按操作顺序落库
    for op in op_results:
        if op["action"] == "create":
            row = op["_row"]
            cur.execute(
                """
                INSERT INTO regions (id, name, valid_from, valid_to, polygon, wkt, version)
                VALUES (%(id)s, %(name)s, %(valid_from)s, %(valid_to)s,
                        %(polygon)s::jsonb, %(wkt)s, %(version)s)
                """,
                {
                    "id": row["id"],
                    "name": row["name"],
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "polygon": json.dumps(row["polygon"]),
                    "wkt": row["wkt"],
                    "version": row["version"],
                },
            )
        elif op["action"] == "update":
            row = op["_row"]
            cur.execute(
                """
                UPDATE regions SET
                    name = %(name)s,
                    valid_from = %(valid_from)s,
                    valid_to = %(valid_to)s,
                    polygon = %(polygon)s::jsonb,
                    wkt = %(wkt)s,
                    version = %(version)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    "id": row["id"],
                    "name": row["name"],
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "polygon": json.dumps(row["polygon"]),
                    "wkt": row["wkt"],
                    "version": row["version"],
                },
            )
        else:  # delete
            cur.execute("DELETE FROM regions WHERE id = %s", (op["region_id"],))

    # 8) 整批全局 revision 只递增一次
    new_revision = bump_revision(cur)
    # 9) 同一事务内为结论翻转的观察追加事件（整批对同一观察至多一条）
    record_observation_flips(cur, before_regions, candidate, new_revision, now)

    public_ops = [
        {k: v for k, v in op.items() if not k.startswith("_")}
        for op in op_results
    ]
    # 整批只有一行审计：涉及区域编号按请求操作顺序，摘要为逐操作与类型计数。
    # 与整批落库、revision 推进、观察事件同一事务提交（失败回滚则一并消失）
    record_region_change(
        cur,
        new_revision,
        "batch",
        [op["region_id"] for op in public_ops],
        _batch_change_summary(public_ops),
    )
    return {
        "revision": new_revision,
        "operations": public_ops,
        "trajectory_results": all_checks,
    }


# ---------------------------------------------------------------- 批量变更影响预演（只读）

def _preview_candidate_regions(
    current: dict[int, dict], op_results: list[dict]
) -> list[dict]:
    """预演用的变更后完整区域集合：create 不分配编号，以操作下标标识。

    返回的几何区域 dict 额外携带 "operation_index"（拟登记区域为操作下标，
    其余为 None）；排序为现有区域按 id 升序在前、拟登记区域按操作下标
    升序在后，保证报告顺序确定。
    """
    regions: list[dict] = []
    touched: set[int] = set()
    for op in op_results:
        if op["action"] == "delete":
            touched.add(op["region_id"])
            continue
        geom = _region_row_to_geom(op["_row"])
        if op["action"] == "create":
            geom["operation_index"] = op["index"]
        else:
            geom["operation_index"] = None
            touched.add(op["region_id"])
        regions.append(geom)
    # 未被本批涉及的现有区域原样保留
    for rid in sorted(current):
        if rid not in touched:
            geom = _region_row_to_geom(current[rid])
            geom["operation_index"] = None
            regions.append(geom)
    regions.sort(key=lambda r: (
        r["operation_index"] is not None,
        r["operation_index"] if r["operation_index"] is not None else r["id"],
    ))
    return regions


def preview_batch(cur, payload, trajectories: list[list[tuple[float, float, float]]]) -> dict:
    """批量变更的只读影响预演：与 apply_batch 同一套校验与核验规则，但不落库。

    在调用方开启的 REPEATABLE READ READ ONLY 事务内执行：revision、区域
    集合、凭证、承诺与观察均来自同一已提交快照；凭证/承诺/观察的有效性
    判定与承诺安全区间裁剪共用同一基准时刻 now。错误约定与正式批量一致
    （revision 不符、重复/未知区域、非法操作抛同一批 Batch* 异常）；
    与正式提交不同，轨迹不安全、凭证冲突、承诺被清空与观察结论翻转都
    只写入报告，不抛错。create 不占用序列编号，报告中以操作下标标识。

    返回 {"revision", "now", "trajectory_results", "pass_conflicts",
          "commitment_conflicts", "observation_flips"}。
    """
    # 1) 只读快照内的当前 revision（与区域集合同一已提交版本）
    current_revision = get_revision_shared(cur)
    if current_revision != payload.expected_revision:
        raise BatchRevisionConflict(payload.expected_revision, current_revision)

    operations = payload.operations

    # 2) 同一现有区域在一批中只能出现一次（create 不占现有 id）
    seen = _check_batch_operation_targets(operations)

    # 3) 引用的现有区域必须存在（只读快照内读取，无需加锁）
    current = _load_all_regions_locked(cur)
    missing = _missing_batch_regions(seen, set(current))
    if missing:
        raise BatchRegionsNotFound(missing)

    # 4) 在内存中构造每条操作落库后的行：create 保持无编号（不占用序列）
    op_results = _plan_batch_operations(operations, current)

    # 5) 变更后完整集合（拟登记区域以操作下标标识），逐轨迹核验并写入报告
    candidate = _preview_candidate_regions(current, op_results)
    all_checks: list[dict] = []
    for ti, traj in enumerate(trajectories):
        results = geometry.verify_trajectory(traj, candidate)
        # verify_trajectory 保持输入区域顺序：按位置补回操作下标标识
        for geom, res in zip(candidate, results):
            res["operation_index"] = geom["operation_index"]
        safe = all(len(r["crossings"]) == 0 for r in results)
        all_checks.append({"index": ti, "safe": safe, "results": results})

    # 6) 同一基准时刻下评估三类影响：凭证冲突、承诺被清空、观察结论净翻转
    before_regions = [_region_row_to_geom(current[rid]) for rid in sorted(current)]
    now = utcnow()
    pass_conflicts = _conflicting_pass_ids(cur, candidate, now)
    commitment_conflicts = _emptied_commitment_ids(
        cur, before_regions, candidate, now
    )
    observation_flips = _observation_flips(cur, before_regions, candidate, now)

    return {
        "revision": current_revision,
        "now": now,
        "trajectory_results": all_checks,
        "pass_conflicts": pass_conflicts,
        "commitment_conflicts": commitment_conflicts,
        "observation_flips": observation_flips,
    }


# ---------------------------------------------------------------- 区域集合变更审计

def record_region_change(
    cur,
    revision: int,
    change_type: str,
    region_ids: list[int],
    summary: dict,
) -> None:
    """在区域写事务内追加一条集合变更审计记录（与区域写入同一事务提交）。

    revision 为本次变更后的全局版本号；主键保证一次成功变更恰好一行。
    调用方事务回滚（校验/冲突失败）时审计行一并回滚；幂等重试只回放已
    提交响应、不会再次进入写路径，因此也不会重复追加。
    """
    cur.execute(
        """
        INSERT INTO region_change_audit
            (revision, change_type, region_ids, summary)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (revision, change_type, region_ids, json.dumps(summary)),
    )


def _single_change_summary(
    action: str, region_id: int, version: int
) -> dict:
    """单区登记/修订/删除的批量操作风格摘要。"""
    counts = {"create": 0, "update": 0, "delete": 0}
    counts[action] = 1
    return {
        "operations": [
            {"index": 0, "action": action, "region_id": region_id, "version": version}
        ],
        "total": 1,
        "create_count": counts["create"],
        "update_count": counts["update"],
        "delete_count": counts["delete"],
    }


def _batch_change_summary(public_ops: list[dict]) -> dict:
    """批量变更摘要：逐操作（保留请求下标）与各类型操作计数。"""
    counts = {"create": 0, "update": 0, "delete": 0}
    for op in public_ops:
        counts[op["action"]] += 1
    return {
        "operations": [
            {
                "index": op["index"],
                "action": op["action"],
                "region_id": op["region_id"],
                "version": op.get("version"),
            }
            for op in public_ops
        ],
        "total": len(public_ops),
        "create_count": counts["create"],
        "update_count": counts["update"],
        "delete_count": counts["delete"],
    }


def list_region_changes(
    cur, after_revision: int, limit: int
) -> tuple[list[dict], bool]:
    """按全局 revision 游标升序读取区域集合变更审计记录。

    返回 (changes, has_more)：取 revision > after_revision 的至多 limit+1 行，
    多取的一行仅用于判定是否还有下一页。revision 为单调递增且一次变更一行，
    配合 REPEATABLE READ 只读快照，分页遍历不会重复或遗漏。
    """
    cur.execute(
        """
        SELECT revision, change_type, region_ids, summary, occurred_at
        FROM region_change_audit
        WHERE revision > %s
        ORDER BY revision ASC
        LIMIT %s
        """,
        (after_revision, limit + 1),
    )
    rows = cur.fetchall()
    has_more = len(rows) > limit
    return rows[:limit], has_more


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 通行凭证

def load_constraining_passes(cur, now: datetime) -> list[dict]:
    """未取消且末点时刻未过的凭证（调用方应已持有 region_meta 行锁）。

    由于签发/取消与所有区域写入都在同一把 meta 行锁上串行化，本查询在
    锁内读到的是稳定集合：锁释放前不会有新的凭证提交或取消生效。
    """
    cur.execute(
        """
        SELECT id, trajectory
        FROM passes
        WHERE status = 'active' AND end_time > %s
        ORDER BY id
        """,
        (now,),
    )
    passes = []
    for row in cur.fetchall():
        traj = [
            (float(p["t"]), float(p["x"]), float(p["y"]))
            for p in row["trajectory"]
        ]
        passes.append({"id": row["id"], "trajectory": traj})
    return passes


def _conflicting_pass_ids(cur, regions: list[dict], now: datetime) -> list[int]:
    """未取消且末点时刻未过、且轨迹在给定区域集合下不再安全的凭证 id（升序）。

    沿用直线匀速、边界接触即违规、生效期左闭右开规则。
    """
    conflicts: list[int] = []
    for p in load_constraining_passes(cur, now):
        results = geometry.verify_trajectory(p["trajectory"], regions)
        if any(r["crossings"] for r in results):
            conflicts.append(p["id"])
    return conflicts


def check_passes_safe(cur, regions: list[dict], now: datetime) -> None:
    """核验未取消且未结束的凭证轨迹在给定区域集合下仍然安全。

    任一凭证发生穿越即抛 PassConflict，调用方应回滚整笔变更。
    """
    conflicts = _conflicting_pass_ids(cur, regions, now)
    if conflicts:
        raise PassConflict(conflicts)


def issue_pass(cur, traj: list[tuple[float, float, float]]) -> dict:
    """签发通行凭证：在持有 meta 行锁的事务内核验并持久化。

    traj: [(epoch, x, y), ...]，已校验（≥2 点、时刻严格递增）。
    首点必须晚于签发时刻（拿到写锁后的当前服务时间），否则 TrajectoryError；
    轨迹在当前完整区域集合下不安全时抛 PassUnsafe，不落库。
    """
    # 锁全局元数据行：与取消、所有区域写入串行化，核验与落库之间无人插队
    revision = get_revision(cur)
    now = utcnow()
    if traj[0][0] <= now.timestamp():
        raise geometry.TrajectoryError(
            "轨迹首点时刻必须晚于签发时刻（当前服务时间），已开始的轨迹不予签发"
        )
    regions = [
        _region_row_to_geom(r) for r in _load_all_regions_locked(cur).values()
    ]
    results = geometry.verify_trajectory(traj, regions)
    if any(r["crossings"] for r in results):
        raise PassUnsafe(results)
    cur.execute(
        """
        INSERT INTO passes (trajectory, start_time, end_time, status, issued_revision)
        VALUES (%(trajectory)s::jsonb, %(start)s, %(end)s, 'active', %(revision)s)
        RETURNING id, trajectory, status, issued_revision, created_at, cancelled_at
        """,
        {
            "trajectory": json.dumps(
                [{"x": x, "y": y, "t": t} for t, x, y in traj]
            ),
            "start": datetime.fromtimestamp(traj[0][0], tz=timezone.utc),
            "end": datetime.fromtimestamp(traj[-1][0], tz=timezone.utc),
            "revision": revision,
        },
    )
    return cur.fetchone()


def get_pass(cur, pass_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, trajectory, status, issued_revision, created_at, cancelled_at
        FROM passes WHERE id = %s
        """,
        (pass_id,),
    )
    return cur.fetchone()


def cancel_pass(cur, pass_id: int) -> dict | None:
    """取消凭证；重复取消幂等（已取消的直接返回现状）。未找到返回 None。"""
    # 与签发、所有区域写入在同一把 meta 行锁上串行化
    get_revision(cur)
    cur.execute(
        """
        SELECT id, trajectory, status, issued_revision, created_at, cancelled_at
        FROM passes WHERE id = %s
        FOR UPDATE
        """,
        (pass_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row["status"] == "active":
        cur.execute(
            """
            UPDATE passes SET status = 'cancelled', cancelled_at = now()
            WHERE id = %s
            RETURNING id, trajectory, status, issued_revision, created_at, cancelled_at
            """,
            (pass_id,),
        )
        row = cur.fetchone()
    return row


# ---------------------------------------------------------------- 择时通行承诺

def commitment_safe_intervals(
    traj: list[tuple[float, float, float]],
    regions: list[dict],
    window_start: float,
    window_end: float,
    now_epoch: float,
) -> list[dict]:
    """承诺视角的安全出发区间：候选窗口内、当前时刻之后的左闭右开区间。

    复用 /api/schedule 的精确区间运算；有效候选窗口取
    [max(window_start, now), window_end)——起点早于当前时刻的窗口部分已
    无法再承诺，以 now 为闭端裁剪（now 为单个零测度时刻，且每次查询/校验
    都以当时的 now 重算）。窗口已结束或已无可行时刻时返回 []。
    """
    effective_start = max(window_start, now_epoch)
    if effective_start >= window_end:
        return []
    return geometry.safe_departure_intervals(
        traj, regions, effective_start, window_end
    )


def _load_constraining_commitments(cur, now: datetime) -> list[dict]:
    """未取消且候选窗口未结束（window_end 晚于当前时刻）的承诺。

    调用方应已持有 region_meta 行锁：签发/取消与所有区域写入在此串行化，
    锁内读到的是稳定集合。
    """
    cur.execute(
        """
        SELECT id, trajectory, window_start, window_end
        FROM commitments
        WHERE status = 'active' AND window_end > %s
        ORDER BY id
        """,
        (now,),
    )
    commitments = []
    for row in cur.fetchall():
        traj = [
            (float(p["t"]), float(p["x"]), float(p["y"]))
            for p in row["trajectory"]
        ]
        commitments.append({
            "id": row["id"],
            "trajectory": traj,
            "window_start": _epoch(row["window_start"]),
            "window_end": _epoch(row["window_end"]),
        })
    return commitments


def _emptied_commitment_ids(
    cur,
    regions_before: list[dict],
    regions_after: list[dict],
    now: datetime,
) -> list[int]:
    """变更前仍有未来安全时刻、变更后一点不剩的未结束承诺 id（升序）。

    对每张未取消且窗口未结束的承诺，分别用变更前、变更后的完整区域集合
    重算「当前时刻之后」的安全出发区间（同一 now）：变更前仍有未来安全
    时刻、变更后一点不剩时该承诺被清空；变更前已无未来安全时刻的承诺
    不再约束（窗口虽未结束，但已无可保护之物）。
    """
    now_epoch = now.timestamp()
    conflicts: list[int] = []
    for c in _load_constraining_commitments(cur, now):
        before = commitment_safe_intervals(
            c["trajectory"], regions_before,
            c["window_start"], c["window_end"], now_epoch,
        )
        if not before:
            # 变更前已无未来安全时刻：本笔变更不再破坏任何东西
            continue
        after = commitment_safe_intervals(
            c["trajectory"], regions_after,
            c["window_start"], c["window_end"], now_epoch,
        )
        if not after:
            conflicts.append(c["id"])
    return conflicts


def check_commitments_safe(
    cur,
    regions_before: list[dict],
    regions_after: list[dict],
    now: datetime,
) -> None:
    """区域变更不得抹去承诺窗口内剩余的全部安全出发时刻。

    任一承诺被清空（变更前仍有未来安全时刻、变更后一点不剩）即抛
    CommitmentConflict，调用方应回滚整笔变更。
    """
    conflicts = _emptied_commitment_ids(cur, regions_before, regions_after, now)
    if conflicts:
        raise CommitmentConflict(conflicts)


def issue_commitment(
    cur,
    traj: list[tuple[float, float, float]],
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """签发择时通行承诺：在持有 meta 行锁的事务内核验并持久化。

    traj: [(t_seconds, x, y), ...]，已校验（≥2 点、首点 t=0、严格递增）。
    窗口起点必须晚于签发时刻（拿到写锁后的当前服务时间），否则
    TrajectoryError；按当前完整区域集合计算左闭右开的安全出发区间，
    区间为空则抛 CommitmentUnsafe，拒签且不落库。
    返回含初始安全区间（epoch 秒）的 dict。
    """
    # 锁全局元数据行：与取消、所有区域写入串行化，核验与落库之间无人插队
    revision = get_revision(cur)
    now = utcnow()
    if window_start <= now:
        raise geometry.TrajectoryError(
            "候选出发窗口起点必须晚于签发时刻（当前服务时间），"
            "已开始或正在开始的窗口不予签发承诺"
        )
    regions = [
        _region_row_to_geom(r) for r in _load_all_regions_locked(cur).values()
    ]
    intervals = geometry.safe_departure_intervals(
        traj, regions, _epoch(window_start), _epoch(window_end)
    )
    if not intervals:
        raise CommitmentUnsafe()
    cur.execute(
        """
        INSERT INTO commitments
            (trajectory, window_start, window_end, status, issued_revision)
        VALUES (%(trajectory)s::jsonb, %(window_start)s, %(window_end)s,
                'active', %(revision)s)
        RETURNING id, trajectory, window_start, window_end, status,
                  issued_revision, created_at, cancelled_at,
                  fulfilled_at, fulfilled_pass_id
        """,
        {
            "trajectory": json.dumps(
                [{"x": x, "y": y, "t": t} for t, x, y in traj]
            ),
            "window_start": window_start,
            "window_end": window_end,
            "revision": revision,
        },
    )
    row = cur.fetchone()
    row["intervals"] = intervals
    return row


def get_commitment(cur, commitment_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, trajectory, window_start, window_end, status,
               issued_revision, created_at, cancelled_at,
               fulfilled_at, fulfilled_pass_id
        FROM commitments WHERE id = %s
        """,
        (commitment_id,),
    )
    return cur.fetchone()


def cancel_commitment(cur, commitment_id: int) -> dict | None:
    """取消承诺；重复取消幂等（已取消的直接返回现状）。未找到返回 None。

    与签发、所有区域写入在同一把 meta 行锁上串行化。已被兑现的承诺按
    既有取消语义已是 cancelled（fulfilled_pass_id 非空），重复取消同样
    幂等返回现状，不会改动兑现标记。
    """
    get_revision(cur)
    cur.execute(
        """
        SELECT id, trajectory, window_start, window_end, status,
               issued_revision, created_at, cancelled_at,
               fulfilled_at, fulfilled_pass_id
        FROM commitments WHERE id = %s
        FOR UPDATE
        """,
        (commitment_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row["status"] == "active":
        cur.execute(
            """
            UPDATE commitments SET status = 'cancelled', cancelled_at = now()
            WHERE id = %s
            RETURNING id, trajectory, window_start, window_end, status,
                      issued_revision, created_at, cancelled_at,
                      fulfilled_at, fulfilled_pass_id
            """,
            (commitment_id,),
        )
        row = cur.fetchone()
    return row


def _departure_is_safe(safe_intervals: list[dict], departure: float) -> bool:
    """出发时刻是否落在某段左闭右开安全出发区间内（严格端点判定）。

    安全区间由与 /api/schedule 完全相同的精确区间运算生成，左闭右开：
    区间起点可出发、终点不可出发，因此这里直接做 start <= s < end，
    不引入任何容差，保证与调度语义逐点一致。
    """
    return any(
        iv["start"] <= departure < iv["end"]
        for iv in safe_intervals
    )


def fulfill_commitment(cur, commitment_id: int, departure: datetime) -> dict:
    """把一张择时通行承诺兑现为一张固定时刻通行凭证（同一写事务）。

    在持有全局元数据行锁的事务内完成，与区域变更、承诺取消及凭证签发
    共用该锁串行化：

    * 未知承诺：返回 None（调用方回滚并返回 404）；
    * 已兑现（cancelled 且 fulfilled_pass_id 非空）：抛
      CommitmentAlreadyFulfilled，失败方可与普通取消区分；
    * 已普通取消：抛 CommitmentExplicitlyCancelled；
    * 窗口已结束（window_end 不晚于受理时刻）：抛 CommitmentExpired；
    * 出发时刻不晚于受理时刻：抛 DepartureNotInFuture；
    * 出发时刻不在原候选窗口 [window_start, window_end)：抛
      DepartureOutOfWindow；
    * 出发时刻不在按当前区域集合重算的安全出发区间内：抛
      DepartureUnsafe（携带当前安全区间）。

    成功时：按承诺相对轨迹生成绝对时刻轨迹（出发时刻 + 相对秒数），
    只插入一张凭证，承诺按既有取消语义置为 cancelled（cancelled_at、
    fulfilled_at 置位并记录凭证 id）；不推进区域 revision，新凭证的
    issued_revision 为本次重验所据的当前 revision。返回
    {"revision", "commitment", "pass"}。
    """
    # 锁全局元数据行：与区域变更、承诺/凭证的签发与取消串行化，
    # 重验与落库之间无人插队
    revision = get_revision(cur)
    now = utcnow()
    cur.execute(
        """
        SELECT id, trajectory, window_start, window_end, status,
               issued_revision, created_at, cancelled_at,
               fulfilled_at, fulfilled_pass_id
        FROM commitments WHERE id = %s
        FOR UPDATE
        """,
        (commitment_id,),
    )
    commitment = cur.fetchone()
    if commitment is None:
        return None

    if commitment["status"] == "cancelled":
        # 承诺已失效：先区分「已兑现」与「普通取消」，失败方据此分支处理
        if commitment["fulfilled_pass_id"] is not None:
            raise CommitmentAlreadyFulfilled(int(commitment["fulfilled_pass_id"]))
        raise CommitmentExplicitlyCancelled()

    # 窗口未结束：以服务端受理时刻（拿到写锁后的 now）为准
    if commitment["window_end"] <= now:
        raise CommitmentExpired()

    departure_epoch = departure.timestamp()
    if departure_epoch <= now.timestamp():
        raise DepartureNotInFuture()

    window_start_epoch = _epoch(commitment["window_start"])
    window_end_epoch = _epoch(commitment["window_end"])
    if not (window_start_epoch <= departure_epoch < window_end_epoch):
        raise DepartureOutOfWindow()

    # 在写事务内按当前完整区域集合重算安全出发区间（与 /api/schedule
    # 同一精确区间运算），并确认出发时刻落在其中
    regions = [
        _region_row_to_geom(r) for r in _load_all_regions_locked(cur).values()
    ]
    rel_traj = [
        (float(p["t"]), float(p["x"]), float(p["y"]))
        for p in commitment["trajectory"]
    ]
    safe_intervals = geometry.safe_departure_intervals(
        rel_traj, regions, window_start_epoch, window_end_epoch
    )
    if not _departure_is_safe(safe_intervals, departure_epoch):
        raise DepartureUnsafe(safe_intervals)

    # 按承诺的相对轨迹生成绝对时刻轨迹：点 i 的绝对时刻 = 出发时刻 + t_i
    abs_traj = [
        (departure_epoch + t, x, y) for t, x, y in rel_traj
    ]
    cur.execute(
        """
        INSERT INTO passes (trajectory, start_time, end_time, status, issued_revision)
        VALUES (%(trajectory)s::jsonb, %(start)s, %(end)s, 'active', %(revision)s)
        RETURNING id, trajectory, status, issued_revision, created_at, cancelled_at
        """,
        {
            "trajectory": json.dumps(
                [{"x": x, "y": y, "t": t} for t, x, y in abs_traj]
            ),
            "start": datetime.fromtimestamp(abs_traj[0][0], tz=timezone.utc),
            "end": datetime.fromtimestamp(abs_traj[-1][0], tz=timezone.utc),
            "revision": revision,
        },
    )
    pass_row = cur.fetchone()

    # 原承诺按既有取消语义失效（status/cancelled_at），并标记兑现来源；
    # 不推进区域 revision、不写区域变更审计
    cur.execute(
        """
        UPDATE commitments
        SET status = 'cancelled', cancelled_at = now(),
            fulfilled_at = now(), fulfilled_pass_id = %(pass_id)s
        WHERE id = %(id)s
        RETURNING id, trajectory, window_start, window_end, status,
                  issued_revision, created_at, cancelled_at,
                  fulfilled_at, fulfilled_pass_id
        """,
        {"pass_id": pass_row["id"], "id": commitment_id},
    )
    commitment = cur.fetchone()
    return {"revision": revision, "commitment": commitment, "pass": pass_row}


# ---------------------------------------------------------------- 安全状态观察

_OBSERVATION_COLUMNS = (
    "id, trajectory, start_time, end_time, status, registered_revision, "
    "conclusion, conclusion_revision, created_at, cancelled_at"
)


def load_constraining_observations(cur, now: datetime) -> list[dict]:
    """未取消且末点时刻未过的观察（调用方应已持有 region_meta 行锁）。

    区域变更提交在同一把 meta 行锁上串行化，锁内读到的是稳定集合：
    锁释放前不会有新观察登记或取消生效。
    """
    cur.execute(
        """
        SELECT id, trajectory
        FROM observations
        WHERE status = 'active' AND end_time > %s
        ORDER BY id
        """,
        (now,),
    )
    observations = []
    for row in cur.fetchall():
        traj = [
            (float(p["t"]), float(p["x"]), float(p["y"]))
            for p in row["trajectory"]
        ]
        observations.append({"id": row["id"], "trajectory": traj})
    return observations


def _observation_flips(
    cur,
    regions_before: list[dict],
    regions_after: list[dict],
    now: datetime,
) -> list[dict]:
    """未取消且末点时刻未过的观察中，结论发生净翻转的清单（按 id 升序）。

    分别用变更前、变更后的完整区域集合核验（沿用直线匀速、边界接触即
    穿越、生效期左闭右开规则），仅结论翻转（safe↔unsafe）的观察入列，
    每条含 observation_id 与旧/新结论。已取消或末点时刻已过的观察不再
    核验。
    """
    flips: list[dict] = []
    for obs in load_constraining_observations(cur, now):
        before_results = geometry.verify_trajectory(obs["trajectory"], regions_before)
        old_safe = all(not r["crossings"] for r in before_results)
        after_results = geometry.verify_trajectory(obs["trajectory"], regions_after)
        new_safe = all(not r["crossings"] for r in after_results)
        if old_safe == new_safe:
            continue
        flips.append({
            "observation_id": obs["id"],
            "old_conclusion": "safe" if old_safe else "unsafe",
            "new_conclusion": "safe" if new_safe else "unsafe",
        })
    return flips


def record_observation_flips(
    cur,
    regions_before: list[dict],
    regions_after: list[dict],
    new_revision: int,
    now: datetime,
) -> None:
    """区域变更成功提交时，在同一事务记录未结束观察的结论翻转事件。

    对每张结论净翻转的观察（见 _observation_flips）：
      * 更新 observations 的最近结论与结论对应 revision；
      * 追加一条事件（含观察编号、旧/新结论、新 revision），seq 在观察内
        连续递增。整批变更对同一观察至多一条事件。
    本函数不改变区域数据；调用方事务回滚时事件与结论更新一并消失。
    """
    for flip in _observation_flips(cur, regions_before, regions_after, now):
        obs_id = flip["observation_id"]
        cur.execute(
            """
            SELECT coalesce(max(seq), 0) AS last_seq
            FROM observation_events
            WHERE observation_id = %s
            """,
            (obs_id,),
        )
        next_seq = int(cur.fetchone()["last_seq"]) + 1
        cur.execute(
            """
            INSERT INTO observation_events
                (observation_id, seq, old_conclusion, new_conclusion, revision)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (obs_id, next_seq, flip["old_conclusion"], flip["new_conclusion"],
             new_revision),
        )
        cur.execute(
            """
            UPDATE observations
            SET conclusion = %s, conclusion_revision = %s
            WHERE id = %s
            """,
            (flip["new_conclusion"], new_revision, obs_id),
        )


def register_observation(cur, traj: list[tuple[float, float, float]]) -> dict:
    """登记安全状态观察：在持有 meta 行锁的事务内核验并持久化。

    traj: [(epoch, x, y), ...]，已校验（≥2 点、时刻严格递增）。
    首点必须晚于登记时刻（拿到写锁后的当前服务时间），否则 TrajectoryError。
    安全或不安全均可登记，返回观察行（含当前结论与区域 revision）；
    登记不推进区域 revision，也不产生事件。
    """
    # 锁全局元数据行：与取消、所有区域写入串行化，核验与落库之间无人插队
    revision = get_revision(cur)
    now = utcnow()
    if traj[0][0] <= now.timestamp():
        raise geometry.TrajectoryError(
            "轨迹首点时刻必须晚于登记时刻（当前服务时间），已开始的轨迹不予登记观察"
        )
    regions = [
        _region_row_to_geom(r) for r in _load_all_regions_locked(cur).values()
    ]
    results = geometry.verify_trajectory(traj, regions)
    safe = all(not r["crossings"] for r in results)
    conclusion = "safe" if safe else "unsafe"
    cur.execute(
        """
        INSERT INTO observations
            (trajectory, start_time, end_time, status,
             registered_revision, conclusion, conclusion_revision)
        VALUES (%(trajectory)s::jsonb, %(start)s, %(end)s, 'active',
                %(revision)s, %(conclusion)s, %(revision)s)
        RETURNING """ + _OBSERVATION_COLUMNS,
        {
            "trajectory": json.dumps(
                [{"x": x, "y": y, "t": t} for t, x, y in traj]
            ),
            "start": datetime.fromtimestamp(traj[0][0], tz=timezone.utc),
            "end": datetime.fromtimestamp(traj[-1][0], tz=timezone.utc),
            "revision": revision,
            "conclusion": conclusion,
        },
    )
    return cur.fetchone()


def get_observation(cur, observation_id: int) -> dict | None:
    cur.execute(
        "SELECT " + _OBSERVATION_COLUMNS + " FROM observations WHERE id = %s",
        (observation_id,),
    )
    return cur.fetchone()


def cancel_observation(cur, observation_id: int) -> dict | None:
    """取消观察；重复取消幂等（已取消的直接返回现状）。未找到返回 None。

    与登记、所有区域写入在同一把 meta 行锁上串行化；取消不推进区域
    revision，取消后观察不再产生任何事件。
    """
    get_revision(cur)
    cur.execute(
        "SELECT " + _OBSERVATION_COLUMNS + " FROM observations WHERE id = %s FOR UPDATE",
        (observation_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row["status"] == "active":
        cur.execute(
            """
            UPDATE observations SET status = 'cancelled', cancelled_at = now()
            WHERE id = %s
            RETURNING """ + _OBSERVATION_COLUMNS,
            (observation_id,),
        )
        row = cur.fetchone()
    return row


def list_observation_events(
    cur, observation_id: int, after_seq: int, limit: int
) -> tuple[list[dict], bool]:
    """按事件序号游标升序读取观察事件。

    返回 (events, has_more)：取 seq > after_seq 的至多 limit+1 行，
    多取的一行仅用于判定是否还有下一页。
    """
    cur.execute(
        """
        SELECT observation_id, seq, old_conclusion, new_conclusion,
               revision, created_at
        FROM observation_events
        WHERE observation_id = %s AND seq > %s
        ORDER BY seq ASC
        LIMIT %s
        """,
        (observation_id, after_seq, limit + 1),
    )
    rows = cur.fetchall()
    has_more = len(rows) > limit
    return rows[:limit], has_more
