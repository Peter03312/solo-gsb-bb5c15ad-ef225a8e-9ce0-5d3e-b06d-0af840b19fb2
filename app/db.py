"""PostgreSQL 连接池与初始化。

使用 psycopg3 的连接池；应用启动时自动建表并初始化全局版本号。
容器编排中数据库可能晚于应用启动，因此连接建立带重试。
"""
from __future__ import annotations

import logging
import os
import time

from psycopg import Cursor
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://restrict:restrict@localhost:5432/restrict",
)
DB_CONNECT_RETRIES = int(os.environ.get("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_INTERVAL = float(os.environ.get("DB_CONNECT_INTERVAL", "1.0"))

pool: ConnectionPool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS region_meta (
    id INTEGER PRIMARY KEY DEFAULT 1,
    revision BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT region_meta_singleton CHECK (id = 1)
);

INSERT INTO region_meta (id, revision)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS regions (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    valid_from  TIMESTAMPTZ NOT NULL,
    valid_to    TIMESTAMPTZ NOT NULL,
    -- GeoJSON 风格的多边形环：[[x, y], ...]，首尾点相同（闭合）
    polygon     JSONB NOT NULL,
    wkt         TEXT NOT NULL,
    version     BIGINT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT regions_time_half_open CHECK (valid_from < valid_to)
);

CREATE INDEX IF NOT EXISTS idx_regions_valid
    ON regions (valid_from, valid_to);

-- 可取消的通行凭证：签发时核验通过的轨迹；未取消且末点时刻未过的凭证
-- 约束后续所有区域写入（登记/修订/删除/批量变更）
CREATE TABLE IF NOT EXISTS passes (
    id              BIGSERIAL PRIMARY KEY,
    -- 签发时核验通过的轨迹：[{"x": .., "y": .., "t": <epoch 秒>}, ...]
    trajectory      JSONB NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    issued_revision BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,
    CONSTRAINT passes_status_check CHECK (status IN ('active', 'cancelled')),
    CONSTRAINT passes_time_order CHECK (start_time < end_time)
);

CREATE INDEX IF NOT EXISTS idx_passes_constraining
    ON passes (status, end_time);

-- 择时通行承诺：安全员以相对秒数轨迹与候选出发窗口申请的可取消承诺。
-- 签发时窗口内存在安全出发区间（左闭右开）；未取消且窗口未结束
-- （window_end 在当前时刻之后）的承诺约束后续所有区域写入：变更前仍有
-- 未来安全时刻而变更后没有时，整笔写入回滚（409 commitment_conflict）
CREATE TABLE IF NOT EXISTS commitments (
    id              BIGSERIAL PRIMARY KEY,
    -- 固定行程：[{"x": .., "y": .., "t": <相对首点秒数>}, ...]，首点 t=0
    trajectory      JSONB NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    issued_revision BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,
    -- 兑现（POST /api/commitments/{id}/fulfill）成功后置位：承诺按既有取消
    -- 语义失效（status='cancelled'、cancelled_at 置位），同时只生成一张
    -- 固定时刻通行凭证；普通取消的承诺两列恒为 NULL。
    -- pass id 不在数据库层加外键：凭证由兑现事务同写同生、承诺不可单独
    -- 删除，应用层保证引用有效（同时保持 passes/commitments 可独立清空）
    fulfilled_at      TIMESTAMPTZ,
    fulfilled_pass_id BIGINT,
    CONSTRAINT commitments_status_check CHECK (status IN ('active', 'cancelled')),
    CONSTRAINT commitments_window_order CHECK (window_start < window_end)
);

CREATE INDEX IF NOT EXISTS idx_commitments_constraining
    ON commitments (status, window_end);

-- 既有库升级：为承诺兑现补充两个可空列（与建表语句一致，不改变任何现有行）
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS fulfilled_at TIMESTAMPTZ;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS fulfilled_pass_id BIGINT;

-- 安全状态观察：安全员为一条带时区绝对时刻轨迹登记的可取消观察；
-- 安全或不安全均可登记。登记时记录当前安全结论（safe/unsafe）与当时的
-- 区域 revision。区域变更（登记/修订/删除/批量）成功提交时，对未取消且
-- 末点时刻未过的观察比较变更前后结论，仅结论翻转才在同一事务追加事件，
-- 整批变更对同一观察至多一条事件
CREATE TABLE IF NOT EXISTS observations (
    id              BIGSERIAL PRIMARY KEY,
    -- 登记时的轨迹：[{"x": .., "y": .., "t": <epoch 秒>}, ...]
    trajectory      JSONB NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    -- 登记时的区域集合 revision
    registered_revision BIGINT NOT NULL,
    -- 最近一次安全结论及其对应的区域 revision（随翻转事件在同一事务更新）
    conclusion      TEXT NOT NULL,
    conclusion_revision BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,
    CONSTRAINT observations_status_check CHECK (status IN ('active', 'cancelled')),
    CONSTRAINT observations_time_order CHECK (start_time < end_time),
    CONSTRAINT observations_conclusion_check
        CHECK (conclusion IN ('safe', 'unsafe'))
);

CREATE INDEX IF NOT EXISTS idx_observations_constraining
    ON observations (status, end_time);

-- 观察结论翻转事件：每条事件对应一次成功的区域变更（同一写事务写入）。
-- seq 为观察内从 1 开始、严格递增的事件序号，用于按序号游标升序分页
CREATE TABLE IF NOT EXISTS observation_events (
    id              BIGSERIAL PRIMARY KEY,
    observation_id  BIGINT NOT NULL REFERENCES observations (id),
    seq             BIGINT NOT NULL,
    old_conclusion  TEXT NOT NULL,
    new_conclusion  TEXT NOT NULL,
    -- 引起本次翻转的区域变更之后的新全局 revision
    revision        BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT observation_events_conclusion_check
        CHECK (old_conclusion IN ('safe', 'unsafe')
               AND new_conclusion IN ('safe', 'unsafe')
               AND old_conclusion <> new_conclusion),
    CONSTRAINT observation_events_seq_check CHECK (seq >= 1),
    CONSTRAINT observation_events_unique_seq UNIQUE (observation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_observation_events_obs_seq
    ON observation_events (observation_id, seq);

-- 幂等键：区域写操作（登记/修订/删除/批量变更）的网络超时安全重试。
-- 占位行与业务变更、响应回填在同一事务提交，因此已提交的行必然带有
-- 完整的原 HTTP 状态码与响应体；业务失败回滚时占位行一并消失（不占键）
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT PRIMARY KEY,
    -- 操作类型 + 目标区域 + 规范化请求内容的 SHA-256，用于判定是否同一次请求
    fingerprint   TEXT NOT NULL,
    status_code   INTEGER,
    -- 首次成功提交的原响应体（JSON 原文），重试时逐字节回放
    response_body TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 区域集合变更审计历史：每个成功的区域写事务（单区登记/修订/删除与批量
-- 变更）在同一事务恰好追加一行，revision 即该次变更后的全局版本号（主键
-- 保证一次变更一条）。失败回滚的事务不产生行；幂等重试回放不重新执行写
-- 路径，也不会追加；凭证/承诺/观察的签发与取消不写本表。
-- change_type: create/update/delete（单区）或 batch（批量变更，整批一行）；
-- region_ids 为本次涉及的区域编号（批量按请求操作顺序，create 为新分配 id）；
-- summary 为操作摘要 JSON（逐操作 action/region_id/version 与各类型计数）。
CREATE TABLE IF NOT EXISTS region_change_audit (
    revision    BIGINT PRIMARY KEY,
    change_type TEXT NOT NULL,
    region_ids  BIGINT[] NOT NULL,
    summary     JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT region_change_audit_type_check
        CHECK (change_type IN ('create', 'update', 'delete', 'batch'))
);
"""


def _init_schema(cur: Cursor) -> None:
    # CREATE TABLE 与 INSERT 分开执行，避免在一个多语句调用里混入副作用
    for statement in _SCHEMA.split(";"):
        stmt = statement.strip()
        if stmt:
            cur.execute(stmt)
    # 早期版本曾以数据库外键 commitments_fulfilled_pass_id_fkey 引用
    # passes(id)：改由应用层保证引用有效（凭证与承诺同事务写入、承诺不可
    # 单独删除，且 passes/commitments 需能被运维脚本独立 TRUNCATE）
    cur.execute(
        """
        SELECT 1 FROM pg_constraint
        WHERE conname = 'commitments_fulfilled_pass_id_fkey'
        """
    )
    if cur.fetchone() is not None:
        cur.execute(
            "ALTER TABLE commitments DROP CONSTRAINT commitments_fulfilled_pass_id_fkey"
        )


def init_pool() -> ConnectionPool:
    global pool
    last_exc: Exception | None = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
        try:
            pool.open()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    _init_schema(cur)
            logger.info("数据库连接池已就绪，schema 已初始化")
            return pool
        except Exception as exc:  # 数据库未就绪时重试
            last_exc = exc
            logger.warning("等待数据库就绪（第 %s/%s 次）：%s",
                           attempt, DB_CONNECT_RETRIES, exc)
            pool.close()
            time.sleep(DB_CONNECT_INTERVAL)
    raise RuntimeError(f"无法在 {DB_CONNECT_RETRIES} 次重试后连接数据库: {last_exc}")


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None
