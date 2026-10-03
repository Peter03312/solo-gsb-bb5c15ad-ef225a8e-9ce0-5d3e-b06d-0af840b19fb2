"""FastAPI 入口：临时限行核验 HTTP API。"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import CheckViolation
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, geometry, repository
from .repository import (
    BatchDuplicateRegion,
    BatchInvalidOperation,
    BatchRegionsNotFound,
    BatchRevisionConflict,
    BatchUnsafeTrajectories,
    CommitmentAlreadyFulfilled,
    CommitmentConflict,
    CommitmentExplicitlyCancelled,
    CommitmentExpired,
    CommitmentUnsafe,
    DepartureNotInFuture,
    DepartureOutOfWindow,
    DepartureUnsafe,
    ExemptionDuplicateRegions,
    ExemptionRegionsNotFound,
    ExemptionRevisionConflict,
    IdempotencyKeyConflict,
    PassConflict,
    PassUnsafe,
)
from .schemas import (
    CommitmentFulfillRequest,
    CommitmentFulfillResponse,
    CommitmentIssueRequest,
    CommitmentOut,
    DepartureIntervalOut,
    ExemptionPreviewRequest,
    ExemptionPreviewResponse,
    ObservationEventPage,
    ObservationOut,
    ObservationRegisterRequest,
    PassIssueRequest,
    PassOut,
    RegionBatchPreviewResponse,
    RegionBatchRequest,
    RegionBatchResponse,
    RegionChangePage,
    RegionCreate,
    RegionCreatedResponse,
    RegionOut,
    RegionResultOut,
    RegionUpdate,
    RevisionOut,
    ScheduleRequest,
    ScheduleResponse,
    VerifyRequest,
    VerifyResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("restriction-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    yield
    db.close_pool()


app = FastAPI(
    title="临时限行核验服务",
    version="1.10.0",
    description="登记平面多边形限行区域、对连续轨迹进行穿越核验，并计算固定行程的安全出发区间；支持多区域变更与轨迹核验一次原子提交；可为指定轨迹签发可取消的通行凭证，未取消且未结束的凭证约束后续区域变更；安全员还可为相对秒数固定行程与候选出发窗口申请择时通行承诺，未取消且窗口未结束的承诺在区域变更抹除其全部未来安全出发时刻时整笔拒绝变更；安全员还可把尚未取消、窗口未结束的承诺兑现为一张固定时刻通行凭证（POST /api/commitments/{id}/fulfill）：在与区域变更、承诺取消及凭证签发共用的写事务内重验出发时刻晚于受理时刻、落在原候选窗口与按当前区域集合重算的安全出发区间内，成功后原承诺按既有取消语义失效、只生成一张凭证，区域 revision 不变，并发兑现至多一次成功且失败方可区分已兑现与普通取消；可为带时区绝对时刻轨迹登记可取消的安全状态观察（安全或不安全均可登记），区域变更成功提交时为未取消且未结束观察的安全结论翻转在同一事务追加事件；区域写操作（登记/修订/删除/批量）在同一事务追加只读可分页的区域集合变更审计历史，并支持 Idempotency-Key 幂等重试；安全员还可对固定行程做最小区域豁免预演（只读）：在可豁免候选区域的子集中找出使候选窗口内存在安全出发时刻的最优方案（豁免数最少、最早安全时刻、编号字典序），预演不改变任何数据；也可对多区调整做批量变更影响预演（只读）：基于同一已提交快照报告各轨迹安全结论与穿越明细、将冲突的通行凭证、将被清空的择时承诺与观察结论净翻转，预演不改变任何数据、不占用区域编号。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 错误处理

class DomainError(Exception):
    """业务语义错误（HTTP 422）。"""

    def __init__(self, message: str, loc: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.loc = loc or []


class InvalidIdempotencyKey(Exception):
    """幂等键本身非法（空键或超过 128 字符，HTTP 422）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


IDEMPOTENCY_KEY_MAX_LEN = 128


def _normalize_idempotency_key(raw: str | None) -> str | None:
    """校验 Idempotency-Key 头：非空且最长 128 字符；未携带返回 None（既有行为）。"""
    if raw is None:
        return None
    if not raw.strip():
        raise InvalidIdempotencyKey("幂等键不能为空（Idempotency-Key 头需为非空字符串）")
    if len(raw) > IDEMPOTENCY_KEY_MAX_LEN:
        raise InvalidIdempotencyKey(
            f"幂等键最长 {IDEMPOTENCY_KEY_MAX_LEN} 字符（当前 {len(raw)} 字符）"
        )
    return raw


def _request_fingerprint(operation: str, target, payload) -> str:
    """以操作类型、目标区域与规范化请求内容判定「同一次请求」。

    payload 一律取 Pydantic 校验归一化后的 model_dump(mode="json")：
    时刻已归一化为 UTC、多边形已归一化为逆时针闭合环、数值统一为浮点，
    因此语义相同的请求（即使 JSON 键序或时区写法不同）得到同一指纹。
    """
    canonical = json.dumps(
        {"operation": operation, "target": target, "payload": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _raw_json_response(body: str, status_code: int) -> Response:
    """按 JSON 原文返回响应。

    带键请求首次提交与其重试回放共用同一串响应原文（入库的就是它），
    保证重试逐字节返回原响应。
    """
    return Response(content=body, media_type="application/json", status_code=status_code)


def _error_response(status: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
            }
        },
    )


def _crossings_to_out(results: list[dict]) -> list[dict]:
    """把几何层结果（穿越时刻为 epoch 秒）转成 API 输出（UTC datetime）。"""
    out_results = []
    for r in results:
        crossings = []
        for c in r["crossings"]:
            crossings.append({
                "entry_time": datetime.fromtimestamp(c["entry_time"], tz=timezone.utc),
                "exit_time": datetime.fromtimestamp(c["exit_time"], tz=timezone.utc),
                "entry_inclusive": c["entry_inclusive"],
                "exit_inclusive": c["exit_inclusive"],
                "point_contact": c["point_contact"],
            })
        out_results.append({
            "region_id": r["region_id"],
            "region_name": r["region_name"],
            "crossings": crossings,
        })
    return out_results


def _preview_results_to_out(results: list[dict]) -> list[dict]:
    """把影响预演的几何层结果转成 API 输出。

    与 _crossings_to_out 相同，但拟登记（create）区域的 region_id 为
    None，另带 operation_index（操作下标）标识。
    """
    out_results = []
    for r in results:
        crossings = []
        for c in r["crossings"]:
            crossings.append({
                "entry_time": datetime.fromtimestamp(c["entry_time"], tz=timezone.utc),
                "exit_time": datetime.fromtimestamp(c["exit_time"], tz=timezone.utc),
                "entry_inclusive": c["entry_inclusive"],
                "exit_inclusive": c["exit_inclusive"],
                "point_contact": c["point_contact"],
            })
        out_results.append({
            "region_id": r["region_id"],
            "operation_index": r["operation_index"],
            "region_name": r["region_name"],
            "crossings": crossings,
        })
    return out_results


def _pass_row_to_out(row: dict) -> dict:
    """把 passes 表行（trajectory 为 JSONB，t 为 epoch 秒）转成 API 输出。"""
    return {
        "id": row["id"],
        "status": row["status"],
        "revision": row["issued_revision"],
        "trajectory": [
            {
                "x": p["x"],
                "y": p["y"],
                "t": datetime.fromtimestamp(p["t"], tz=timezone.utc),
            }
            for p in row["trajectory"]
        ],
        "issued_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
    }


def _observation_row_to_out(row: dict) -> dict:
    """把 observations 表行（trajectory 为 JSONB，t 为 epoch 秒）转成 API 输出。"""
    return {
        "id": row["id"],
        "status": row["status"],
        "trajectory": [
            {
                "x": p["x"],
                "y": p["y"],
                "t": datetime.fromtimestamp(p["t"], tz=timezone.utc),
            }
            for p in row["trajectory"]
        ],
        "conclusion": row["conclusion"],
        "revision": row["conclusion_revision"],
        "registered_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
    }


def _commitment_intervals_to_out(intervals: list[dict]) -> list[dict]:
    """把承诺安全区间（epoch 秒）转成 API 输出（UTC datetime，左闭右开）。"""
    return [
        {
            "start": datetime.fromtimestamp(iv["start"], tz=timezone.utc),
            "end": datetime.fromtimestamp(iv["end"], tz=timezone.utc),
            "start_inclusive": iv["start_inclusive"],
            "end_inclusive": iv["end_inclusive"],
        }
        for iv in intervals
    ]


def _commitment_row_to_out(row: dict, intervals: list[dict]) -> dict:
    """把 commitments 表行（trajectory 为相对秒数 JSONB）转成 API 输出。"""
    return {
        "id": row["id"],
        "status": row["status"],
        "revision": row["issued_revision"],
        "trajectory": [
            {"x": p["x"], "y": p["y"], "t": p["t"]}
            for p in row["trajectory"]
        ],
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "intervals": _commitment_intervals_to_out(intervals),
        "issued_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
        "fulfilled_pass_id": row.get("fulfilled_pass_id"),
    }


@app.exception_handler(ValidationError)
async def _on_pydantic_error(_request: Request, exc: ValidationError):
    details = []
    for err in exc.errors():
        details.append({
            "loc": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return _error_response(422, "validation_error", "请求参数校验失败", details)


@app.exception_handler(RequestValidationError)
async def _on_request_validation_error(_request: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        details.append({
            "loc": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return _error_response(422, "validation_error", "请求参数校验失败", details)


@app.exception_handler(StarletteHTTPException)
async def _on_http_exception(_request: Request, exc: StarletteHTTPException):
    # 404/405 等也使用统一错误信封；业务侧已把 {"error": {...}} 放在 detail 中
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = exc.detail
    else:
        body = {"error": {"code": "http_error", "message": str(exc.detail), "details": []}}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(DomainError)
async def _on_domain_error(_request: Request, exc: DomainError):
    details = [{"loc": exc.loc, "message": exc.message}] if exc.loc else []
    return _error_response(422, "invalid_input", exc.message, details)


@app.exception_handler(InvalidIdempotencyKey)
async def _on_invalid_idempotency_key(_request: Request, exc: InvalidIdempotencyKey):
    return _error_response(
        422,
        "invalid_idempotency_key",
        exc.message,
        [{"loc": ["header", "Idempotency-Key"], "message": exc.message}],
    )


@app.exception_handler(IdempotencyKeyConflict)
async def _on_idempotency_key_conflict(_request: Request, exc: IdempotencyKeyConflict):
    return _error_response(
        409,
        "idempotency_key_conflict",
        str(exc),
        [{"loc": ["header", "Idempotency-Key"],
          "message": "该幂等键已绑定另一份内容不同的请求（操作类型/目标区域/请求内容不一致）"}],
    )


@app.exception_handler(geometry.RegionError)
async def _on_region_error(_request: Request, exc: geometry.RegionError):
    return _error_response(422, "invalid_polygon", str(exc))


@app.exception_handler(geometry.TrajectoryError)
async def _on_trajectory_error(_request: Request, exc: geometry.TrajectoryError):
    return _error_response(422, "invalid_trajectory", str(exc))


@app.exception_handler(BatchRevisionConflict)
async def _on_batch_revision_conflict(_request: Request, exc: BatchRevisionConflict):
    return _error_response(
        409,
        "revision_conflict",
        str(exc),
        [{"loc": ["expected_revision"],
          "message": f"预期 revision={exc.expected}，当前 revision={exc.current}",
          "current_revision": exc.current}],
    )


@app.exception_handler(BatchRegionsNotFound)
async def _on_batch_regions_not_found(_request: Request, exc: BatchRegionsNotFound):
    return _error_response(
        404,
        "region_not_found",
        str(exc),
        [{"loc": ["operations", str(m["index"]), "id"],
          "message": f"区域 {m['region_id']} 不存在",
          "operation_index": m["index"],
          "region_id": m["region_id"]}
         for m in exc.missing],
    )


@app.exception_handler(BatchDuplicateRegion)
async def _on_batch_duplicate_region(_request: Request, exc: BatchDuplicateRegion):
    return _error_response(
        422,
        "duplicate_region",
        str(exc),
        [{"loc": ["operations"],
          "message": f"区域 {exc.region_id} 在同一批中出现了多次",
          "region_id": exc.region_id,
          "operation_indices": exc.indices}],
    )


@app.exception_handler(BatchInvalidOperation)
async def _on_batch_invalid_operation(_request: Request, exc: BatchInvalidOperation):
    loc = ["operations", str(exc.index)] + ([exc.field] if exc.field else [])
    return _error_response(
        422,
        "invalid_operation",
        exc.message,
        [{"loc": loc, "message": exc.message, "operation_index": exc.index}],
    )


@app.exception_handler(BatchUnsafeTrajectories)
async def _on_batch_unsafe(_request: Request, exc: BatchUnsafeTrajectories):
    details = []
    for t in exc.trajectory_results:
        if t["safe"]:
            continue
        # 只回显有穿越的区域，突出违规明细；经响应模型序列化（UTC Z 时刻）
        violated = [r for r in t["results"] if r["crossings"]]
        dumped = [
            RegionResultOut.model_validate(r).model_dump(mode="json")
            for r in _crossings_to_out(violated)
        ]
        details.append({
            "trajectory_index": t["index"],
            "results": dumped,
        })
    return _error_response(
        422,
        "unsafe_trajectory",
        "存在轨迹在变更后区域集合下发生穿越，整批变更已取消且不落库",
        details,
    )


@app.exception_handler(PassUnsafe)
async def _on_pass_unsafe(_request: Request, exc: PassUnsafe):
    # 只回显有穿越的区域；经响应模型序列化（UTC Z 时刻）
    violated = [r for r in exc.results if r["crossings"]]
    details = [
        RegionResultOut.model_validate(r).model_dump(mode="json")
        for r in _crossings_to_out(violated)
    ]
    return _error_response(
        422,
        "unsafe_trajectory",
        "轨迹在当前区域集合下发生穿越，凭证未签发且不落库",
        details,
    )


@app.exception_handler(PassConflict)
async def _on_pass_conflict(_request: Request, exc: PassConflict):
    return _error_response(
        409,
        "pass_conflict",
        str(exc),
        [{"pass_id": pid,
          "message": f"凭证 {pid}（未取消且末点时刻未过）的轨迹在变更后区域集合下不再安全"}
         for pid in exc.pass_ids],
    )


@app.exception_handler(CommitmentUnsafe)
async def _on_commitment_unsafe(_request: Request, exc: CommitmentUnsafe):
    return _error_response(
        422,
        "unsafe_trajectory",
        "候选出发窗口内当前不存在任何安全出发时刻，承诺拒签且不落库",
        [],
    )


@app.exception_handler(CommitmentConflict)
async def _on_commitment_conflict(_request: Request, exc: CommitmentConflict):
    return _error_response(
        409,
        "commitment_conflict",
        str(exc),
        [{"commitment_id": cid,
          "message": f"承诺 {cid}（未取消且窗口未结束）在变更前仍有未来安全出发时刻，"
                     f"变更后窗口结束前将不再有任何安全出发时刻"}
         for cid in exc.commitment_ids],
    )


@app.exception_handler(CommitmentAlreadyFulfilled)
async def _on_commitment_already_fulfilled(_request: Request, exc: CommitmentAlreadyFulfilled):
    return _error_response(
        409,
        "commitment_already_fulfilled",
        str(exc),
        [{"loc": ["path", "commitment_id"],
          "message": f"承诺已被兑现为通行凭证 {exc.pass_id}，不能重复兑现",
          "pass_id": exc.pass_id}],
    )


@app.exception_handler(CommitmentExplicitlyCancelled)
async def _on_commitment_explicitly_cancelled(_request: Request, exc: CommitmentExplicitlyCancelled):
    return _error_response(
        422,
        "commitment_cancelled",
        "承诺已取消，不能兑现",
        [{"loc": ["path", "commitment_id"], "message": "承诺已被普通取消，不能兑现"}],
    )


@app.exception_handler(CommitmentExpired)
async def _on_commitment_expired(_request: Request, exc: CommitmentExpired):
    return _error_response(
        422,
        "commitment_expired",
        "承诺候选窗口已结束，不能兑现",
        [{"loc": ["path", "commitment_id"],
          "message": "承诺候选窗口终点不晚于服务端受理时刻，窗口已结束"}],
    )


@app.exception_handler(DepartureNotInFuture)
async def _on_departure_not_in_future(_request: Request, exc: DepartureNotInFuture):
    return _error_response(
        422,
        "departure_not_in_future",
        str(exc),
        [{"loc": ["body", "departure_time"],
          "message": "出发时刻必须晚于服务端受理时刻（当前服务时间）"}],
    )


@app.exception_handler(DepartureOutOfWindow)
async def _on_departure_out_of_window(_request: Request, exc: DepartureOutOfWindow):
    return _error_response(
        422,
        "departure_out_of_window",
        str(exc),
        [{"loc": ["body", "departure_time"],
          "message": "出发时刻必须落在承诺原候选窗口 [window_start, window_end) 内"}],
    )


@app.exception_handler(DepartureUnsafe)
async def _on_departure_unsafe(_request: Request, exc: DepartureUnsafe):
    details = [{"loc": ["body", "departure_time"],
                "message": "出发时刻不在按当前区域集合重算的安全出发区间内"}]
    if exc.safe_intervals:
        # 回显当前（窗口内）左闭右开安全区间，便于客户端改取安全时刻重试；
        # 经响应模型序列化（UTC Z 时刻的纯 JSON 结构）
        dumped = [
            DepartureIntervalOut.model_validate(iv).model_dump(mode="json")
            for iv in _commitment_intervals_to_out(exc.safe_intervals)
        ]
        details.append({"safe_intervals": dumped})
    return _error_response(
        422,
        "unsafe_trajectory",
        "出发时刻不在当前安全出发区间内，承诺未兑现且不改变承诺",
        details,
    )


@app.exception_handler(ExemptionRevisionConflict)
async def _on_exemption_revision_conflict(_request: Request, exc: ExemptionRevisionConflict):
    return _error_response(
        409,
        "revision_conflict",
        str(exc),
        [{"loc": ["expected_revision"],
          "message": f"预期 revision={exc.expected}，当前 revision={exc.current}",
          "current_revision": exc.current}],
    )


@app.exception_handler(ExemptionRegionsNotFound)
async def _on_exemption_regions_not_found(_request: Request, exc: ExemptionRegionsNotFound):
    return _error_response(
        404,
        "region_not_found",
        str(exc),
        [{"loc": ["candidate_region_ids"],
          "message": f"区域 {rid} 不存在",
          "region_id": rid}
         for rid in exc.missing],
    )


@app.exception_handler(ExemptionDuplicateRegions)
async def _on_exemption_duplicate_regions(_request: Request, exc: ExemptionDuplicateRegions):
    return _error_response(
        422,
        "duplicate_region",
        str(exc),
        [{"loc": ["candidate_region_ids"],
          "message": f"区域 {rid} 在候选豁免编号中出现了多次",
          "region_id": rid}
         for rid in exc.duplicates],
    )


# ---------------------------------------------------------------- 区域管理

@app.post("/api/regions", response_model=RegionCreatedResponse, status_code=201,
          tags=["regions"])
def create_region(payload: RegionCreate,
                  idempotency_key: str | None = Header(
                      default=None,
                      description="幂等键：非空、最长 128 字符；"
                                  "同键同内容重试返回原响应，同键不同内容 409",
                  )):
    """登记一个限行区域；非法多边形或不递增时段返回 422 且不保存。

    携带 `Idempotency-Key` 请求头时启用幂等重试：首次成功提交后，相同键 +
    相同请求内容的重试直接返回原状态与原响应，不重复登记。
    """
    key = _normalize_idempotency_key(idempotency_key)
    try:
        payload.validate_time_order()
    except ValueError as exc:
        raise DomainError(str(exc), ["valid_from", "valid_to"]) from exc
    fingerprint = (
        _request_fingerprint("create", None, payload.model_dump(mode="json"))
        if key is not None else None
    )
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                if key is not None:
                    replay = repository.idempotency_claim(cur, key, fingerprint)
                    if replay is not None:
                        conn.rollback()  # 只读回放，不产生任何变更
                        return _raw_json_response(replay["response_body"],
                                                  replay["status_code"])
                region, revision = repository.create_region(cur, payload, payload.polygon)
                response = RegionCreatedResponse(
                    region=RegionOut.model_validate(region), revision=revision
                )
                if key is not None:
                    # 响应原文与业务变更同事务落库；此后同键重试只回放、不再变更
                    body = response.model_dump_json()
                    repository.idempotency_store(cur, key, 201, body)
            except CheckViolation as exc:
                conn.rollback()
                raise DomainError("生效时段必须满足 valid_from < valid_to（左闭右开）") from exc
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    if key is not None:
        return _raw_json_response(body, 201)
    return response


@app.get("/api/regions", response_model=list[RegionOut], tags=["regions"])
def list_regions():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            rows = repository.list_regions(cur)
    return [RegionOut.model_validate(r) for r in rows]


REGION_CHANGE_PAGE_MAX = 500


@app.get("/api/regions/changes", response_model=RegionChangePage, tags=["regions"])
def list_region_changes(
    cursor: int = Query(
        default=0, ge=0,
        description="全局 revision 游标：只返回 revision 严格大于该游标的变更记录；"
                    "首页传 0 或不传（不可为负）",
    ),
    limit: int = Query(
        default=100, ge=1, le=REGION_CHANGE_PAGE_MAX,
        description="每页记录数（1~500，默认 100）",
    ),
):
    """区域集合变更的**只读审计历史**：按全局 revision 升序分页。

    仅包含**成功提交**的区域写事务——单区登记（`create`）、修订
    （`update`）、删除（`delete`）与批量变更（`batch`，整批一行）；
    每条记录含 `revision`、`occurred_at`（发生时间）、`change_type`、
    `region_ids`（涉及区域编号，批量按请求操作顺序）与 `summary`
    （批量操作摘要：逐操作 action/region_id/version 及各类型计数）。

    失败事务（422/404/409）随回滚不留记录；幂等重试只回放原响应、不新增
    记录；通行凭证、择时承诺与安全状态观察的签发/取消不产生区域变更记录。

    查询在 `REPEATABLE READ READ ONLY` 快照中完成，游标为严格大于的
    revision 键集分页：查询期间有新变更提交也不会造成重复或遗漏。
    `next_cursor` 为本页最后一条记录的 revision，无更多记录时为 `null`；
    从无变更时返回空数组 `changes: []` 与 `next_cursor: null`。
    """
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                rows, has_more = repository.list_region_changes(cur, cursor, limit)
                page = {
                    "changes": [
                        {
                            "revision": r["revision"],
                            "occurred_at": r["occurred_at"],
                            "change_type": r["change_type"],
                            "region_ids": r["region_ids"],
                            "summary": r["summary"],
                        }
                        for r in rows
                    ],
                    "next_cursor": rows[-1]["revision"] if has_more and rows else None,
                }
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    return page


@app.get("/api/regions/{region_id}", response_model=RegionOut, tags=["regions"])
def get_region(region_id: int = Path(ge=1)):
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            cur.execute(
                """SELECT id, name, polygon, valid_from, valid_to, version,
                          created_at, updated_at
                   FROM regions WHERE id = %s""",
                (region_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"区域 {region_id} 不存在"}})
    return RegionOut.model_validate(row)


@app.patch("/api/regions/{region_id}", response_model=RegionCreatedResponse, tags=["regions"])
def update_region(payload: RegionUpdate, region_id: int = Path(ge=1),
                  idempotency_key: str | None = Header(
                      default=None,
                      description="幂等键：非空、最长 128 字符；"
                                  "同键同内容重试返回原响应，同键不同内容 409",
                  )):
    """修订区域（部分更新）；每次修订 version 加 1 并推进全局 revision。

    携带 `Idempotency-Key` 时，同键同内容重试返回首次修订的原响应，
    version/revision 不会再次推进。
    """
    key = _normalize_idempotency_key(idempotency_key)
    fingerprint = (
        _request_fingerprint("update", region_id, payload.model_dump(mode="json"))
        if key is not None else None
    )
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                if key is not None:
                    replay = repository.idempotency_claim(cur, key, fingerprint)
                    if replay is not None:
                        conn.rollback()  # 只读回放，不产生任何变更
                        return _raw_json_response(replay["response_body"],
                                                  replay["status_code"])
                result = repository.update_region(
                    cur, region_id, payload, payload.polygon
                )
                if result is None:
                    conn.rollback()
                    raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                                           "message": f"区域 {region_id} 不存在"}})
                region, revision = result
                response = RegionCreatedResponse(
                    region=RegionOut.model_validate(region), revision=revision
                )
                if key is not None:
                    body = response.model_dump_json()
                    repository.idempotency_store(cur, key, 200, body)
            except ValueError as exc:
                conn.rollback()
                raise DomainError(str(exc), ["valid_from", "valid_to"]) from exc
            except CheckViolation as exc:
                conn.rollback()
                raise DomainError("生效时段必须满足 valid_from < valid_to（左闭右开）") from exc
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    if key is not None:
        return _raw_json_response(body, 200)
    return response


@app.delete("/api/regions/{region_id}", response_model=RevisionOut, tags=["regions"])
def remove_region(region_id: int = Path(ge=1),
                  idempotency_key: str | None = Header(
                      default=None,
                      description="幂等键：非空、最长 128 字符；"
                                  "同键同内容重试返回原响应，同键不同内容 409",
                  )):
    """删除区域并推进全局 revision；不存在的区域返回 404。

    携带 `Idempotency-Key` 时，同键重试返回首次删除的原响应（200），
    不会因区域已不存在而变成 404，revision 也不会再次推进。
    """
    key = _normalize_idempotency_key(idempotency_key)
    fingerprint = (
        _request_fingerprint("delete", region_id, None)
        if key is not None else None
    )
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                if key is not None:
                    replay = repository.idempotency_claim(cur, key, fingerprint)
                    if replay is not None:
                        conn.rollback()  # 只读回放，不产生任何变更
                        return _raw_json_response(replay["response_body"],
                                                  replay["status_code"])
                deleted, revision = repository.delete_region(cur, region_id)
                if not deleted:
                    conn.rollback()
                    raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                                           "message": f"区域 {region_id} 不存在"}})
                response = RevisionOut(revision=revision)
                if key is not None:
                    body = response.model_dump_json()
                    repository.idempotency_store(cur, key, 200, body)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    if key is not None:
        return _raw_json_response(body, 200)
    return response


@app.get("/api/revision", response_model=RevisionOut, tags=["regions"])
def current_revision():
    """当前区域集合版本号；每次登记/修订/删除后单调递增。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            revision = repository.get_revision_shared(cur)
    return RevisionOut(revision=revision)


@app.post("/api/regions/batch", response_model=RegionBatchResponse, status_code=200,
          responses={
              409: {"description": "expected_revision 已过期（revision_conflict）"},
              404: {"description": "批内引用了不存在的区域（region_not_found）"},
          },
          tags=["regions"])
def batch_change_regions(payload: RegionBatchRequest,
                         idempotency_key: str | None = Header(
                             default=None,
                             description="幂等键：非空、最长 128 字符；"
                                         "同键同内容重试返回原响应，同键不同内容 409",
                         )):
    """把多处区域登记/修订/删除作为**一次原子变更**提交，并保证指定轨迹在
    变更后的完整区域集合下仍然安全。

    处理顺序（全部在一个读/写事务内，行锁按「meta 行 → 区域行（id 升序）」获取）：

    1. 锁定全局 `region_meta` 行并比对 `expected_revision`；不符返回
       409 `revision_conflict`（携带当前 revision），整批不落库。
    2. 同一现有区域在一批中只能出现一次（update/delete 共用 id 维度）；
       重复返回 422 `duplicate_region`。
    3. 按 id 升序锁定涉及的现有区域；任一不存在返回 404 `region_not_found`
       （列出操作下标与区域 id），整批不落库。
    4. 单条操作数据非法（如修订后 `valid_from >= valid_to`）返回
       422 `invalid_operation`（带操作下标）；多边形/时区等结构问题在请求
       校验阶段返回 422 `validation_error`/`invalid_polygon`。
    5. 在内存中构造**变更后完整区域集合**（未涉及区域原样保留），对每条
       带时区绝对时刻轨迹按现有规则（直线匀速、边界接触即穿越、生效时段
       左闭右开）核验；任一轨迹不安全返回 422 `unsafe_trajectory`，明细
       列出对应轨迹下标与穿越区间，整批不落库。
    6. 未取消且末点时刻未过的通行凭证，其轨迹在变更后完整集合下仍须安全；
       任一冲突返回 409 `pass_conflict`（details 列出冲突凭证 ID），
       整批回滚，version/revision 不变。
    7. 仅当 revision 匹配、操作均合法、全部轨迹与全部约束凭证安全时，
       一次性落库：修订沿用 version +1，整批全局 revision 只递增一次。

    并发区域变更（含单个登记/修订/删除）与凭证签发/取消因同一把 meta 行锁
    在此串行化，无法绕过 revision 比对、轨迹核验与凭证约束检查。

    携带 `Idempotency-Key` 请求头时启用幂等重试：首次成功提交后，相同键 +
    相同请求内容的重试直接返回原状态与原响应（即使期间 revision 已被其它
    变更推进），整批不会再次落库，也不会重新分配区域 ID。
    """
    key = _normalize_idempotency_key(idempotency_key)
    # 轨迹合法性（点数、有限坐标、时刻严格递增）在几何层统一报错
    trajectories = [
        geometry.validate_trajectory(points, lambda p: p.t)
        for points in payload.trajectories
    ]
    fingerprint = (
        _request_fingerprint("batch", None, payload.model_dump(mode="json"))
        if key is not None else None
    )

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                if key is not None:
                    replay = repository.idempotency_claim(cur, key, fingerprint)
                    if replay is not None:
                        conn.rollback()  # 只读回放，不产生任何变更
                        return _raw_json_response(replay["response_body"],
                                                  replay["status_code"])
                result = repository.apply_batch(cur, payload, trajectories)
                out_trajectories = []
                for check in result["trajectory_results"]:
                    out_trajectories.append({
                        "index": check["index"],
                        "safe": check["safe"],
                        "results": _crossings_to_out(check["results"]),
                    })
                response = RegionBatchResponse(
                    revision=result["revision"],
                    operations=result["operations"],
                    trajectories=out_trajectories,
                )
                if key is not None:
                    # 响应原文与整批变更同事务落库；此后同键重试只回放、不再变更
                    body = response.model_dump_json()
                    repository.idempotency_store(cur, key, 200, body)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    if key is not None:
        return _raw_json_response(body, 200)
    return response


@app.post("/api/regions/batch/preview", response_model=RegionBatchPreviewResponse,
          responses={
              409: {"description": "expected_revision 已过期（revision_conflict）"},
              404: {"description": "批内引用了不存在的区域（region_not_found）"},
          },
          tags=["regions"])
def preview_batch_change(payload: RegionBatchRequest):
    """批量变更的**只读影响预演**：输入与 `POST /api/regions/batch` 相同
    （`expected_revision`、按序 `operations`、`trajectories`），在单个
    `REPEATABLE READ READ ONLY` 快照上评估「若现在提交会怎样」，但
    **不改变任何数据**——区域、凭证、承诺、观察、审计与 revision 均不变，
    也不为 create 分配正式区域编号（报告中的拟登记区域以操作下标标识）。

    报告内容：

    * `revision` / `evaluated_at`：预演所依据的已提交区域集合版本与基准
      时刻（凭证/承诺/观察的有效性判定与承诺安全区间裁剪共用该时刻）；
    * `trajectories`：每条待核验轨迹在**拟变更完整区域集合**下的安全结论
      与逐区域穿越明细（沿用直线匀速、边界接触即穿越、生效时段左闭右开；
      拟登记区域 `region_id` 为 null、`operation_index` 为操作下标）；
    * `pass_conflicts`：若提交将冲突的有效通行凭证（未取消且末点时刻未过、
      其轨迹在拟变更集合下不再安全）；
    * `commitment_conflicts`：变更前仍有未来安全时刻、变更后一点不剩的
      未取消且窗口未结束承诺；
    * `observation_flips`：未取消且末点时刻未过观察的结论净翻转
      （旧/新结论）。

    错误约定与正式批量一致：revision 过期 409 `revision_conflict`、
    未知区域 404 `region_not_found`、重复区域 422 `duplicate_region`、
    非法操作 422 `invalid_operation`、非法轨迹 422 `invalid_trajectory`。
    与正式提交不同：轨迹不安全、凭证/承诺冲突**不作为错误**，只写入报告。

    预演基于同一已提交快照，**不保证之后可提交**；正式提交仍按现有保护
    规则在写事务内重验。
    """
    # 轨迹合法性（点数、有限坐标、时刻严格递增）在几何层统一报错
    trajectories = [
        geometry.validate_trajectory(points, lambda p: p.t)
        for points in payload.trajectories
    ]

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            # REPEATABLE READ 只读事务：revision、区域、凭证、承诺与观察来自
            # 同一已提交快照；预演期间并发的变更不影响本次评估，本次评估也
            # 不改变任何数据、不占用区域编号
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                report = repository.preview_batch(cur, payload, trajectories)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    out_trajectories = []
    for check in report["trajectory_results"]:
        out_trajectories.append({
            "index": check["index"],
            "safe": check["safe"],
            "results": _preview_results_to_out(check["results"]),
        })
    return RegionBatchPreviewResponse(
        revision=report["revision"],
        evaluated_at=report["now"],
        trajectories=out_trajectories,
        pass_conflicts=[{"pass_id": pid} for pid in report["pass_conflicts"]],
        commitment_conflicts=[
            {"commitment_id": cid} for cid in report["commitment_conflicts"]
        ],
        observation_flips=report["observation_flips"],
    )


# ---------------------------------------------------------------- 通行凭证

@app.post("/api/passes", response_model=PassOut, status_code=201,
          responses={
              422: {"description": "轨迹非法、已开始或在当前区域集合下不安全"},
          },
          tags=["passes"])
def issue_pass(payload: PassIssueRequest):
    """为指定通行轨迹签发可取消的通行凭证。

    轨迹要求与 `/api/verify` 相同（至少 2 点、带时区绝对时刻、时刻严格递增、
    相邻点直线匀速），且**首点时刻必须晚于签发时刻**（以服务端拿到写锁后的
    当前时间为准）。签发在持有全局元数据行锁的写事务内完成：与取消及所有
    区域写入串行化，按当前完整区域集合核验（边界接触即违规、生效期
    左闭右开）。

    * 安全：持久化并返回 201，携带凭证 ID、签发时的区域 `revision` 与轨迹；
    * 不安全：422 `unsafe_trajectory`，details 列出穿越区域与起止明细，不落库；
    * 非法或已开始：422 `invalid_trajectory`/`validation_error`，不落库。
    """
    # 时刻严格递增等结构校验在几何层统一报错；先快速失败避免无谓的数据库读取
    traj = geometry.validate_trajectory(payload.points, lambda p: p.t)

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.issue_pass(cur, traj)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    return _pass_row_to_out(row)


@app.get("/api/passes/{pass_id}", response_model=PassOut, tags=["passes"])
def get_pass(pass_id: int = Path(ge=1)):
    """按 ID 查询凭证（含状态）；未知 ID 返回 404。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            row = repository.get_pass(cur, pass_id)
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"凭证 {pass_id} 不存在"}})
    return _pass_row_to_out(row)


@app.post("/api/passes/{pass_id}/cancel", response_model=PassOut, tags=["passes"])
def cancel_pass(pass_id: int = Path(ge=1)):
    """取消凭证并返回状态；重复取消幂等（结果不变），未知 ID 返回 404。

    取消与签发、所有区域写入在同一把全局元数据行锁上串行化。
    """
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.cancel_pass(cur, pass_id)
            except Exception:
                conn.rollback()
                raise
            else:
                if row is None:
                    conn.rollback()
                else:
                    conn.commit()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"凭证 {pass_id} 不存在"}})
    return _pass_row_to_out(row)


# ---------------------------------------------------------------- 择时通行承诺

@app.post("/api/commitments", response_model=CommitmentOut, status_code=201,
          responses={
              422: {"description": "轨迹/窗口非法、窗口起点不晚于签发时刻，"
                                   "或窗口内无任何安全出发时刻"},
          },
          tags=["commitments"])
def issue_commitment(payload: CommitmentIssueRequest):
    """为相对秒数固定行程申请择时通行承诺。

    轨迹点用相对首点的秒数给出（首点必须为 0、其余严格递增）；候选出发
    窗口 `[window_start, window_end)` 带时区，且**窗口起点必须晚于签发时刻**
    （以服务端拿到写锁后的当前时间为准）。签发在持有全局元数据行锁的写
    事务内完成：与取消及所有区域写入串行化，按**当前完整区域集合**用
    `/api/schedule` 的同一精确区间运算求窗口内左闭右开的安全出发区间。

    * 存在安全出发区间：持久化并返回 201，携带承诺 ID、签发时的区域
      `revision` 与当前安全区间；签发不推进区域 revision；
    * 区间为空：422 `unsafe_trajectory`，拒签且不落库；
    * 轨迹/窗口非法或窗口起点不晚于当前时刻：422
      `invalid_trajectory`/`invalid_input`，不落库。

    未取消且窗口未结束的承诺会约束后续区域写入（见各区域写接口）：
    区域单项变更与批量变更提交前，系统在同一写事务内以变更后的完整集合
    重算当前时刻之后的安全区间；变更前仍有未来安全时刻而变更后没有时，
    返回 409 `commitment_conflict`（details 列出冲突承诺 ID），整笔回滚；
    变更前已无未来安全时刻的承诺不再约束。
    """
    # 相对轨迹合法性（点数、有限坐标、首点 0、严格递增）在几何层统一报错
    traj = geometry.validate_relative_trajectory(
        payload.points, lambda p: (p.x, p.y), lambda p: p.t
    )
    if payload.window_start >= payload.window_end:
        raise DomainError(
            "候选出发窗口必须满足 window_start < window_end（左闭右开，起早于止）",
            ["window_start", "window_end"],
        )

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.issue_commitment(
                    cur, traj, payload.window_start, payload.window_end
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    intervals = row.pop("intervals")
    return _commitment_row_to_out(row, intervals)


def _active_commitment_view(cur, row: dict) -> dict:
    """在当前事务内按当前区域集合重算承诺的可用安全区间。"""
    revision, regions = repository.load_snapshot(cur)
    now_epoch = repository.utcnow().timestamp()
    intervals = repository.commitment_safe_intervals(
        [
            (float(p["t"]), float(p["x"]), float(p["y"]))
            for p in row["trajectory"]
        ],
        regions,
        row["window_start"].timestamp(),
        row["window_end"].timestamp(),
        now_epoch,
    )
    return _commitment_row_to_out(row, intervals)


@app.get("/api/commitments/{commitment_id}", response_model=CommitmentOut,
         tags=["commitments"])
def get_commitment(commitment_id: int = Path(ge=1)):
    """查询承诺及其**当前可用**安全出发区间（按当前区域集合与当前时刻重算）；
    已取消承诺的 intervals 恒为空；未知 ID 返回 404。"""
    with db.pool.connection() as conn:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                row = repository.get_commitment(cur, commitment_id)
                if row is None:
                    view = None
                elif row["status"] == "cancelled":
                    view = _commitment_row_to_out(row, [])
                else:
                    view = _active_commitment_view(cur, row)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    if view is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"承诺 {commitment_id} 不存在"}})
    return view


@app.post("/api/commitments/{commitment_id}/cancel", response_model=CommitmentOut,
          tags=["commitments"])
def cancel_commitment(commitment_id: int = Path(ge=1)):
    """取消承诺并返回状态；重复取消幂等（结果不变），未知 ID 返回 404。

    取消与签发、所有区域写入在同一把全局元数据行锁上串行化；取消不推进
    区域 revision。取消后承诺不再约束任何区域变更，响应 intervals 恒为空。
    """
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.cancel_commitment(cur, commitment_id)
            except Exception:
                conn.rollback()
                raise
            else:
                if row is None:
                    conn.rollback()
                else:
                    conn.commit()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"承诺 {commitment_id} 不存在"}})
    return _commitment_row_to_out(row, [])


@app.post("/api/commitments/{commitment_id}/fulfill",
          response_model=CommitmentFulfillResponse, status_code=201,
          responses={
              404: {"description": "未知承诺（not_found）"},
              409: {"description": "承诺已被兑现（commitment_already_fulfilled）"},
              422: {"description": "已取消 / 窗口已结束 / 出发时刻不晚于受理时刻 /"
                                   " 越出候选窗口 / 不在当前安全出发区间"},
          },
          tags=["commitments"])
def fulfill_commitment(
    payload: CommitmentFulfillRequest,
    commitment_id: int = Path(ge=1),
):
    """把一张**尚未取消、窗口未结束**的择时通行承诺兑现为一张**固定时刻
    通行凭证**。

    输入承诺编号（路径）与**带时区的出发时刻** `departure_time`（服务端
    归一化为 UTC）。在与区域变更、承诺取消及凭证签发共用的全局元数据行
    锁写事务内完成，全部检查在落库前的同一事务重验：

    * 出发时刻必须**晚于服务端受理时刻**（以拿到写锁后的当前时间为准）；
    * 必须落在承诺**原候选窗口** `[window_start, window_end)` 内；
    * 必须落在**按当前区域集合重算**的安全出发区间内（与
      `/api/schedule` 同一精确区间运算，左闭右开）；
    * 服务端按承诺的相对秒数轨迹生成绝对时刻轨迹
      （点 i 时刻 = departure_time + t_i），据此只签发一张通行凭证。

    成功返回 **201**：`commitment_id`、`pass`（结构与
    `GET /api/passes/{id}` 一致，`status=active`）与所据区域 `revision`。
    原承诺按**既有取消语义**失效（`status=cancelled`，`GET` 可经
    `fulfilled_pass_id` 找到凭证）；只生成一张凭证；**区域 revision 不
    因兑现改变**（不写区域变更审计）。新凭证即刻受既有 `pass_conflict`
    保护规则约束。

    失败均不给凭证且**不改变承诺**（除已取消/已兑现/已过期这些既成
    状态外）：未知承诺 **404** `not_found`；已兑现 **409**
    `commitment_already_fulfilled`（details 带已生成的 `pass_id`，
    与普通取消区分）；普通取消 **422** `commitment_cancelled`；窗口
    已结束 **422** `commitment_expired`；出发时刻不晚于受理时刻
    **422** `departure_not_in_future`；越出候选窗口 **422**
    `departure_out_of_window`；不在当前安全出发区间 **422**
    `unsafe_trajectory`（details 回显当前安全区间）。

    并发兑现/取消在同一把行锁上串行化：同一承诺至多一次兑现成功；
    后到的兑现请求按其终态得到 409 已兑现或 422 已取消，两种失败原因
    可区分。
    """
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                result = repository.fulfill_commitment(
                    cur, commitment_id, payload.departure_time
                )
            except Exception:
                conn.rollback()
                raise
            else:
                if result is None:
                    conn.rollback()
                else:
                    conn.commit()
    if result is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"承诺 {commitment_id} 不存在"}})
    return {
        "commitment_id": commitment_id,
        "revision": result["revision"],
        "pass": _pass_row_to_out(result["pass"]),
    }


# ---------------------------------------------------------------- 安全状态观察

OBSERVATION_EVENT_PAGE_MAX = 500


@app.post("/api/observations", response_model=ObservationOut, status_code=201,
          responses={
              422: {"description": "轨迹非法（点数不足/时刻不严格递增/首点不晚于登记时刻）"},
          },
          tags=["observations"])
def register_observation(payload: ObservationRegisterRequest):
    """为带时区绝对时刻轨迹登记可取消的安全状态观察。

    轨迹要求与 `/api/verify` 相同（至少 2 点、带时区绝对时刻、时刻严格
    递增、相邻点直线匀速），且**首点时刻必须晚于登记时刻**（以服务端拿到
    写锁后的当前时间为准，已开始的轨迹拒绝）。登记在持有全局元数据行锁的
    写事务内完成，按**当前完整区域集合**核验，沿用边界接触即穿越、生效期
    左闭右开规则：

    * 安全与不安全均可登记并返回 **201**，响应含观察 `id`、`status`、
      当前安全结论 `conclusion`（`safe`/`unsafe`）、该结论对应的区域
      `revision`、回显轨迹与 `registered_at`；登记不推进区域 revision，
      也不产生事件；
    * 轨迹非法或首点不晚于当前时刻：**422** `invalid_trajectory`/
      `validation_error`，不落库。

    区域变更（登记/修订/删除/批量）成功提交时，未取消且末点时刻未过的
    观察若安全结论翻转，会在**同一事务**内追加翻转事件（见
    `GET /api/observations/{id}/events`）；观察不阻止任何区域变更。
    """
    # 时刻严格递增等结构校验在几何层统一报错；先快速失败避免无谓的数据库读取
    traj = geometry.validate_trajectory(payload.points, lambda p: p.t)

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.register_observation(cur, traj)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    return _observation_row_to_out(row)


@app.get("/api/observations/{observation_id}", response_model=ObservationOut,
         tags=["observations"])
def get_observation(observation_id: int = Path(ge=1)):
    """按 ID 查询观察：返回轨迹、状态、最近安全结论及结论对应 revision；
    未知 ID 返回 404。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            row = repository.get_observation(cur, observation_id)
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"观察 {observation_id} 不存在"}})
    return _observation_row_to_out(row)


@app.post("/api/observations/{observation_id}/cancel", response_model=ObservationOut,
          tags=["observations"])
def cancel_observation(observation_id: int = Path(ge=1)):
    """取消观察并返回状态；重复取消幂等（结果不变），未知 ID 返回 404。

    取消与登记、所有区域写入在同一把全局元数据行锁上串行化；取消不推进
    区域 revision。已取消的观察不再对任何区域变更产生事件。
    """
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.cancel_observation(cur, observation_id)
            except Exception:
                conn.rollback()
                raise
            else:
                if row is None:
                    conn.rollback()
                else:
                    conn.commit()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"观察 {observation_id} 不存在"}})
    return _observation_row_to_out(row)


@app.get("/api/observations/{observation_id}/events",
         response_model=ObservationEventPage, tags=["observations"])
def get_observation_events(
    observation_id: int = Path(ge=1),
    cursor: int = Query(
        default=0, ge=0,
        description="事件序号游标：只返回 seq 严格大于该游标的事件；首页传 0 或不传",
    ),
    limit: int = Query(
        default=100, ge=1, le=OBSERVATION_EVENT_PAGE_MAX,
        description="每页事件数（1~500，默认 100）",
    ),
):
    """按观察编号与事件序号游标**升序分页**读取结论翻转事件。

    事件按 `seq`（观察内从 1 开始严格递增）升序返回；响应 `next_cursor`
    为下一页游标（即本页最后一条事件的 `seq`），无更多事件时为 `null`。
    下一页请求携带 `?cursor=<next_cursor>` 即可。观察编号未知返回 404
    （观察从未产生事件的已存在观察返回空页而非 404）。
    """
    with db.pool.connection() as conn:
        conn.rollback()
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            row = repository.get_observation(cur, observation_id)
            if row is None:
                view = None
            else:
                events, has_more = repository.list_observation_events(
                    cur, observation_id, cursor, limit
                )
                view = {
                    "observation_id": observation_id,
                    "events": [
                        {
                            "seq": e["seq"],
                            "old_conclusion": e["old_conclusion"],
                            "new_conclusion": e["new_conclusion"],
                            "revision": e["revision"],
                            "occurred_at": e["created_at"],
                        }
                        for e in events
                    ],
                    "next_cursor": events[-1]["seq"] if has_more and events else None,
                }
    if view is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"观察 {observation_id} 不存在"}})
    return view


# ---------------------------------------------------------------- 轨迹核验

@app.post("/api/verify", response_model=VerifyResponse, tags=["verification"])
def verify(payload: VerifyRequest):
    """对整段连续轨迹核验。

    整个核验在一个 REPEATABLE READ 只读事务中完成，因此并发修改区域时，
    本次核验使用的是同一已提交版本的区域集合（响应中带 revision）。
    """
    # 时刻严格递增在几何层统一报错；先快速失败避免无谓的数据库读取
    traj = geometry.validate_trajectory(payload.points, lambda p: p.t)

    with db.pool.connection() as conn:
        # REPEATABLE READ 只读事务：事务内所有读取共享同一快照，
        # 即使核验期间有区域被并发提交修改，本请求仍使用同一已提交版本的集合
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                # 几何计算在事务内完成：快照一直持有到本次核验结束
                results = geometry.verify_trajectory(traj, regions)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    checked_at = datetime.now(timezone.utc)
    safe = all(len(r["crossings"]) == 0 for r in results)

    return VerifyResponse(
        safe=safe,
        revision=revision,
        checked_at=checked_at,
        results=_crossings_to_out(results),
    )


@app.post("/api/schedule", response_model=ScheduleResponse, tags=["scheduling"])
def schedule(payload: ScheduleRequest):
    """计算固定行程在候选出发时段内的全部安全出发区间。

    轨迹点用相对首点的秒数给出（首点必须为 0、其余严格递增）；候选时段
    [window_start, window_end) 带时区，服务端归一化到 UTC。计算为精确的
    区间运算（不做离散抽样）：对每个区域、每段几何接触 [a,b]（闭）与生效
    窗口 [vf, vt)（左闭右开），其禁止出发时刻恰为半开区间 [vf-b, vt-a)；
    全部禁止区间取并集后从候选窗口中扣除。

    整个计算在一个 REPEATABLE READ 只读事务中完成，与 /api/verify 一样，
    并发修改区域时本请求使用同一已提交快照（响应中带 revision）。
    """
    traj = geometry.validate_relative_trajectory(
        payload.points, lambda p: (p.x, p.y), lambda p: p.t
    )
    if payload.window_start >= payload.window_end:
        raise DomainError(
            "候选时段必须满足 window_start < window_end（左闭右开，起早于止）",
            ["window_start", "window_end"],
        )

    window_start = payload.window_start.timestamp()
    window_end = payload.window_end.timestamp()

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                # 几何计算在事务内完成：快照一直持有到本次计算结束
                intervals = geometry.safe_departure_intervals(
                    traj, regions, window_start, window_end
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    out_intervals = [
        {
            "start": datetime.fromtimestamp(iv["start"], tz=timezone.utc),
            "end": datetime.fromtimestamp(iv["end"], tz=timezone.utc),
            "start_inclusive": iv["start_inclusive"],
            "end_inclusive": iv["end_inclusive"],
        }
        for iv in intervals
    ]

    return ScheduleResponse(
        revision=revision,
        window_start=payload.window_start,
        window_end=payload.window_end,
        intervals=out_intervals,
    )


@app.post("/api/exemptions/preview", response_model=ExemptionPreviewResponse,
          responses={
              409: {"description": "expected_revision 已过期（revision_conflict）"},
              404: {"description": "候选豁免编号引用了不存在的区域（region_not_found）"},
          },
          tags=["scheduling"])
def preview_exemption(payload: ExemptionPreviewRequest):
    """最小区域豁免预演（只读，不产生任何副作用）。

    输入相对秒数固定行程（首点 `t=0`、其余严格递增）、带时区候选出发窗口
    `[window_start, window_end)`、可豁免的互异现有区域编号与客户端已读的
    区域 `expected_revision`。预演只在本次计算中忽略被选区域，沿用
    `/api/schedule` 的直线匀速、边界接触即违规、生效时段左闭右开规则，
    从候选区域子集中找出使窗口内至少存在一个安全出发时刻的方案：

    1. 豁免区域数最少；
    2. 该方案下最早安全时刻最早；
    3. 仍并列时豁免编号升序序列字典序最小。

    返回所依据的 `revision`、豁免编号、最早安全时刻与该方案的完整安全
    出发区间；原本安全时豁免集为空；豁免全部候选区域仍不可行时返回
    `feasible=false`（豁免编号与最早时刻为 null）。

    * 候选编号重复：422 `duplicate_region`；引用不存在的区域：404
      `region_not_found`；轨迹/窗口非法：422
      `invalid_trajectory`/`invalid_input`/`validation_error`；
    * `expected_revision` 与当前 revision 不符：409 `revision_conflict`
      （details 带当前值）；
    * 整个预演在单个 `REPEATABLE READ READ ONLY` 事务中读取同一已提交
      区域快照，不改变区域、凭证、承诺、观察、审计记录与 revision。
    """
    # 相对轨迹合法性（点数、有限坐标、首点 0、严格递增）在几何层统一报错
    traj = geometry.validate_relative_trajectory(
        payload.points, lambda p: (p.x, p.y), lambda p: p.t
    )
    if payload.window_start >= payload.window_end:
        raise DomainError(
            "候选出发窗口必须满足 window_start < window_end（左闭右开，起早于止）",
            ["window_start", "window_end"],
        )
    # 候选豁免编号必须互异（与请求内容相关，先快速失败避免无谓的数据库读取）
    seen: set[int] = set()
    duplicates: set[int] = set()
    for rid in payload.candidate_region_ids:
        if rid in seen:
            duplicates.add(rid)
        seen.add(rid)
    if duplicates:
        raise ExemptionDuplicateRegions(sorted(duplicates))
    candidate_ids = sorted(seen)

    window_start = payload.window_start.timestamp()
    window_end = payload.window_end.timestamp()

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            # REPEATABLE READ 只读事务：revision 与区域集合来自同一已提交快照，
            # 预演期间并发的区域变更不影响本次计算，本次计算也不改变任何数据
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                if revision != payload.expected_revision:
                    raise ExemptionRevisionConflict(payload.expected_revision, revision)
                known = {r["id"] for r in regions}
                missing = [rid for rid in candidate_ids if rid not in known]
                if missing:
                    raise ExemptionRegionsNotFound(missing)
                plan = geometry.minimal_exemption_plan(
                    traj, regions, candidate_ids, window_start, window_end
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    out_intervals = [
        {
            "start": datetime.fromtimestamp(iv["start"], tz=timezone.utc),
            "end": datetime.fromtimestamp(iv["end"], tz=timezone.utc),
            "start_inclusive": iv["start_inclusive"],
            "end_inclusive": iv["end_inclusive"],
        }
        for iv in plan["intervals"]
    ]

    return ExemptionPreviewResponse(
        feasible=plan["feasible"],
        revision=revision,
        window_start=payload.window_start,
        window_end=payload.window_end,
        exempted_region_ids=plan["exempted"],
        earliest_departure=(
            datetime.fromtimestamp(plan["earliest"], tz=timezone.utc)
            if plan["earliest"] is not None else None
        ),
        intervals=out_intervals,
    )


@app.get("/health", tags=["meta"])
def health():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "临时限行核验服务",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /api/regions", "GET /api/regions", "GET /api/regions/{id}",
            "PATCH /api/regions/{id}", "DELETE /api/regions/{id}",
            "POST /api/regions/batch",
            "POST /api/regions/batch/preview",
            "GET /api/regions/changes",
            "GET /api/revision", "POST /api/verify", "POST /api/schedule",
            "POST /api/exemptions/preview",
            "POST /api/passes", "GET /api/passes/{id}",
            "POST /api/passes/{id}/cancel",
            "POST /api/commitments", "GET /api/commitments/{id}",
            "POST /api/commitments/{id}/cancel",
            "POST /api/commitments/{id}/fulfill",
            "POST /api/observations", "GET /api/observations/{id}",
            "POST /api/observations/{id}/cancel",
            "GET /api/observations/{id}/events",
            "GET /health",
        ],
    }
