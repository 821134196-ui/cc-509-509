"""模拟支付适配器。

- charge(): 买家“付款”，网关生成回执号并入延迟队列（payment_outbox）。
- deliver_due(): 投递到期回执到领域服务 apply_payment；未到期的继续等待，
  这就模拟“支付回执延迟”。
- replay(): 手动重放某回执（模拟网关重复回调），apply_payment 幂等去重。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from . import clock, services


def _receipt() -> str:
    return "PAY" + uuid.uuid4().hex[:14].upper()


def charge(conn: sqlite3.Connection, order_code: str, stage: str, amount: int,
           delay_sec: float = 0, at: datetime | None = None,
           receipt_no: str | None = None) -> str:
    """买家发起一笔付款。delay_sec>0 表示回执延迟到达。返回回执号。"""
    at = at or clock.now()
    rn = receipt_no or _receipt()
    conn.execute(
        """INSERT INTO payment_outbox (order_code, stage, amount, receipt_no,
             deliver_at, delivered, created_at)
           VALUES (?,?,?,?,?,0,?)""",
        (order_code, stage, amount, rn,
         clock.fmt(clock.add(at, delay_sec)), clock.fmt(at)),
    )
    return rn


def deliver_due(conn: sqlite3.Connection, at: datetime | None = None) -> int:
    """投递所有到期回执。返回投递条数。"""
    at = at or clock.now()
    rows = conn.execute(
        "SELECT * FROM payment_outbox WHERE delivered=0 AND deliver_at<=? ORDER BY id",
        (clock.fmt(at),),
    ).fetchall()
    delivered = 0
    for r in rows:
        try:
            services.apply_payment(
                conn, r["order_code"], r["stage"], r["amount"], r["receipt_no"], at=at
            )
        except services.ServiceError as e:
            # 例如金额不符被业务拒绝：不再反复投递，错误留在网关侧日志中
            print(f"[deliver] receipt {r['receipt_no']} rejected: {e}")
        finally:
            conn.execute("UPDATE payment_outbox SET delivered=1 WHERE id=?", (r["id"],))
        delivered += 1
    return delivered


def replay(conn: sqlite3.Connection, receipt_no: str,
           at: datetime | None = None) -> dict:
    """模拟网关重复回调同一回执（outbox 或已直接入账的回执都可重放）。"""
    at = at or clock.now()
    row = conn.execute(
        "SELECT * FROM payment_outbox WHERE receipt_no=?", (receipt_no,)
    ).fetchone()
    if row:
        return services.apply_payment(
            conn, row["order_code"], row["stage"], row["amount"], receipt_no, at=at
        )
    pay = conn.execute("SELECT * FROM payment WHERE receipt_no=?", (receipt_no,)).fetchone()
    if not pay:
        raise services.ServiceError("回执不存在，无法重放")
    order = conn.execute('SELECT code FROM "order" WHERE id=?',
                         (pay["order_id"],)).fetchone()
    return services.apply_payment(
        conn, order["code"], pay["stage"], pay["amount"], receipt_no, at=at
    )
