"""SQLite 连接、建表与虚拟时钟。

仅使用标准库。所有写操作在调用方事务中进行；通过 IMMEDIATE 事务 +
库存三计数行锁保证并发下不超卖。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

# 时钟倍率：真实 1 秒 = 虚拟 10 秒。种子把截止时刻设在几分钟后，
# 启动后约 1 分钟内可观察到开窗、到账、截止、释放全过程，也可手动拨钟。
TICK_MULTIPLIER = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  -- 虚拟时钟基准：vtime = base_v + (now_real - base_real) * TICK_MULTIPLIER
  base_v TEXT NOT NULL,
  base_real REAL NOT NULL
);

-- 定金规则版本：下单时锁定版本号，后续改价/改规格不影响已下单订单
CREATE TABLE IF NOT EXISTS rule_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  activity_id INTEGER NOT NULL REFERENCES activities(id),
  version INTEGER NOT NULL,
  deposit_cents INTEGER NOT NULL CHECK (deposit_cents >= 0),
  balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
  spec TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(activity_id, version)
);

CREATE TABLE IF NOT EXISTS activities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  color TEXT NOT NULL,            -- 商品色（页面点缀，白底黑字）
  image TEXT NOT NULL,            -- static/images 下的本地文件名
  stock INTEGER NOT NULL CHECK (stock >= 0),
  reserved INTEGER NOT NULL DEFAULT 0,  -- 占名额（含仅付定金）
  paid INTEGER NOT NULL DEFAULT 0,      -- 尾款已付清、进入配货
  released INTEGER NOT NULL DEFAULT 0,  -- 已释放名额
  deposit_open_at TEXT NOT NULL,
  deposit_close_at TEXT NOT NULL,
  balance_open_at TEXT NOT NULL,
  balance_close_at TEXT NOT NULL,
  -- 当前规则版本（老订单仍锁历史版本）
  current_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
  cancel_note TEXT NOT NULL DEFAULT '',
  CHECK (reserved + paid + released <= stock)
);

CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL UNIQUE,
  activity_id INTEGER NOT NULL REFERENCES activities(id),
  qty INTEGER NOT NULL CHECK (qty > 0),
  -- 下单时锁定的规则版本
  rule_version INTEGER NOT NULL,
  deposit_cents INTEGER NOT NULL,
  balance_cents INTEGER NOT NULL,
  spec_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'reserved',      -- 已占名额（演示中视为定金已付）
    'partial',       -- 尾款部分支付
    'paid',          -- 尾款付清 -> 配货
    'fulfilled',     -- 已发货
    'released',      -- 逾期/取消，名额已释放
    'cancelled'      -- 预售取消（保留订单，区别于逾期释放）
  )),
  paid_deposit_cents INTEGER NOT NULL DEFAULT 0,
  paid_balance_cents INTEGER NOT NULL DEFAULT 0,
  -- 财务在异常款上决定的去向
  disposition TEXT NOT NULL DEFAULT '' CHECK (disposition IN (
    '','ship','refund','partial_refund','reject')),
  disposition_note TEXT NOT NULL DEFAULT '',
  released_at TEXT,
  cancel_refunded_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  at TEXT NOT NULL,
  kind TEXT NOT NULL,            -- created/deposit_paid/balance_partial/...
  detail TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL DEFAULT 0
);

-- 模拟支付回执（适配器回执登记 + 人工补单都走这张表，做幂等）
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  phase TEXT NOT NULL CHECK (phase IN ('deposit','balance')),
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  receipt_no TEXT NOT NULL UNIQUE,          -- 回执号：唯一约束防重复入账
  paid_at TEXT NOT NULL,                    -- 支付渠道声称的付款时间
  arrived_at TEXT NOT NULL,                 -- 回执到达系统时间（可能延迟）
  duplicate_of INTEGER REFERENCES payments(id),
  applied_to_order INTEGER NOT NULL DEFAULT 0,  -- 是否实际入账
  kind TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal','duplicate','late')),
  note TEXT NOT NULL DEFAULT ''
);

-- 模拟支付渠道回执：可能延迟到达，到达后才投递；delivery 幂等
CREATE TABLE IF NOT EXISTS pending_receipts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  phase TEXT NOT NULL CHECK (phase IN ('deposit','balance')),
  amount_cents INTEGER NOT NULL,
  receipt_no TEXT NOT NULL,
  paid_at TEXT NOT NULL,      -- 渠道声称的付款时刻
  arrive_at TEXT NOT NULL,    -- 预计到达系统的时刻（模拟延迟）
  delivered INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT ''
);

-- 异常款队列
CREATE TABLE IF NOT EXISTS exceptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  type TEXT NOT NULL CHECK (type IN (
    'late_payment',      -- 释放后迟到的尾款
    'overpayment',       -- 重复/超额入账的钱
    'overpaid_balance',  -- 单笔/多笔合计超过尾款
    'partial_timeout',   -- 部分支付但仍逾期
    'cancel_payment'     -- 取消预售之后到达的付款
  )),
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  payment_id INTEGER REFERENCES payments(id),
  amount_cents INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT ''
);

-- 客服处理请求 -> 财务/运营审核
CREATE TABLE IF NOT EXISTS service_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  opened_by TEXT NOT NULL,           -- 客服
  at TEXT NOT NULL,
  request_type TEXT NOT NULL CHECK (request_type IN (
    'release_early','cancel_activity','spec_change_confirm','manual_review')),
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  reviewed_by TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT,
  review_note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_orders_activity ON orders(activity_id);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id, id);
CREATE INDEX IF NOT EXISTS idx_exceptions_status ON exceptions(status);
"""


class Clock:
    """全局虚拟时钟，加锁保护；时间一律 UTC ISO 字符串存储。"""

    def __init__(self, conn: sqlite3.Connection):
        self._lock = threading.Lock()
        self.conn = conn
        row = conn.execute("SELECT base_v, base_real FROM app_state WHERE id=1").fetchone()
        now_real = time.time()
        if row is None:
            now = datetime.fromtimestamp(now_real, tz=timezone.utc).replace(microsecond=0)
            conn.execute(
                "INSERT INTO app_state(id, base_v, base_real) VALUES(1,?,?)",
                (now.isoformat(), now_real),
            )
            conn.commit()
            self.base_v = now
            self.base_real = now_real
        else:
            self.base_v = datetime.fromisoformat(row[0])
            self.base_real = row[1]

    def _now_locked(self) -> datetime:
        delta = (time.time() - self.base_real) * TICK_MULTIPLIER
        return self.base_v + timedelta(seconds=delta)

    def now(self) -> datetime:
        with self._lock:
            return self._now_locked()

    def now_iso(self) -> str:
        return self.now().isoformat()

    def advance(self, seconds: int) -> datetime:
        """把虚拟时钟向前拨（分钟级演示用）。"""
        with self._lock:
            cur = self._now_locked()
            self.base_v = cur + timedelta(seconds=seconds)
            self.base_real = time.time()
            self.conn.execute(
                "UPDATE app_state SET base_v=?, base_real=? WHERE id=1",
                (self.base_v.isoformat(), self.base_real),
            )
            self.conn.commit()
            return self.base_v

    def reset_to(self, dt: datetime) -> datetime:
        with self._lock:
            self.base_v = dt
            self.base_real = time.time()
            self.conn.execute(
                "UPDATE app_state SET base_v=?, base_real=? WHERE id=1",
                (self.base_v.isoformat(), self.base_real),
            )
            self.conn.commit()
            return self.base_v


def connect(path: str = "presale.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn
