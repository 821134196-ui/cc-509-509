"""领域服务：下单占名额、尾款支付、逾期释放、异常款、取消、配货。

关键不变量：
1. 名额占用 = 状态属于占名额集合（HOLDING_STATES）的订单数，使用 IMMEDIATE 事务
   原子检查+插入，杜绝超卖。
2. 支付与释放竞争：每笔支付/释放都在同一个 IMMEDIATE 事务内对订单行加保留锁，
   以状态判定归属。已释放订单的迟到款绝不入账到订单，而是进异常款队列；
   已付清订单不会再被释放。因此不会出现“既退款又发货”。
3. 定金规则在下单时快照（版本号+金额+时间窗口），规则之后改版不影响老订单。
4. 回执以 receipt_no UNIQUE 去重；重复回执标记 duplicate，绝不重复入账。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from . import clock

# ---- 订单状态 ----
AWAITING_DEPOSIT = "awaiting_deposit"
DEPOSIT_PAID = "deposit_paid"
AWAITING_BALANCE = "awaiting_balance"
BALANCE_PARTIAL = "balance_partial"
PAID = "paid"
ALLOCATED = "allocated"
SHIPPED = "shipped"
RELEASED = "released"
REFUNDING = "refunding"
REFUNDED = "refunded"
CANCELLED = "cancelled"

# 仍占用名额的状态
HOLDING_STATES = (
    AWAITING_DEPOSIT, DEPOSIT_PAID, AWAITING_BALANCE,
    BALANCE_PARTIAL, PAID, ALLOCATED, SHIPPED,
    REFUNDING,
)
# 已付清、可以进入配货的状态
PAID_STATES = (PAID, ALLOCATED, SHIPPED)
# 终结态
TERMINAL_STATES = (SHIPPED, RELEASED, REFUNDED, CANCELLED)


class ServiceError(Exception):
    """业务规则错误。"""


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _event(conn: sqlite3.Connection, order_id: int, actor: str, kind: str,
           detail: str = "", at: datetime | None = None) -> None:
    conn.execute(
        'INSERT INTO timeline_event (order_id, at, actor, kind, detail) VALUES (?,?,?,?,?)',
        (order_id, clock.fmt(at or clock.now()), actor, kind, detail),
    )


def get_active_rule(conn: sqlite3.Connection, activity_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM deposit_rule WHERE activity_id=? ORDER BY version DESC LIMIT 1",
        (activity_id,),
    ).fetchone()
    if not row:
        raise ServiceError("活动没有定金规则")
    return row


def create_order(conn: sqlite3.Connection, activity_id: int, spec: str,
                 at: datetime | None = None) -> sqlite3.Row:
    """下单：原子占用预售名额，并锁定定金规则版本。"""
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        act = conn.execute(
            "SELECT * FROM activity WHERE id=?", (activity_id,)
        ).fetchone()
        if not act:
            raise ServiceError("活动不存在")
        if act["status"] != "on_sale":
            raise ServiceError("活动已取消，不能下单")

        rule = get_active_rule(conn, activity_id)
        if not (clock.parse(rule["deposit_open"]) <= at <= clock.parse(rule["deposit_close"])):
            raise ServiceError("不在定金支付时段内")

        sku = conn.execute(
            "SELECT * FROM sku WHERE activity_id=? AND spec=?", (activity_id, spec)
        ).fetchone()
        if not sku:
            raise ServiceError("商品规格不存在")

        held = conn.execute(
            f'SELECT COUNT(*) c FROM "order" WHERE activity_id=? AND status IN ({",".join("?"*len(HOLDING_STATES))})',
            (activity_id, *HOLDING_STATES),
        ).fetchone()["c"]
        if held >= act["total_quota"]:
            raise ServiceError("预售名额已满")

        deposit = rule["deposit_amount"]
        balance = rule["balance_amount"] + sku["extra_cents"]
        total = deposit + balance
        code = _code("PS")
        release_deadline = clock.add(
            clock.parse(rule["balance_close"]), rule["release_grace_sec"]
        )
        cur = conn.execute(
            """INSERT INTO "order" (code, activity_id, sku_id, rule_version,
                 deposit_amount, balance_amount, total_amount,
                 deposit_open, deposit_close, balance_open, balance_close,
                 release_deadline, release_grace_sec, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, activity_id, sku["id"], rule["version"], deposit, balance, total,
             rule["deposit_open"], rule["deposit_close"], rule["balance_open"],
             rule["balance_close"], clock.fmt(release_deadline),
             rule["release_grace_sec"], AWAITING_DEPOSIT, clock.fmt(at)),
        )
        oid = cur.lastrowid
        _event(conn, oid, "customer", "order_created",
               f"下单占用名额，锁定规则 v{rule['version']}", at)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.execute('SELECT * FROM "order" WHERE id=?', (oid,)).fetchone()


def _record_receipt(conn: sqlite3.Connection, order_id: int, stage: str,
                    receipt_no: str, amount: int, at: datetime) -> int:
    """登记回执：receipt_no 唯一 => 重复回执返回既有 payment id（调用方判断）。"""
    existing = conn.execute(
        "SELECT * FROM payment WHERE receipt_no=?", (receipt_no,)
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO payment (order_id, stage, receipt_no, amount, status,
                                raw_arrived_at)
           VALUES (?,?,?,?, 'recorded', ?)""",
        (order_id, stage, receipt_no, amount, clock.fmt(at)),
    )
    return cur.lastrowid


def apply_payment(conn: sqlite3.Connection, order_code: str, stage: str,
                  amount: int, receipt_no: str, at: datetime | None = None) -> dict:
    """处理一笔支付回执（可能延迟到达）。返回处置说明。

    处置：
    - applied：正常入账（可能含部分支付）
    - duplicate：重复回执，幂等忽略
    - exception：异常款（释放后迟到 / 活动取消后到账 / 金额异常），转财务队列
    """
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = conn.execute(
            'SELECT * FROM "order" WHERE code=?', (order_code,)
        ).fetchone()
        if not order:
            raise ServiceError("订单不存在")
        activity = conn.execute(
            "SELECT * FROM activity WHERE id=?", (order["activity_id"],)
        ).fetchone()

        # 1) 回执去重（跨订单也不允许同号）
        existing = conn.execute(
            "SELECT * FROM payment WHERE receipt_no=?", (receipt_no,)
        ).fetchone()
        if existing:
            # 原流水保持 applied（绝不能因重复回执把首笔入账冲掉），仅追加一次备注
            if "重复回执已忽略" not in (existing["note"] or ""):
                conn.execute(
                    "UPDATE payment SET note=COALESCE(note,'')||? WHERE id=?",
                    (f" | 重复回执于 {clock.fmt(at)} 已忽略", existing["id"]),
                )
            _event(conn, order["id"], "system", "payment_duplicate",
                   f"重复回执 {receipt_no}，金额 {amount} 分，已忽略", at)
            conn.execute("COMMIT")
            return {"result": "duplicate", "payment_id": existing["id"]}

        pid = _record_receipt(conn, order["id"], stage, receipt_no, amount, at)

        # 2) 终结态 / 已释放 / 已取消活动 => 异常款，不发货
        st = order["status"]
        abnormal_kind = None
        if st == RELEASED:
            abnormal_kind = "late_after_release"
        if activity["status"] == "cancelled" and st not in (RELEASED, PAID,
                                                             ALLOCATED, SHIPPED,
                                                             REFUNDED):
            # 活动取消后到账（订单可能已被置为 cancelled）
            abnormal_kind = "cancelled_activity"
        if st in (REFUNDED, CANCELLED, SHIPPED, ALLOCATED, PAID):
            if st in (PAID, ALLOCATED, SHIPPED):
                abnormal_kind = abnormal_kind or "overpay"
            else:
                abnormal_kind = abnormal_kind or "other"

        if abnormal_kind:
            conn.execute(
                "UPDATE payment SET status='exception', note=? WHERE id=?",
                (f"订单状态 {st}，不入账，转异常款", pid),
            )
            conn.execute(
                """INSERT INTO exception_case (order_id, payment_id, kind, amount,
                     status, note, raised_by, created_at)
                   VALUES (?,?,?,?,'open',?, 'system', ?)""",
                (order["id"], pid, abnormal_kind, amount,
                 f"回执 {receipt_no} 在订单状态 {st} 时到达", clock.fmt(at)),
            )
            _event(conn, order["id"], "system", "payment_exception",
                   f"异常款 {abnormal_kind}，金额 {amount} 分，待财务审核", at)
            conn.execute("COMMIT")
            return {"result": "exception", "kind": abnormal_kind, "payment_id": pid}

        # 3) 正常入账
        if stage == "deposit":
            if st != AWAITING_DEPOSIT:
                raise ServiceError(f"当前状态 {st} 不能收定金")
            if amount != order["deposit_amount"]:
                # 定金不接受部分/多付：拒绝并回滚登记，买家可按正确金额重新发起
                raise ServiceError(
                    f"定金金额必须恰为 {order['deposit_amount']} 分（收到 {amount} 分），请重新发起")
            conn.execute(
                """UPDATE "order" SET status=?, deposit_paid_at=? WHERE id=?""",
                (DEPOSIT_PAID, clock.fmt(at), order["id"]),
            )
            conn.execute("UPDATE payment SET status='applied', paid=?, applied_at=? WHERE id=?",
                         (amount, clock.fmt(at), pid))
            _event(conn, order["id"], "customer", "deposit_paid", "定金付清", at)
            conn.execute("COMMIT")
            return {"result": "applied", "status": DEPOSIT_PAID, "payment_id": pid}

        if stage != "balance":
            raise ServiceError("未知支付阶段")

        # 尾款：必须已付定金且在尾款窗口之后；窗口开始前付尾款属于提前支付
        if at < clock.parse(order["balance_open"]):
            conn.execute(
                "UPDATE payment SET status='exception', note='尾款窗口未开始，暂挂异常款' WHERE id=?",
                (pid,),
            )
            conn.execute(
                """INSERT INTO exception_case (order_id, payment_id, kind, amount,
                     status, note, raised_by, created_at)
                   VALUES (?,?,?,?, 'open', ?, 'system', ?)""",
                (order["id"], pid, "other", amount, "尾款窗口开始前提前付款", clock.fmt(at)),
            )
            conn.execute("COMMIT")
            return {"result": "exception", "kind": "other", "payment_id": pid}

        if st not in (DEPOSIT_PAID, AWAITING_BALANCE, BALANCE_PARTIAL):
            raise ServiceError(f"当前状态 {st} 不能支付尾款")

        already = conn.execute(
            "SELECT COALESCE(SUM(paid),0) s FROM payment WHERE order_id=? AND stage='balance' AND status='applied'",
            (order["id"],),
        ).fetchone()["s"]
        due_total = order["balance_amount"]
        new_paid = already + amount
        conn.execute("UPDATE payment SET status='applied', paid=?, applied_at=? WHERE id=?",
                     (amount, clock.fmt(at), pid))

        if new_paid < due_total:
            conn.execute(
                'UPDATE "order" SET status=?, balance_open=balance_open WHERE id=?',
                (BALANCE_PARTIAL, order["id"]),
            )
            _event(conn, order["id"], "customer", "balance_partial",
                   f"部分支付 {amount} 分，已付 {new_paid}/{due_total}，"
                   f"剩余 {due_total - new_paid} 分", at)
            conn.execute("COMMIT")
            return {"result": "applied", "status": BALANCE_PARTIAL,
                    "paid": new_paid, "due": due_total, "payment_id": pid}

        note = "尾款付清"
        if new_paid > due_total:
            over = new_paid - due_total
            note += f"，多付 {over} 分，挂异常款待财务退款（订单正常配货）"
            conn.execute(
                """INSERT INTO exception_case (order_id, payment_id, kind, amount,
                     status, note, raised_by, created_at)
                   VALUES (?,?,'overpay',?,'open',?, 'system', ?)""",
                (order["id"], pid, over,
                 f"尾款多付 {over} 分（订单已付清，不影响发货）", clock.fmt(at)),
            )
        conn.execute(
            'UPDATE "order" SET status=?, paid_off_at=? WHERE id=?',
            (PAID, clock.fmt(at), order["id"]),
        )
        _event(conn, order["id"], "customer", "balance_paid", note, at)
        conn.execute("COMMIT")
        return {"result": "applied", "status": PAID,
                "paid": new_paid, "due": due_total, "payment_id": pid}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release_overdue(conn: sqlite3.Connection, at: datetime | None = None) -> int:
    """扫描并释放逾期未付清尾款的名额。返回释放订单数。

    只释放：已付定金且尾款窗口已过（含宽限）且未付清的订单。
    与支付在同一行级竞争模型下：付款成功的订单状态变为 paid，不会被释放。
    """
    at = at or clock.now()
    released = 0
    rows = conn.execute(
        """SELECT id FROM "order"
           WHERE status IN (?, ?, ?) AND release_deadline <= ?""",
        (DEPOSIT_PAID, AWAITING_BALANCE, BALANCE_PARTIAL, clock.fmt(at)),
    ).fetchall()
    for r in rows:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 重新读锁定行；若已被支付事务改成 paid，跳过
            o = conn.execute('SELECT * FROM "order" WHERE id=?', (r["id"],)).fetchone()
            if o["status"] not in (DEPOSIT_PAID, AWAITING_BALANCE, BALANCE_PARTIAL):
                conn.execute("ROLLBACK")
                continue
            if clock.parse(o["release_deadline"]) >= at:
                conn.execute("ROLLBACK")
                continue
            # 部分支付过的订单释放时，已付尾款进入异常款队列（等待退款审核），不自动退
            partial = conn.execute(
                "SELECT COALESCE(SUM(paid),0) s FROM payment WHERE order_id=? AND stage='balance' AND status='applied'",
                (r["id"],),
            ).fetchone()["s"]
            conn.execute(
                'UPDATE "order" SET status=?, released_at=? WHERE id=?',
                (RELEASED, clock.fmt(at), r["id"]),
            )
            detail = "逾期未付尾款，名额释放"
            if partial > 0:
                detail += f"；已部分支付 {partial} 分，转异常款待退款审核"
                conn.execute(
                    """INSERT INTO exception_case (order_id, payment_id, kind, amount,
                         status, note, raised_by, created_at)
                       VALUES (?, NULL, 'overpay', ?, 'open', ?, 'system', ?)""",
                    (r["id"], partial, "释放时存在已入账部分尾款，需退款", clock.fmt(at)),
                )
            _event(conn, r["id"], "system", "released", detail, at)
            conn.execute("COMMIT")
            released += 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return released


def enter_balance_window(conn: sqlite3.Connection, at: datetime | None = None) -> int:
    """尾款期开始：deposit_paid -> awaiting_balance（页面据此显示应付与截止）。"""
    at = at or clock.now()
    n = conn.execute(
        """UPDATE "order" SET status=?
           WHERE status=? AND balance_open <= ?""",
        (AWAITING_BALANCE, DEPOSIT_PAID, clock.fmt(at)),
    ).rowcount
    return n


def cancel_activity(conn: sqlite3.Connection, activity_id: int,
                    at: datetime | None = None) -> int:
    """取消预售活动：活动关闭；未付清订单标记取消并释放名额；
    已付定金/部分尾款进入异常款队列由财务退款。返回受影响订单数。"""
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        act = conn.execute("SELECT * FROM activity WHERE id=?", (activity_id,)).fetchone()
        if not act:
            raise ServiceError("活动不存在")
        if act["status"] == "cancelled":
            conn.execute("ROLLBACK")
            return 0
        conn.execute(
            "UPDATE activity SET status='cancelled', cancelled_at=? WHERE id=?",
            (clock.fmt(at), activity_id),
        )
        rows = conn.execute(
            """SELECT * FROM "order" WHERE activity_id=?
                 AND status IN (?,?,?,?,?)""",
            (activity_id, AWAITING_DEPOSIT, DEPOSIT_PAID,
             AWAITING_BALANCE, BALANCE_PARTIAL, PAID),
        ).fetchall()
        # 已付清(paid)的订单不取消：转为正常配货（合同已成立），仅停止活动销售
        for o in rows:
            if o["status"] == PAID:
                continue
            paid_dep = o["deposit_amount"] if o["status"] in (
                DEPOSIT_PAID, AWAITING_BALANCE, BALANCE_PARTIAL) else 0
            partial = conn.execute(
                "SELECT COALESCE(SUM(paid),0) s FROM payment WHERE order_id=? AND status='applied'",
                (o["id"],),
            ).fetchone()["s"]
            conn.execute(
                'UPDATE "order" SET status=?, cancelled_at=? WHERE id=?',
                (CANCELLED, clock.fmt(at), o["id"]),
            )
            if partial > 0:
                conn.execute(
                    """INSERT INTO exception_case (order_id, payment_id, kind, amount,
                         status, note, raised_by, created_at)
                       VALUES (?, NULL, 'cancelled_activity', ?, 'open', ?, 'system', ?)""",
                    (o["id"], partial, "活动取消，已付款项待退款", clock.fmt(at)),
                )
            _event(conn, o["id"], "system", "order_cancelled",
                   f"活动取消，订单取消；已付 {partial} 分待退", at)
        conn.execute("COMMIT")
        return len(rows)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def change_specs(conn: sqlite3.Connection, activity_id: int,
                 specs: list[tuple[str, int]], at: datetime | None = None) -> None:
    """商品规格变更：只影响之后的新订单（老订单已快照 sku 与金额）。

    用“停用旧规格 + 启用新规格”实现：被改名/删除的规格不再可选，
    已有订单引用的 sku 行保留不动。
    """
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = {r["spec"]: r for r in conn.execute(
            "SELECT * FROM sku WHERE activity_id=?", (activity_id,)).fetchall()}
        wanted = {s[0] for s in specs}
        for spec, extra in specs:
            if spec in existing:
                conn.execute("UPDATE sku SET extra_cents=? WHERE id=?",
                             (extra, existing[spec]["id"]))
            else:
                conn.execute(
                    "INSERT INTO sku (activity_id, spec, extra_cents) VALUES (?,?,?)",
                    (activity_id, spec, extra),
                )
        for spec, row in existing.items():
            if spec not in wanted:
                # 软删除：改名为 ~已下架，历史订单外键仍可用
                conn.execute(
                    "UPDATE sku SET spec=? WHERE id=?",
                    (f"[已下架] {spec}", row["id"]),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 配货 / 批次 ----------

def allocate(conn: sqlite3.Connection, activity_id: int, batch_id: int,
             limit: int | None = None, at: datetime | None = None) -> list[int]:
    """把已付清订单按先付清先配货原则分配到批次（发货批次管理）。"""
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute(
            "SELECT * FROM shipment_batch WHERE id=? AND activity_id=?",
            (batch_id, activity_id),
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在")
        used = conn.execute(
            'SELECT COUNT(*) c FROM "order" WHERE batch_id=?', (batch_id,)
        ).fetchone()["c"]
        cap = batch["capacity"]
        room = None if cap is None else max(0, cap - used)
        if room == 0:
            conn.execute("ROLLBACK")
            return []
        q = ('SELECT id FROM "order" WHERE activity_id=? AND status=? '
             'ORDER BY paid_off_at ASC, id ASC')
        params: list = [activity_id, PAID]
        if limit is not None:
            room = min(room, limit) if room is not None else limit
        if room is not None:
            q += " LIMIT ?"
            params.append(room)
        rows = conn.execute(q, params).fetchall()
        ids = [r["id"] for r in rows]
        for oid in ids:
            conn.execute(
                'UPDATE "order" SET status=?, batch_id=?, allocated_at=? WHERE id=?',
                (ALLOCATED, batch_id, clock.fmt(at), oid),
            )
            _event(conn, oid, "system", "allocated",
                   f"已配货到批次 {batch['label']}", at)
        conn.execute("COMMIT")
        return ids
    except Exception:
        conn.execute("ROLLBACK")
        raise


def ship_batch(conn: sqlite3.Connection, batch_id: int,
               at: datetime | None = None) -> int:
    """批次发货：只有到达 ship_after 才发，防止提前发货。"""
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute("SELECT * FROM shipment_batch WHERE id=?",
                             (batch_id,)).fetchone()
        if not batch:
            raise ServiceError("批次不存在")
        if at < clock.parse(batch["ship_after"]):
            raise ServiceError("未到该批次发货时间")
        rows = conn.execute(
            'SELECT id FROM "order" WHERE batch_id=? AND status=?',
            (batch_id, ALLOCATED),
        ).fetchall()
        for r in rows:
            conn.execute('UPDATE "order" SET status=?, shipped_at=? WHERE id=?',
                         (SHIPPED, clock.fmt(at), r["id"]))
            _event(conn, r["id"], "system", "shipped", "批次发货", at)
        conn.execute("COMMIT")
        return len(rows)
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 异常款审核（财务） / 客服请求 ----------

def review_exception(conn: sqlite3.Connection, case_id: int, action: str,
                     reviewer: str = "finance", note: str = "",
                     at: datetime | None = None) -> None:
    """财务审核异常款：approve_refund 批准退款并执行 / reject 驳回 / force_ship 强制发货（仅限活动有效且名额允许）。"""
    at = at or clock.now()
    assert reviewer == "finance", "只有财务可以审核异常款"
    conn.execute("BEGIN IMMEDIATE")
    try:
        case = conn.execute("SELECT * FROM exception_case WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise ServiceError("异常款不存在")
        if case["status"] != "open":
            raise ServiceError("该异常款已审核")
        order = conn.execute('SELECT * FROM "order" WHERE id=?',
                             (case["order_id"],)).fetchone()

        if action == "reject":
            conn.execute(
                "UPDATE exception_case SET status='rejected', reviewed_by=?, reviewed_at=?, note=COALESCE(note,'')||? WHERE id=?",
                (reviewer, clock.fmt(at), f" | 驳回：{note}", case_id),
            )
            _event(conn, order["id"], "finance", "exception_rejected", note, at)
        elif action == "approve_refund":
            # 已释放订单直接退款终结；占名额中的订单也终结为已退款（名额同时释放）
            conn.execute(
                'UPDATE "order" SET status=?, released_at=COALESCE(released_at,?) WHERE id=?',
                (REFUNDED, clock.fmt(at), order["id"]),
            )
            conn.execute(
                "UPDATE exception_case SET status='refunded', reviewed_by=?, reviewed_at=?, note=COALESCE(note,'')||? WHERE id=?",
                (reviewer, clock.fmt(at), f" | 已退款：{note}", case_id),
            )
            _event(conn, order["id"], "finance", "refunded",
                   f"异常款 {case['amount']} 分已退款", at)
        elif action == "force_ship":
            if order["status"] != RELEASED:
                raise ServiceError("只能对已释放订单决定是否强制发货")
            act = conn.execute("SELECT * FROM activity WHERE id=?",
                               (order["activity_id"],)).fetchone()
            if act["status"] != "on_sale":
                raise ServiceError("活动已取消，不能强制发货")
            # 重新占用名额需要检查容量
            held = conn.execute(
                f'SELECT COUNT(*) c FROM "order" WHERE activity_id=? AND status IN ({",".join("?"*len(HOLDING_STATES))})',
                (order["activity_id"], *HOLDING_STATES),
            ).fetchone()["c"]
            if held >= act["total_quota"]:
                raise ServiceError("名额不足，无法强制发货")
            conn.execute(
                'UPDATE "order" SET status=?, released_at=NULL WHERE id=?',
                (PAID, order["id"]),
            )
            conn.execute(
                "UPDATE exception_case SET status='shipped', reviewed_by=?, reviewed_at=?, note=COALESCE(note,'')||? WHERE id=?",
                (reviewer, clock.fmt(at), " | 财务核准恢复订单，重新占名额待配货", case_id),
            )
            _event(conn, order["id"], "finance", "exception_force_paid",
                   "财务核准迟到尾款，恢复为已付清待配货", at)
        else:
            raise ServiceError("未知审核动作")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def add_rule_version(conn: sqlite3.Connection, activity_id: int,
                     deposit_amount: int, balance_amount: int,
                     deposit_open: str, deposit_close: str,
                     balance_open: str, balance_close: str,
                     release_grace_sec: int = 0,
                     at: datetime | None = None) -> int:
    """发布新定金规则版本。只影响之后下单的订单，老订单保持快照。"""
    at = at or clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        act = conn.execute("SELECT * FROM activity WHERE id=?", (activity_id,)).fetchone()
        if not act:
            raise ServiceError("活动不存在")
        ver = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 v FROM deposit_rule WHERE activity_id=?",
            (activity_id,),
        ).fetchone()["v"]
        conn.execute(
            """INSERT INTO deposit_rule (activity_id,version,deposit_amount,balance_amount,
                 deposit_open,deposit_close,balance_open,balance_close,
                 release_grace_sec,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (activity_id, ver, deposit_amount, balance_amount,
             deposit_open, deposit_close, balance_open, balance_close,
             release_grace_sec, clock.fmt(at)),
        )
        conn.execute("COMMIT")
        return ver
    except Exception:
        conn.execute("ROLLBACK")
        raise


def create_service_request(conn: sqlite3.Connection, order_id: int, rtype: str,
                           reason: str, at: datetime | None = None) -> int:
    """客服只能发起处理请求，不能直接改账。"""
    at = at or clock.now()
    cur = conn.execute(
        """INSERT INTO service_request (order_id, type, reason, status, created_by, created_at)
           VALUES (?,?,?, 'pending', 'customer_service', ?)""",
        (order_id, rtype, reason, clock.fmt(at)),
    )
    _event(conn, order_id, "customer_service", "service_request",
           f"客服发起请求：{rtype}（{reason}）", at)
    return cur.lastrowid
