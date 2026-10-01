"""SQLite 连接与 schema。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 预售活动（可取消；取消后新支付进入异常款，不再发货）
CREATE TABLE IF NOT EXISTS activity (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    image           TEXT NOT NULL DEFAULT '',
    color           TEXT NOT NULL DEFAULT '#2563eb',
    total_quota     INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'on_sale',  -- on_sale | cancelled
    cancelled_at    TEXT,
    created_at      TEXT NOT NULL
);

-- 定金规则按版本管理：下单时快照锁定规则版本号与具体金额/时间
CREATE TABLE IF NOT EXISTS deposit_rule (
    id              INTEGER PRIMARY KEY,
    activity_id     INTEGER NOT NULL REFERENCES activity(id),
    version         INTEGER NOT NULL,
    deposit_amount  INTEGER NOT NULL,          -- 定金，分
    balance_amount  INTEGER NOT NULL,          -- 尾款，分
    deposit_open    TEXT NOT NULL,
    deposit_close   TEXT NOT NULL,
    balance_open    TEXT NOT NULL,
    balance_close   TEXT NOT NULL,             -- 尾款截止
    release_grace_sec INTEGER NOT NULL DEFAULT 0,  -- 截止后宽限秒数才释放
    created_at      TEXT NOT NULL,
    UNIQUE (activity_id, version)
);

CREATE TABLE IF NOT EXISTS sku (
    id              INTEGER PRIMARY KEY,
    activity_id     INTEGER NOT NULL REFERENCES activity(id),
    spec            TEXT NOT NULL,             -- 规格名，如 曜石黑 / 月光白
    extra_cents     INTEGER NOT NULL DEFAULT 0,-- 规格加价，分
    UNIQUE (activity_id, spec)
);

-- 批次：尾款付清的订单按批次配货发货
CREATE TABLE IF NOT EXISTS shipment_batch (
    id              INTEGER PRIMARY KEY,
    activity_id     INTEGER NOT NULL REFERENCES activity(id),
    label           TEXT NOT NULL,
    ship_after      TEXT NOT NULL,             -- 最早发货时间
    capacity        INTEGER,                   -- 该批容量，NULL 不限
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS "order" (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,      -- 业务单号
    activity_id     INTEGER NOT NULL REFERENCES activity(id),
    sku_id          INTEGER NOT NULL REFERENCES sku(id),
    rule_version    INTEGER NOT NULL,          -- 下单锁定的规则版本
    deposit_amount  INTEGER NOT NULL,          -- 快照：定金（含规格加价全部计入尾款前的定价）
    balance_amount  INTEGER NOT NULL,          -- 快照：尾款（= 规则尾款 + 规格加价）
    total_amount    INTEGER NOT NULL,          -- 快照：合计
    deposit_open    TEXT NOT NULL,             -- 快照：付款窗口
    deposit_close   TEXT NOT NULL,
    balance_open    TEXT NOT NULL,
    balance_close   TEXT NOT NULL,
    release_deadline TEXT NOT NULL,            -- 截止 + 宽限
    release_grace_sec INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,             -- 见 order.status 说明
    -- awaiting_deposit 待付定金 / deposit_paid 定金已付占名额 / awaiting_balance 尾款期中 /
    -- balance_partial 尾款部分支付 / paid 已付清待配货 / allocated 已配货 / shipped 已发货 /
    -- released 逾期释放 / refunding 退款处理中 / refunded 已退款 / cancelled 主动取消
    batch_id        INTEGER REFERENCES shipment_batch(id),
    deposit_paid_at TEXT,
    paid_off_at     TEXT,
    allocated_at    TEXT,
    shipped_at      TEXT,
    released_at     TEXT,
    cancelled_at    TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_activity ON "order"(activity_id);
CREATE INDEX IF NOT EXISTS idx_order_status ON "order"(status);

-- 支付流水（模拟支付网关侧也有自己的单号；本表同时承担幂等台账）
CREATE TABLE IF NOT EXISTS payment (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES "order"(id),
    stage           TEXT NOT NULL,             -- deposit | balance
    receipt_no      TEXT NOT NULL UNIQUE,      -- 网关回执号，唯一去重
    amount          INTEGER NOT NULL,          -- 回执金额，分
    paid            INTEGER NOT NULL DEFAULT 0,-- 已入账金额（重复回执不重复入账）
    status          TEXT NOT NULL,             -- recorded 已记录 | applied 已入账 | duplicate 重复 | exception 异常款
    note            TEXT NOT NULL DEFAULT '',
    raw_arrived_at  TEXT NOT NULL,             -- 回执到达时间（可能延迟）
    applied_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_order ON payment(order_id);

-- 异常款：财务审核队列。客服只能创建处理请求，财务可审核
CREATE TABLE IF NOT EXISTS exception_case (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES "order"(id),
    payment_id      INTEGER REFERENCES payment(id),
    kind            TEXT NOT NULL,             -- late_after_release 释放后迟到款 | overpay 多付 |
                                                -- cancelled_activity 活动取消后付款 | other
    amount          INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open', -- open | approved_refund | refunded | rejected | shipped
    note            TEXT NOT NULL DEFAULT '',
    raised_by       TEXT NOT NULL DEFAULT 'customer_service', -- customer_service | system
    reviewed_by     TEXT,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_exception_status ON exception_case(status);

-- 订单时间线（人工/系统动作留痕）
CREATE TABLE IF NOT EXISTS timeline_event (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES "order"(id),
    at              TEXT NOT NULL,
    actor           TEXT NOT NULL,             -- system | customer | finance | customer_service
    kind            TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_timeline_order ON timeline_event(order_id, id);

-- 模拟支付网关的延迟回执队列
CREATE TABLE IF NOT EXISTS payment_outbox (
    id              INTEGER PRIMARY KEY,
    order_code      TEXT NOT NULL,
    stage           TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    receipt_no      TEXT NOT NULL,
    deliver_at      TEXT NOT NULL,             -- 计划投递时间（模拟回执延迟）
    delivered       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- 客服处理请求（只能发起，由财务/系统执行）
CREATE TABLE IF NOT EXISTS service_request (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES "order"(id),
    type            TEXT NOT NULL,             -- cancel | refund | investigate
    reason          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | done | rejected
    created_by      TEXT NOT NULL DEFAULT 'customer_service',
    created_at      TEXT NOT NULL,
    handled_at      TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
