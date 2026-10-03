"""HTTP 请求/响应模型与输入校验。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from . import geometry


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    if not math.isfinite(float(value)):
        raise ValueError(f"{label}必须是有限数，不能为 NaN/Infinity")
    return float(value)


def _require_utc(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{field}必须带时区信息（请使用 UTC，例如 2026-09-30T00:00:00Z）")
    return dt.astimezone(timezone.utc)


class PointIn(BaseModel):
    x: float
    y: float
    t: datetime

    model_config = ConfigDict(extra="forbid")

    @field_validator("x", "y", mode="before")
    @classmethod
    def _check_coord(cls, v):
        return _finite(v, "坐标")

    @field_validator("t")
    @classmethod
    def _check_t(cls, v: datetime) -> datetime:
        return _require_utc(v, "轨迹点时刻")


class RegionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    polygon: list[list[float]] = Field(description="简单多边形顶点 [[x,y], ...]，可闭合可不闭合")
    valid_from: datetime
    valid_to: datetime

    model_config = ConfigDict(extra="forbid")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        # 先做有限数检查，再交给 Shapely 做简单多边形判定
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)

    def validate_time_order(self) -> None:
        if self.valid_from >= self.valid_to:
            raise ValueError(
                "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）"
            )


class RegionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    polygon: list[list[float]] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v, info):
        return None if v is None else _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)


class RegionCreateInBatch(BaseModel):
    """批量变更中的登记操作（action 固定为 create）。"""

    action: Literal["create"] = "create"
    name: str = Field(min_length=1, max_length=200)
    polygon: list[list[float]] = Field(description="简单多边形顶点 [[x,y], ...]，可闭合可不闭合")
    valid_from: datetime
    valid_to: datetime

    model_config = ConfigDict(extra="forbid")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)

    def validate_time_order(self) -> None:
        if self.valid_from >= self.valid_to:
            raise ValueError(
                "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）"
            )


class RegionUpdateInBatch(BaseModel):
    """批量变更中的修订操作（action 固定为 update，字段任意组合的部分更新）。"""

    action: Literal["update"] = "update"
    region_id: int = Field(ge=1, alias="id")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    polygon: list[list[float]] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _check_time(cls, v, info):
        return None if v is None else _require_utc(v, info.field_name)

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("polygon 必须是顶点数组")
        for i, point in enumerate(v):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"第 {i} 个顶点必须是 [x, y] 形式")
            _finite(point[0], f"第 {i} 个顶点 x")
            _finite(point[1], f"第 {i} 个顶点 y")
        return geometry.validate_polygon(v)


class RegionDeleteInBatch(BaseModel):
    """批量变更中的删除操作（action 固定为 delete）。"""

    action: Literal["delete"] = "delete"
    region_id: int = Field(ge=1, alias="id")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


BatchOperation = Annotated[
    RegionCreateInBatch | RegionUpdateInBatch | RegionDeleteInBatch,
    Field(discriminator="action"),
]


class RegionBatchRequest(BaseModel):
    """区域集合的一次性原子变更：登记/修订/删除 + 指定轨迹安全核验。"""

    expected_revision: int = Field(ge=0, description="客户端预期的全局 revision")
    operations: list[BatchOperation] = Field(
        min_length=1, description="区域操作列表，元素为 create/update/delete 之一"
    )
    trajectories: list[list[PointIn]] = Field(
        min_length=1, description="至少一条带时区绝对时刻轨迹；每条至少 2 个点"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("trajectories")
    @classmethod
    def _check_trajectories(cls, v):
        for i, pts in enumerate(v):
            if not isinstance(pts, list) or len(pts) < 2:
                raise ValueError(f"第 {i} 条轨迹至少需要 2 个坐标点")
        return v


class ReservationCreateRequest(BaseModel):
    """预约一次未来的批量限行区域调整。

    execute_at 为带时区的执行时刻（服务端归一化为 UTC，且必须晚于受理
    时刻）；expected_revision/operations/trajectories 与
    `POST /api/regions/batch` 完全一致。受理时只校验输入格式，不预占
    revision、不保证届时可提交；到期才在写事务内重验全部提交条件。
    """

    execute_at: datetime = Field(
        description="带时区的执行时刻（须晚于服务端受理时刻）"
    )
    expected_revision: int = Field(ge=0, description="客户端预期的全局 revision")
    operations: list[BatchOperation] = Field(
        min_length=1, description="区域操作列表，元素为 create/update/delete 之一"
    )
    trajectories: list[list[PointIn]] = Field(
        min_length=1, description="至少一条带时区绝对时刻轨迹；每条至少 2 个点"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("execute_at")
    @classmethod
    def _check_execute_at(cls, v: datetime) -> datetime:
        return _require_utc(v, "execute_at")

    @field_validator("trajectories")
    @classmethod
    def _check_trajectories(cls, v):
        for i, pts in enumerate(v):
            if not isinstance(pts, list) or len(pts) < 2:
                raise ValueError(f"第 {i} 条轨迹至少需要 2 个坐标点")
        return v


class VerifyRequest(BaseModel):
    points: list[PointIn] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class PassIssueRequest(BaseModel):
    """签发通行凭证：一条带时区绝对时刻的连续轨迹（结构同 /api/verify）。"""

    points: list[PointIn] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class ObservationRegisterRequest(BaseModel):
    """登记安全状态观察：一条带时区绝对时刻的连续轨迹（结构同 /api/verify）。

    安全或不安全均可登记；轨迹至少 2 点、时刻严格递增，且首点时刻必须
    晚于登记时刻（服务端拿到写锁后的当前时间）。
    """

    points: list[PointIn] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class RelativePointIn(BaseModel):
    """调度请求中的轨迹点：相对首点的秒数（首点必须为 0）。"""

    x: float
    y: float
    t: float = Field(description="相对首点的秒数；首点必须为 0，其余严格递增")

    model_config = ConfigDict(extra="forbid")

    @field_validator("x", "y", "t", mode="before")
    @classmethod
    def _check_finite(cls, v, info):
        return _finite(v, {"x": "坐标 x", "y": "坐标 y", "t": "相对时刻"}.get(
            info.field_name, info.field_name))


class ScheduleRequest(BaseModel):
    """固定行程的安全出发时刻计算请求。"""

    points: list[RelativePointIn] = Field(min_length=2)
    window_start: datetime = Field(description="候选出发时段起点（含，带时区）")
    window_end: datetime = Field(description="候选出发时段终点（不含，带时区）")

    model_config = ConfigDict(extra="forbid")

    @field_validator("window_start", "window_end")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)


class CommitmentIssueRequest(BaseModel):
    """择时通行承诺签发请求：相对秒数固定行程 + 带时区候选出发窗口。

    轨迹与窗口语义与 /api/schedule 相同（首点相对秒数为 0、其余严格递增；
    窗口左闭右开），且窗口起点必须晚于签发时刻。
    """

    points: list[RelativePointIn] = Field(min_length=2)
    window_start: datetime = Field(description="候选出发窗口起点（含，带时区），须晚于签发时刻")
    window_end: datetime = Field(description="候选出发窗口终点（不含，带时区）")

    model_config = ConfigDict(extra="forbid")

    @field_validator("window_start", "window_end")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)


class CommitmentFulfillRequest(BaseModel):
    """择时通行承诺兑现请求：一个带时区的出发时刻。

    出发时刻必须晚于服务端受理时刻（以服务端拿到写锁后的当前时间为准）、
    落在承诺原候选窗口（左闭右开）内、且落在按当前区域集合重算的安全
    出发区间内；服务端据承诺的相对轨迹生成绝对时刻轨迹并签发一张固定
    时刻通行凭证。
    """

    departure_time: datetime = Field(
        description="带时区的出发时刻（须晚于受理时刻、落在候选窗口与当前安全出发区间内）"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("departure_time")
    @classmethod
    def _check_time(cls, v: datetime) -> datetime:
        return _require_utc(v, "departure_time")


class ExemptionPreviewRequest(BaseModel):
    """最小区域豁免预演请求：相对秒数固定行程 + 带时区候选出发窗口 +
    可豁免的互异现有区域编号 + 客户端已读的区域 revision。

    轨迹与窗口语义与 /api/schedule 相同（首点相对秒数为 0、其余严格递增；
    窗口左闭右开）。预演为只读计算：不修改任何数据，也不推进 revision。
    """

    points: list[RelativePointIn] = Field(min_length=2)
    window_start: datetime = Field(description="候选出发窗口起点（含，带时区）")
    window_end: datetime = Field(description="候选出发窗口终点（不含，带时区）")
    candidate_region_ids: list[int] = Field(
        description="可豁免的现有区域编号（必须互异；空列表表示不允许豁免任何区域）"
    )
    expected_revision: int = Field(ge=0, description="客户端已读的区域集合 revision")

    model_config = ConfigDict(extra="forbid")

    @field_validator("window_start", "window_end")
    @classmethod
    def _check_time(cls, v: datetime, info):
        return _require_utc(v, info.field_name)

    @field_validator("candidate_region_ids", mode="before")
    @classmethod
    def _check_ids(cls, v):
        if not isinstance(v, list):
            return v  # 非数组交由 pydantic 报类型错误
        for rid in v:
            if isinstance(rid, bool) or not isinstance(rid, int) or rid < 1:
                raise ValueError("可豁免区域编号必须为正整数")
        return v


# ---------------------------------------------------------------- 响应模型

def _iso_z(dt: datetime) -> str:
    """输出 UTC ISO8601 并带 Z 后缀。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _UTCDatetimeModel(BaseModel):
    """所有 datetime 字段统一输出 UTC ISO8601 且带 Z 后缀。"""

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_dt(self, value, _info):  # pragma: no cover - 见具体字段序列化
        if isinstance(value, datetime):
            return _iso_z(value)
        return value


class CrossingOut(_UTCDatetimeModel):
    entry_time: datetime
    exit_time: datetime
    entry_inclusive: bool
    exit_inclusive: bool
    point_contact: bool


class RegionResultOut(_UTCDatetimeModel):
    region_id: int
    region_name: str
    crossings: list[CrossingOut]


class RegionOut(_UTCDatetimeModel):
    id: int
    name: str
    polygon: list[list[float]]
    valid_from: datetime
    valid_to: datetime
    version: int
    created_at: datetime
    updated_at: datetime


class VerifyResponse(_UTCDatetimeModel):
    safe: bool
    revision: int
    checked_at: datetime
    results: list[RegionResultOut]


class DepartureIntervalOut(_UTCDatetimeModel):
    start: datetime
    end: datetime
    start_inclusive: bool
    end_inclusive: bool


class ScheduleResponse(_UTCDatetimeModel):
    revision: int
    window_start: datetime
    window_end: datetime
    intervals: list[DepartureIntervalOut]


class ExemptionPreviewResponse(_UTCDatetimeModel):
    """最小区域豁免预演结果。

    feasible=false 表示即使豁免全部候选区域，窗口内仍不存在安全出发时刻，
    此时 exempted_region_ids/earliest_departure 为 null、intervals 为空；
    feasible=true 时 exempted_region_ids 为最优方案的豁免编号（升序，
    原本安全则为空列表），earliest_departure 为该方案下最早安全出发时刻，
    intervals 为该方案的完整安全出发区间（左闭右开）。
    revision 为本次预演所依据的已提交区域快照版本。
    """

    feasible: bool
    revision: int
    window_start: datetime
    window_end: datetime
    exempted_region_ids: list[int] | None
    earliest_departure: datetime | None
    intervals: list[DepartureIntervalOut]


class CommitmentOut(_UTCDatetimeModel):
    """择时通行承诺：签发/查询/取消的统一响应。

    status: active（未取消）/ cancelled（已取消，含已被兑现而按取消语义
    失效的承诺）；revision 为签发时核验所用的区域集合版本号。
    intervals 为「当前时刻」视角的安全出发区间（相对 now 裁剪后的
    左闭右开区间）；取消/兑现承诺时 intervals 恒为空（承诺已失效）。
    fulfilled_pass_id 非空表示该承诺已被兑现为指定编号的固定时刻通行
    凭证（普通取消时为 null，active 承诺恒为 null）。
    """

    id: int
    status: str
    revision: int
    trajectory: list[RelativePointIn]
    window_start: datetime
    window_end: datetime
    intervals: list[DepartureIntervalOut]
    issued_at: datetime
    cancelled_at: datetime | None
    fulfilled_pass_id: int | None = None


class RegionCreatedResponse(_UTCDatetimeModel):
    region: RegionOut
    revision: int


class BatchOperationResultOut(BaseModel):
    """单个操作的结果。

    create：带新分配的 region_id 与 version=1；
    update：带修订后的 version（原 version +1）；
    delete：只回显被删除的 region_id 与删除前 version。
    """

    index: int
    action: str
    region_id: int
    version: int | None = None


class TrajectoryCheckOut(_UTCDatetimeModel):
    """批量变更中单条轨迹对变更后集合的核验结果。"""

    index: int
    safe: bool
    results: list[RegionResultOut]


class RegionBatchResponse(_UTCDatetimeModel):
    revision: int
    operations: list[BatchOperationResultOut]
    trajectories: list[TrajectoryCheckOut]


# ---------------------------------------------------------------- 批量调整预约

class ReservationResultOut(_UTCDatetimeModel):
    """预约到期执行成功的提交结果（与批量变更成功响应同构）。"""

    revision: int
    operations: list[BatchOperationResultOut]
    trajectories: list[TrajectoryCheckOut]


class ReservationFailureOut(BaseModel):
    """预约到期执行失败的可查询原因（沿用批量变更错误码与明细结构）。"""

    code: str
    message: str
    details: list[dict]


# ---------------------------------------------------------------- 批量变更影响预演

class BatchPreviewRegionResultOut(_UTCDatetimeModel):
    """影响预演中单个区域的穿越明细。

    拟登记（create）区域不分配正式编号：region_id 为 null，以
    operation_index（操作下标）标识；其余区域 operation_index 为 null。
    """

    region_id: int | None
    operation_index: int | None
    region_name: str
    crossings: list[CrossingOut]


class BatchPreviewTrajectoryOut(_UTCDatetimeModel):
    """单条待核验轨迹在拟变更完整区域集合下的安全结论与逐区域穿越明细。"""

    index: int
    safe: bool
    results: list[BatchPreviewRegionResultOut]


class BatchPreviewPassConflictOut(BaseModel):
    """若提交将冲突的有效通行凭证（未取消且末点时刻未过）。"""

    pass_id: int


class BatchPreviewCommitmentConflictOut(BaseModel):
    """若提交将被清空的择时承诺（变更前仍有未来安全时刻、变更后一点不剩）。"""

    commitment_id: int


class BatchPreviewObservationFlipOut(BaseModel):
    """有效观察（未取消且末点时刻未过）在拟变更前后的结论净翻转。"""

    observation_id: int
    old_conclusion: str
    new_conclusion: str


class RegionBatchPreviewResponse(_UTCDatetimeModel):
    """批量变更影响预演报告（只读，不产生任何副作用）。

    revision 为预演所依据的已提交区域集合版本（与 expected_revision 一致）；
    evaluated_at 为本次预演的基准时刻：凭证/承诺/观察的有效性判定与承诺
    安全区间裁剪共用该时刻。
    """

    revision: int
    evaluated_at: datetime
    trajectories: list[BatchPreviewTrajectoryOut]
    pass_conflicts: list[BatchPreviewPassConflictOut]
    commitment_conflicts: list[BatchPreviewCommitmentConflictOut]
    observation_flips: list[BatchPreviewObservationFlipOut]


class RevisionOut(BaseModel):
    revision: int


class PassPointOut(_UTCDatetimeModel):
    """凭证轨迹点：时刻输出 UTC ISO8601 并带 Z 后缀。"""

    x: float
    y: float
    t: datetime


class PassOut(_UTCDatetimeModel):
    """通行凭证：签发/查询/取消的统一响应。

    status: active（未取消）/ cancelled（已取消）；
    revision 为签发时核验所用的区域集合版本号。
    """

    id: int
    status: str
    revision: int
    trajectory: list[PassPointOut]
    issued_at: datetime
    cancelled_at: datetime | None


class CommitmentFulfillResponse(_UTCDatetimeModel):
    """承诺兑现成功响应：承诺编号、唯一生成的固定时刻通行凭证与所据区域
    revision（兑现不推进区域 revision）。

    pass 字段的结构与 GET /api/passes/{id} 完全一致（status 恒为 active）。
    """

    commitment_id: int
    revision: int
    pass_: PassOut = Field(alias="pass")

    model_config = ConfigDict(populate_by_name=True)


class ReservationOut(_UTCDatetimeModel):
    """批量限行区域调整预约：受理/查询/取消的统一响应。

    status: pending（待执行）/ committed（已提交）/ failed（执行失败）/
    cancelled（已取消）；execute_at 为带时区执行时刻（UTC 归一化）；
    expected_revision、operations、trajectories 回显受理时的规范化请求；
    result 仅在 committed 时非空（提交结果），failure 仅在 failed 时非空
    （沿用批量变更错误码的失败原因）。
    """

    id: int
    status: str
    execute_at: datetime
    expected_revision: int
    operations: list[dict]
    trajectories: list[list[PassPointOut]]
    result: ReservationResultOut | None
    failure: ReservationFailureOut | None
    created_at: datetime
    executed_at: datetime | None
    cancelled_at: datetime | None


class ObservationOut(_UTCDatetimeModel):
    """安全状态观察：登记/查询/取消的统一响应。

    status: active（未取消）/ cancelled（已取消）；
    conclusion 为最近一次安全结论（safe/unsafe），conclusion_revision 为
    该结论对应的区域集合版本号（登记时即为当时 revision）；
    registered_at 为登记时刻。
    """

    id: int
    status: str
    trajectory: list[PassPointOut]
    conclusion: str
    revision: int
    registered_at: datetime
    cancelled_at: datetime | None


class ObservationEventOut(_UTCDatetimeModel):
    """观察结论翻转事件：一次成功区域变更导致 safe↔unsafe 翻转的记录。

    seq 为观察内从 1 开始严格递增的事件序号；revision 为引起翻转的区域
    变更之后的新全局 revision。
    """

    seq: int
    old_conclusion: str
    new_conclusion: str
    revision: int
    occurred_at: datetime


class ObservationEventPage(_UTCDatetimeModel):
    """观察事件分页结果：按 seq 升序；next_cursor 为下一页游标（末页为 null）。"""

    observation_id: int
    events: list[ObservationEventOut]
    next_cursor: int | None


# ---------------------------------------------------------------- 区域集合变更审计

class RegionChangeOperationOut(BaseModel):
    """审计摘要中的单个操作（单区变更时恰有一项 index=0）。"""

    index: int
    action: str
    region_id: int
    version: int | None = None


class RegionChangeSummaryOut(BaseModel):
    """批量操作摘要：逐操作（保留请求下标）与各类型操作计数。"""

    operations: list[RegionChangeOperationOut]
    total: int
    create_count: int
    update_count: int
    delete_count: int


class RegionChangeOut(_UTCDatetimeModel):
    """一条区域集合变更审计记录（对应一次成功提交的区域写事务）。

    revision 为变更后的全局版本号，全库一条变更恰好一行；
    change_type 为 create/update/delete（单区）或 batch（批量变更，整批一行）；
    region_ids 为本次涉及的区域编号（批量按请求操作顺序，create 为新 id）；
    summary 为批量操作摘要（逐操作与类型计数，单区变更同样返回）。
    """

    revision: int
    occurred_at: datetime
    change_type: str
    region_ids: list[int]
    summary: RegionChangeSummaryOut


class RegionChangePage(_UTCDatetimeModel):
    """区域集合变更审计分页结果：按 revision 升序；末页 next_cursor 为 null。"""

    changes: list[RegionChangeOut]
    next_cursor: int | None


class ErrorOut(BaseModel):
    error: dict
