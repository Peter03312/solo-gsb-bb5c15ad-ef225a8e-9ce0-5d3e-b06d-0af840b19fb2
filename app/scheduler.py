"""到期预约批量变更的后台执行器。

一个守护线程周期性地把已到执行时刻、仍为 pending 的预约按
（执行时刻、编号）顺序领取并执行。每个预约在**与现有区域写入共用的
region_meta 事务锁**内重验 revision、操作、轨迹安全及凭证/承诺保护：

* 重验通过：批量变更（区域、revision、观察翻转事件、审计）与预约行的
  succeeded 终态及提交结果在**同一事务**提交；
* 重验失败：批量变更在保存点内回滚，预约行单独记为 failed 并写入沿用
  批量变更错误码的可查询原因，**区域集合不改变**。

领取使用 `FOR UPDATE SKIP LOCKED`（取消接口按同一加锁顺序等待同一行），
因此无论进程重启还是并发处理，同一预约至多被提交一次；取消与执行竞争
时先到的终态生效，只会产生一个终态。
"""
from __future__ import annotations

import json
import logging
import threading

from . import db, geometry, repository
from .repository import (
    BatchDuplicateRegion,
    BatchInvalidOperation,
    BatchRegionsNotFound,
    BatchRevisionConflict,
    BatchUnsafeTrajectories,
    CommitmentConflict,
    PassConflict,
)
from .schemas import RegionBatchRequest, RegionBatchResponse, RegionResultOut

logger = logging.getLogger("restriction-service.scheduler")

# 轮询间隔：到期后最坏滞后一个间隔（0.2s）；执行本身按数据库时刻串行，
# 顺序由 (execute_at, id) 领取顺序与 meta 事务锁保证
POLL_INTERVAL_SECONDS = 0.2


# ---------------------------------------------------------------- 失败原因序列化
#
# 失败原因与 main.py 的异常处理器使用同一组错误码与 details 结构，保证
# 查询接口读到的原因与「同一请求在到期时直接打 POST /api/regions/batch」
# 得到的错误信封可逐字段对应。

def _error(code: str, message: str, details=None) -> dict:
    return {"code": code, "message": message, "details": details or []}


def _batch_error_from_exception(exc: Exception) -> dict:
    """把批量变更重验阶段抛出的异常映射为 {code,message,details}（沿用批量错误码）。"""
    # 延迟导入以打破 main ↔ scheduler 的模块环（main 启动工作线程时才需要本模块）
    from .main import _crossings_to_out

    if isinstance(exc, BatchRevisionConflict):
        return _error(
            "revision_conflict",
            str(exc),
            [{"loc": ["expected_revision"],
              "message": f"预期 revision={exc.expected}，当前 revision={exc.current}",
              "current_revision": exc.current}],
        )
    if isinstance(exc, BatchRegionsNotFound):
        return _error(
            "region_not_found",
            str(exc),
            [{"loc": ["operations", str(m["index"]), "id"],
              "message": f"区域 {m['region_id']} 不存在",
              "operation_index": m["index"],
              "region_id": m["region_id"]}
             for m in exc.missing],
        )
    if isinstance(exc, BatchDuplicateRegion):
        return _error(
            "duplicate_region",
            str(exc),
            [{"loc": ["operations"],
              "message": f"区域 {exc.region_id} 在同一批中出现了多次",
              "region_id": exc.region_id,
              "operation_indices": exc.indices}],
        )
    if isinstance(exc, BatchInvalidOperation):
        loc = ["operations", str(exc.index)] + ([exc.field] if exc.field else [])
        return _error(
            "invalid_operation",
            exc.message,
            [{"loc": loc, "message": exc.message, "operation_index": exc.index}],
        )
    if isinstance(exc, BatchUnsafeTrajectories):
        details = []
        for t in exc.trajectory_results:
            if t["safe"]:
                continue
            violated = [r for r in t["results"] if r["crossings"]]
            dumped = [
                RegionResultOut.model_validate(r).model_dump(mode="json")
                for r in _crossings_to_out(violated)
            ]
            details.append({"trajectory_index": t["index"], "results": dumped})
        return _error(
            "unsafe_trajectory",
            "存在轨迹在变更后区域集合下发生穿越，整批变更已取消且不落库",
            details,
        )
    if isinstance(exc, PassConflict):
        return _error(
            "pass_conflict",
            str(exc),
            [{"pass_id": pid,
              "message": f"凭证 {pid}（未取消且末点时刻未过）的轨迹在变更后区域集合下不再安全"}
             for pid in exc.pass_ids],
        )
    if isinstance(exc, CommitmentConflict):
        return _error(
            "commitment_conflict",
            str(exc),
            [{"commitment_id": cid,
              "message": f"承诺 {cid}（未取消且窗口未结束）在变更前仍有未来安全出发时刻，"
                         f"变更后窗口结束前将不再有任何安全出发时刻"}
             for cid in exc.commitment_ids],
        )
    if isinstance(exc, geometry.TrajectoryError):
        return _error("invalid_trajectory", str(exc))
    if isinstance(exc, geometry.RegionError):
        return _error("invalid_polygon", str(exc))
    # 兜底：任何未预期的异常都以可查询错误码落终态，绝不破坏区域集合，
    # 也不让后台线程静默吞掉（完整堆栈另记日志）
    logger.exception("预约批量变更执行时发生未预期异常")
    return _error("scheduled_execution_error", f"执行时发生未预期错误：{exc}")


def _success_result(repo_result: dict) -> dict:
    """把 apply_batch 的成功结果转为与 POST /api/regions/batch 一致的响应 JSON。"""
    from .main import _crossings_to_out

    out_trajectories = []
    for check in repo_result["trajectory_results"]:
        out_trajectories.append({
            "index": check["index"],
            "safe": check["safe"],
            "results": _crossings_to_out(check["results"]),
        })
    response = RegionBatchResponse(
        revision=repo_result["revision"],
        operations=repo_result["operations"],
        trajectories=out_trajectories,
    )
    return json.loads(response.model_dump_json())


# ---------------------------------------------------------------- 单轮执行

def run_due_scheduled_batches(now=None) -> int:
    """执行所有已到期且仍为 pending 的预约（跨多批领取直到清空）；返回处理数。

    每个预约使用**独立事务**（事务先锁 region_meta 行、再按
    (execute_at, id) 顺序用 SKIP LOCKED 领取一行）：成功时区域变更与
    succeeded 终态同一事务提交；失败时保存点回滚区域变更，failed 终态
    与原因在外层事务单独提交，区域集合不改变。单预约的未预期错误不影响
    后续预约（其行保持 pending，下一轮重新领取）。循环持续领取直到没有
    到期行，避免大量积压时饿死。可在测试中直接调用以同步等待执行结果。
    """
    now = now or repository.utcnow()
    processed = 0
    while True:
        row = _claim_one_due(now)
        if row is None:
            break
        processed += 1
        _execute_claimed(row, now)
    return processed


def _claim_one_due(now) -> dict | None:
    """开启写事务，按「region_meta 行 → 到期预约行」顺序领取一行。

    领取到行时，把仍打开的事务（meta 行锁 + 预约行锁）连同池连接存入
    返回行的 "_conn" 键，交由 _execute_claimed 在同一事务上执行、提交
    或回滚并归还连接；没有到期行时立即回滚并归还连接，返回 None。
    """
    conn = db.pool.getconn()
    conn.rollback()  # 确保拿到的连接没有残留事务
    try:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            # 先锁全局 meta 行：与所有区域写入、预约取消同一加锁顺序
            cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
            cur.fetchone()
            rows = repository.claim_due_scheduled_batches(cur, now, 1)
            row = rows[0] if rows else None
    except Exception:
        conn.rollback()
        db.pool.putconn(conn)
        raise
    if row is None:
        conn.rollback()
        db.pool.putconn(conn)
        return None
    # 把仍打开的事务（meta 行锁 + 预约行锁）连同连接一起交给执行函数，
    # 由它提交或回滚并归还连接；附加键以 "_" 开头，落库时不会被读取
    row["_conn"] = conn
    return row


def _execute_claimed(row: dict, now) -> None:
    """在已持有 meta 行锁与该预约行锁的事务内执行单个预约。"""
    conn = row.pop("_conn")
    try:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            # 保存点隔离失败的批量变更：回滚保存点只撤销区域写入、revision
            # 推进、观察事件与审计，外层事务（meta 锁、预约行锁）仍然有效，
            # 随后把 failed 终态与失败原因写回预约行，连同外层事务提交
            cur.execute("SAVEPOINT scheduled_batch_apply")
            try:
                payload = RegionBatchRequest.model_validate(row["payload"])
                trajectories = [
                    geometry.validate_trajectory(points, lambda p: p.t)
                    for points in payload.trajectories
                ]
                repo_result = repository.apply_batch(cur, payload, trajectories)
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT scheduled_batch_apply")
                error = _batch_error_from_exception(exc)
                repository.mark_scheduled_batch_failed(cur, row, error, now)
            else:
                result = _success_result(repo_result)
                repository.mark_scheduled_batch_succeeded(
                    cur, row, repo_result["revision"], result, now
                )
        conn.commit()
    except Exception:
        # 连终态写回都失败（如数据库故障）：整笔回滚，预约仍为 pending，
        # 下一轮/重启后重新领取执行，不存在「已改区域却无终态」的中间态
        conn.rollback()
        logger.exception("预约 %s 执行事务失败，将在下个周期重新领取", row.get("id"))
    finally:
        db.pool.putconn(conn)


# ---------------------------------------------------------------- 后台线程

class ScheduledBatchWorker:
    """到期预约执行器守护线程。

    应用启动时 start()，关闭时 stop()。线程只在数据库上工作，FastAPI 的
    请求线程池与它互不阻塞；应用崩溃/重启时，未完成的 pending 预约会在
    下次启动后由新的工作线程按到期顺序重新领取（SKIP LOCKED 保证不会
    与其它实例重复提交）。
    """

    def __init__(self, interval: float = POLL_INTERVAL_SECONDS):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="scheduled-batch-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_due_scheduled_batches()
            except Exception:
                # 单轮失败不应杀死工作线程：下一轮重试（未提交的预约仍为
                # pending；已被标记终态的行不会再次领取）
                logger.exception("到期预约批量变更轮询失败，将在下个周期重试")
            self._stop.wait(self.interval)


worker = ScheduledBatchWorker()
