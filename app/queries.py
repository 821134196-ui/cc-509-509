"""只读查询：库存看板、订单详情、时间线、异常款。"""
from __future__ import annotations

import sqlite3

from .services import (AWAITING_DEPOSIT, AWAITING_BALANCE, ALLOCATED, BALANCE_PARTIAL,
                       CANCELLED, DEPOSIT_PAID, HOLDING_STATES, PAID, REFUNDED,
                       RELEASED, SHIPPED)

STATUS_CN = {
    AWAITING_DEPOSIT: "待付定金",
    DEPOSIT_PAID: "定金已付",
    AWAITING_BALANCE: "待付尾款",
    BALANCE_PARTIAL: "尾款部分支付",
    PAID: "已付清待配货",
    ALLOCATED: "已配货",
    SHIPPED: "已发货",
    RELEASED: "逾期释放",
    "refunding": "退款中",
    REFUNDED: "已退款",
    CANCELLED: "已取消",
}


def inventory(conn: sqlite3.Connection, activity_id: int) -> dict:
    """库存三值：预留(占名额未付清) / 已付(付清及以后) / 释放(含取消退款)。"""
    holding = conn.execute(
        f'SELECT status, COUNT(*) c FROM "order" WHERE activity_id=? AND status IN ({",".join("?"*len(HOLDING_STATES))}) GROUP BY status',
        (activity_id, *HOLDING_STATES),
    ).fetchall()
    by = {r["status"]: r["c"] for r in holding}
    reserved = sum(by.get(s, 0) for s in
                   (AWAITING_DEPOSIT, DEPOSIT_PAID, AWAITING_BALANCE, BALANCE_PARTIAL))
    paid = sum(by.get(s, 0) for s in (PAID, ALLOCATED, SHIPPED))
    refunding = by.get("refunding", 0)
    released_rows = conn.execute(
        """SELECT (SELECT COUNT(*) FROM "order" WHERE activity_id=? AND status=?) +
                  (SELECT COUNT(*) FROM "order" WHERE activity_id=? AND status=?) +
                  (SELECT COUNT(*) FROM "order" WHERE activity_id=? AND status=?) AS n""",
        (activity_id, RELEASED, activity_id, REFUNDED, activity_id, CANCELLED),
    ).fetchone()["n"]
    total = conn.execute(
        "SELECT total_quota FROM activity WHERE id=?", (activity_id,)
    ).fetchone()["total_quota"]
    held = reserved + paid + refunding
    return {
        "total": total,
        "reserved": reserved,
        "paid": paid,
        "refunding": refunding,
        "released": released_rows,
        "held": held,
        "available": total - held,
    }


def order_detail(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT o.*, a.name activity_name, a.color, a.status activity_status,
                  a.image, s.spec, d.deposit_amount rule_deposit, d.balance_amount rule_balance,
                  b.label batch_label
           FROM "order" o
           JOIN activity a ON a.id=o.activity_id
           JOIN sku s ON s.id=o.sku_id
           JOIN deposit_rule d ON d.activity_id=a.id AND d.version=o.rule_version
           LEFT JOIN shipment_batch b ON b.id=o.batch_id
           WHERE o.code=?""",
        (code,),
    ).fetchone()


def order_payments(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM payment WHERE order_id=? ORDER BY id", (order_id,)
    ).fetchall()


def balance_paid(conn: sqlite3.Connection, order_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(paid),0) s FROM payment WHERE order_id=? AND stage='balance' AND status='applied'",
        (order_id,),
    ).fetchone()["s"]


def deposit_paid_amount(conn: sqlite3.Connection, order_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(paid),0) s FROM payment WHERE order_id=? AND stage='deposit' AND status='applied'",
        (order_id,),
    ).fetchone()["s"]
