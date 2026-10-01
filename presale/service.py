"""预售领域服务：名额占用、尾款状态机、回执幂等、逾期释放与异常款。

并发正确性：所有写事务以 BEGIN IMMEDIATE 开始，SQLite 同一时刻只允许一个
此类事务，因此“检查库存/状态 -> 改计数 -> 落订单”在全库范围原子。
支付回执处理与逾期释放走同一把写锁，二者不可能同时提交，
从机制上杜绝“既退款又发货”。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .db import Clock


class PresaleError(Exception):
    """业务规则拒绝（未产生资金变动）。"""


class PaymentRejected(PresaleError):
    pass


# 订单状态 -> 中文
STATUS_CN = {
    "reserved": "待付尾款",
    "partial": "尾款部分支付",
    "paid": "待配货",
    "fulfilled": "已发货",
    "released": "已释放（名额回收）",
    "cancelled": "预售取消",
}

EVENT_CN = {
    "created": "创建订单，锁定规则版本",
    "deposit_paid": "定金支付成功",
    "balance_paid": "尾款到账",
    "balance_partial": "尾款部分到账",
    "paid_ready": "尾款结清，进入配货",
    "fulfilled": "配货完成并发货",
    "released": "逾期未付尾款，名额释放",
    "activity_cancelled": "活动取消，订单终止",
    "refund": "发起退款（模拟原路退回）",
    "late_receipt": "释放/取消后迟到回执，入异常款队列",
    "duplicate_receipt": "收到重复回执，已忽略未重复入账",
    "overdue_receipt": "过截止时间后到账，未入账，入异常款队列",
    "overpaid_receipt": "金额超出应付，未入账，入异常款队列",
    "exception_opened": "登记异常款",
    "exception_resolved": "异常款审核完成",
    "replenished": "经财务批准占用回补名额恢复履约",
    "version_published": "发布新规则版本",
    "service_opened": "客服发起处理请求",
    "service_reviewed": "处理请求被审核",
    "spec_updated": "客户确认按新规格履约（价格仍按锁定版本）",
}


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


@dataclass
class Window:
    now: datetime

    def state(self, open_at: str, close_at: str) -> str:
        o, c = parse(open_at), parse(close_at)
        if self.now < o:
            return "before"
        if self.now <= c:
            return "open"
        return "after"


class PresaleService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    # ---------- 基础工具 ----------
    def now(self) -> datetime:
        return self.clock.now()

    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def _event(self, order_id: int, kind: str, detail: str = "", amount: int = 0):
        self.conn.execute(
            "INSERT INTO order_events(order_id,at,kind,detail,amount_cents) VALUES(?,?,?,?,?)",
            (order_id, self.now_iso, kind, detail, amount),
        )

    @property
    def now_iso(self) -> str:
        return self.clock.now_iso()

    def _activity(self, activity_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if row is None:
            raise PresaleError("活动不存在")
        return row

    def _order(self, order_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise PresaleError("订单不存在")
        return row

    # ---------- 查询 ----------
    def list_activities(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM activities ORDER BY id"))

    def get_activity(self, activity_id: int) -> sqlite3.Row:
        return self._activity(activity_id)

    def list_orders(self, activity_id: Optional[int] = None) -> list[sqlite3.Row]:
        if activity_id:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE activity_id=? ORDER BY id", (activity_id,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM orders ORDER BY id")
        return list(rows)

    def get_order(self, order_id: int) -> sqlite3.Row:
        return self._order(order_id)

    def timeline(self, order_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,)
            )
        )

    def payments_of(self, order_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM payments WHERE order_id=? ORDER BY id", (order_id,))
        )

    def list_exceptions(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT e.*, o.order_no, a.name activity_name "
            "FROM exceptions e JOIN orders o ON o.id=e.order_id "
            "JOIN activities a ON a.id=o.activity_id "
        )
        if status:
            sql += "WHERE e.status=? ORDER BY e.id"
            return list(self.conn.execute(sql, (status,)))
        return list(self.conn.execute(sql + "ORDER BY e.id DESC"))

    def list_requests(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT s.*, o.order_no, a.name activity_name FROM service_requests s "
            "JOIN orders o ON o.id=s.order_id JOIN activities a ON a.id=o.activity_id "
        )
        if status:
            return list(self.conn.execute(sql + "WHERE s.status=? ORDER BY s.id", (status,)))
        return list(self.conn.execute(sql + "ORDER BY s.id DESC"))

    def pending_receipts(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT p.*, o.order_no FROM pending_receipts p "
                "JOIN orders o ON o.id=p.order_id WHERE p.delivered=0 ORDER BY p.arrive_at"
            )
        )

    def rule_versions(self, activity_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM rule_versions WHERE activity_id=? ORDER BY version",
                (activity_id,),
            )
        )

    def available_qty(self, a: sqlite3.Row) -> int:
        """可售名额：释放回补的名额可再次出售，故 released 不占库存。"""
        return a["stock"] - a["reserved"] - a["paid"]

    # ---------- 下单（占名额 + 锁定规则版本） ----------
    def create_order(self, activity_id: int, qty: int = 1) -> int:
        try:
            self._tx()
            now = self.now_iso
            a = self._activity(activity_id)
            w = Window(self.now())
            if a["status"] != "active":
                raise PaymentRejected("预售活动已取消，无法下单")
            if w.state(a["deposit_open_at"], a["deposit_close_at"]) != "open":
                raise PaymentRejected("不在定金下单时段内")
            if self.available_qty(a) < qty:
                raise PaymentRejected("预售名额不足")
            rv = self.conn.execute(
                "SELECT * FROM rule_versions WHERE activity_id=? AND version=?",
                (activity_id, a["current_version"]),
            ).fetchone()
            cur = self.conn.execute(
                "SELECT COALESCE(MAX(id),0)+1 FROM orders"
            ).fetchone()[0]
            order_no = f"PS-{a['sku']}-{cur:04d}"
            cur = self.conn.execute(
                """INSERT INTO orders(order_no,activity_id,qty,rule_version,
                   deposit_cents,balance_cents,spec_snapshot,created_at,status,
                   paid_deposit_cents)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (order_no, activity_id, qty, rv["version"], rv["deposit_cents"],
                 rv["balance_cents"], rv["spec"], now, "reserved",
                 rv["deposit_cents"] * qty),
            )
            oid = cur.lastrowid
            # 优先占用回补名额（released 递减），其余占用原始名额
            take_released = min(qty, a["released"])
            self.conn.execute(
                "UPDATE activities SET reserved=reserved+?, "
                "released=released-? WHERE id=?",
                (qty, take_released, activity_id)
            )
            self._event(oid, "created",
                        f"下单 {qty} 件，锁定规则 v{rv['version']}：{rv['spec']}")
            # 演示中定金随单一并支付成功
            dep = rv["deposit_cents"] * qty
            self.conn.execute(
                """INSERT INTO payments(order_id,phase,amount_cents,receipt_no,
                   paid_at,arrived_at,applied_to_order,note)
                   VALUES(?,?,?,?,?,?,1,'定金随单支付')""",
                (oid, "deposit", dep, f"D-{oid:06d}", now, now),
            )
            self._event(oid, "deposit_paid", "模拟支付渠道回执", dep)
            self.conn.commit()
            return oid
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 规则版本（运营） ----------
    def publish_version(self, activity_id: int, deposit_cents: int, balance_cents: int,
                        spec: str, note: str) -> int:
        try:
            self._tx()
            a = self._activity(activity_id)
            nv = a["current_version"] + 1
            self.conn.execute(
                """INSERT INTO rule_versions(activity_id,version,deposit_cents,
                   balance_cents,spec,created_at,note) VALUES(?,?,?,?,?,?,?)""",
                (activity_id, nv, deposit_cents, balance_cents, spec, self.now_iso, note),
            )
            self.conn.execute(
                "UPDATE activities SET current_version=? WHERE id=?", (nv, activity_id)
            )
            self.conn.commit()
            return nv
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 支付回执（模拟适配器入口） ----------
    def queue_receipt(self, order_id: int, phase: str, amount_cents: int,
                      receipt_no: str, paid_at: str, arrive_at: str) -> int:
        """登记一个可能延迟到达的回执；到达时刻由投递器判定。"""
        self._tx()
        try:
            self._order(order_id)
            cur = self.conn.execute(
                """INSERT INTO pending_receipts(order_id,phase,amount_cents,receipt_no,
                   paid_at,arrive_at,delivered,result) VALUES(?,?,?,?,?,?,0,'')""",
                (order_id, phase, amount_cents, receipt_no, paid_at, arrive_at),
            )
            self.conn.commit()
            return cur.lastrowid
        except Exception:
            self.conn.rollback()
            raise

    def send_balance_receipt(self, order_id: int, amount_cents: int,
                             delay_seconds: int = 0) -> str:
        """模拟“用户付款 -> 渠道回执”。delay>0 时回执延迟到达。"""
        now = self.now()
        paid_at = now.isoformat()
        arrive_at = (now + timedelta(seconds=delay_seconds)).isoformat()
        n = self.conn.execute(
            "SELECT COALESCE(COUNT(*),0) FROM pending_receipts"
        ).fetchone()[0] + 1
        receipt_no = f"B-{order_id:06d}-{n:03d}"
        self.queue_receipt(order_id, "balance", amount_cents, receipt_no,
                           paid_at, arrive_at)
        return receipt_no

    def send_duplicate(self, pending_id: int) -> str:
        """模拟渠道把同一张尾款回执再推一次（相同 receipt_no）。"""
        try:
            self._tx()
            row = self.conn.execute(
                "SELECT * FROM pending_receipts WHERE id=?", (pending_id,)
            ).fetchone()
            if row is None:
                raise PresaleError("回执不存在")
            outcome = self._apply_receipt(row["order_id"], row["phase"],
                                          row["amount_cents"], row["receipt_no"],
                                          row["paid_at"], duplicate=True)
            # 标记原 pending 的重复推送结果（不改变其投递状态）
            self.conn.commit()
            return outcome
        except Exception:
            self.conn.rollback()
            raise

    def deliver_pending(self) -> list[str]:
        """投递所有到达时间已到的回执（后台线程与页面按钮共用，幂等）。

        每张回执独立事务：一张被规则拒绝不影响其他回执；失败的回执
        保持未投递，窗口打开后会自动重试成功。
        """
        results = []
        rows = self.conn.execute(
            "SELECT * FROM pending_receipts WHERE delivered=0 AND arrive_at<=? ORDER BY id",
            (self.now_iso,),
        ).fetchall()
        for r in rows:
            try:
                self._tx()
                outcome = self._apply_receipt(
                    r["order_id"], r["phase"], r["amount_cents"], r["receipt_no"],
                    r["paid_at"], duplicate=False,
                )
                self.conn.execute(
                    "UPDATE pending_receipts SET delivered=1,result=? WHERE id=?",
                    (outcome, r["id"]),
                )
                self.conn.commit()
                results.append(f"{r['receipt_no']}:{outcome}")
            except PaymentRejected:
                # 例如尾款窗未开始：留待下轮重试
                self.conn.rollback()
            except Exception:
                self.conn.rollback()
                raise
        return results

    def deliver_now(self, pending_id: int) -> str:
        self._tx()
        try:
            r = self.conn.execute(
                "SELECT * FROM pending_receipts WHERE id=?", (pending_id,)
            ).fetchone()
            if r is None:
                raise PresaleError("回执不存在")
            if r["delivered"]:
                self.conn.rollback()
                return r["result"]
            outcome = self._apply_receipt(
                r["order_id"], r["phase"], r["amount_cents"], r["receipt_no"],
                r["paid_at"], duplicate=False,
            )
            self.conn.execute(
                "UPDATE pending_receipts SET delivered=1,result=? WHERE id=?",
                (outcome, pending_id),
            )
            self.conn.commit()
            return outcome
        except Exception:
            self.conn.rollback()
            raise

    def _apply_receipt(self, order_id: int, phase: str, amount_cents: int,
                       receipt_no: str, paid_at: str, duplicate: bool) -> str:
        """调用方已持有 IMMEDIATE 写事务。返回结果码。"""
        now = self.now_iso
        # 1) 回执幂等：同 receipt_no 永不二次入账
        existed = self.conn.execute(
            "SELECT id FROM payments WHERE receipt_no=?", (receipt_no,)
        ).fetchone()
        if existed is not None or duplicate:
            self._event(order_id, "duplicate_receipt",
                        f"重复回执 {receipt_no}（金额 {amount_cents} 分）已忽略")
            return "duplicate_ignored"

        o = self._order(order_id)
        a = self._activity(o["activity_id"])

        def record(kind: str, applied: int, note: str) -> int:
            cur = self.conn.execute(
                """INSERT INTO payments(order_id,phase,amount_cents,receipt_no,
                   paid_at,arrived_at,applied_to_order,kind,note)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (order_id, phase, amount_cents, receipt_no, paid_at, now,
                 applied, kind, note),
            )
            return cur.lastrowid

        # 2) 订单已释放 / 活动已取消：迟到付款 -> 异常款，绝不动名额
        if o["status"] == "released":
            pid = record("late", 0, "名额释放后才到达，未入账")
            self._open_exception(order_id, "late_payment", amount_cents, pid,
                                 f"回执 {receipt_no} 在名额释放后到达（付款时刻 {paid_at}）")
            self._event(order_id, "late_receipt",
                        f"迟到回执 {receipt_no}，未入账，挂异常款", amount_cents)
            return "late_exception"
        if o["status"] == "cancelled" or a["status"] == "cancelled":
            pid = record("late", 0, "活动取消后到达，未入账")
            self._open_exception(order_id, "cancel_payment", amount_cents, pid,
                                 f"取消预售之后到达的回执 {receipt_no}")
            self._event(order_id, "late_receipt",
                        f"活动取消后回执 {receipt_no}，挂异常款", amount_cents)
            return "cancelled_exception"
        # 3) 已付清订单再来的钱：重复/超额 -> 异常款待退
        if o["status"] in ("paid", "fulfilled"):
            pid = record("duplicate", 0, "订单已付清，超额到账未入账")
            self._open_exception(order_id, "overpayment", amount_cents, pid,
                                 f"订单已结清后仍收到 {amount_cents} 分（{receipt_no}）")
            self._event(order_id, "overpaid_receipt",
                        f"结清后到账 {receipt_no}，挂异常款待退", amount_cents)
            return "overpaid_exception"

        # 4) 仅在尾款窗口内允许入账（以本事务取到的虚拟时间为准）
        wstate = Window(self.now()).state(a["balance_open_at"], a["balance_close_at"])
        if wstate == "before":
            raise PaymentRejected("尾款窗口尚未开始")
        if wstate == "after":
            pid = record("late", 0, "超过尾款截止时间，未入账")
            self._open_exception(order_id, "late_payment", amount_cents, pid,
                                 f"截止 {a['balance_close_at']} 后到账（{receipt_no}）")
            self._event(order_id, "overdue_receipt",
                        f"过截止时间回执 {receipt_no}，挂异常款", amount_cents)
            # 回执到达即证明已过截止：与名额回收在同一事务原子完成，
            # 避免“未扫描 + 有回执”的订单长期占着名额
            self._release_order_locked(o, a)
            return "overdue_exception"

        due = o["balance_cents"] * o["qty"] - o["paid_balance_cents"]
        if amount_cents > due:
            pid = record("duplicate", 0, f"超出应付 {amount_cents - due} 分，未入账")
            self._open_exception(order_id, "overpaid_balance", amount_cents, pid,
                                 f"本次 {amount_cents} 分 > 应付 {due} 分（{receipt_no}）")
            self._event(order_id, "overpaid_receipt",
                        f"超额回执 {receipt_no}，挂异常款", amount_cents)
            return "overpaid_exception"

        # 5) 正常入账（部分或结清；结清即 reserved->paid，进入配货）
        record("normal", 1, "")
        new_balance = o["paid_balance_cents"] + amount_cents
        if new_balance == o["balance_cents"] * o["qty"]:
            self.conn.execute(
                "UPDATE orders SET status='paid',paid_balance_cents=? WHERE id=?",
                (new_balance, order_id),
            )
            self.conn.execute(
                "UPDATE activities SET reserved=reserved-?, paid=paid+? WHERE id=?",
                (o["qty"], o["qty"], o["activity_id"]),
            )
            self._event(order_id, "balance_paid", f"回执 {receipt_no}", amount_cents)
            self._event(order_id, "paid_ready", "名额转为已付，进入配货")
            return "paid"
        self.conn.execute(
            "UPDATE orders SET status='partial',paid_balance_cents=? WHERE id=?",
            (new_balance, order_id),
        )
        self._event(order_id, "balance_partial",
                    f"回执 {receipt_no}，尚余 "
                    f"{o['balance_cents'] * o['qty'] - new_balance} 分",
                    amount_cents)
        return "partial"

    def _release_order_locked(self, o: sqlite3.Row, a: sqlite3.Row,
                              reason: str = "超过尾款截止时间，名额回收并回补可售") -> None:
        """在已持有写事务的前提下释放单个订单（幂等：仅 reserved/partial 生效）。"""
        if o["status"] not in ("reserved", "partial"):
            return
        now_s = self.now_iso
        if o["status"] == "partial":
            self._open_exception(
                o["id"], "partial_timeout", o["paid_balance_cents"], None,
                f"部分支付 {o['paid_balance_cents']} 分后逾期，待退款")
        self.conn.execute(
            "UPDATE orders SET status='released', released_at=? WHERE id=?",
            (now_s, o["id"]),
        )
        self.conn.execute(
            "UPDATE activities SET reserved=reserved-?, released=released+? WHERE id=?",
            (o["qty"], o["qty"], a["id"]),
        )
        self._event(o["id"], "released", reason)
        if o["paid_deposit_cents"]:
            self._event(o["id"], "refund",
                        "定金自动退回（模拟渠道）", o["paid_deposit_cents"])
        if o["paid_balance_cents"]:
            self._event(o["id"], "refund",
                        "部分尾款挂起，由财务在异常款队列审核退款",
                        o["paid_balance_cents"])

    # ---------- 异常款 ----------
    def _open_exception(self, order_id: int, etype: str, amount: int,
                        payment_id: Optional[int], detail: str):
        # 同一 payment 不重复开单
        if payment_id is not None:
            dup = self.conn.execute(
                "SELECT 1 FROM exceptions WHERE payment_id=?", (payment_id,)
            ).fetchone()
            if dup:
                return
        self.conn.execute(
            """INSERT INTO exceptions(order_id,type,opened_at,payment_id,
               amount_cents,detail) VALUES(?,?,?,?,?,?)""",
            (order_id, etype, self.now_iso, payment_id, amount, detail),
        )
        self._event(order_id, "exception_opened", f"{etype}：{detail}", amount)

    def resolve_exception(self, exc_id: int, action: str, note: str, actor: str) -> None:
        """财务审核异常款。action: refund / ship / reject。"""
        # 允许传入中文角色名（页面传“财务”）
        if actor not in ("finance", "财务"):
            raise PresaleError("仅财务可审核异常款")
        try:
            self._tx()
            e = self.conn.execute("SELECT * FROM exceptions WHERE id=?", (exc_id,)).fetchone()
            if e is None:
                raise PresaleError("异常单不存在")
            if e["status"] != "open":
                raise PresaleError("该异常单已处理")
            o = self._order(e["order_id"])
            a = self._activity(o["activity_id"])
            amount = e["amount_cents"]

            if action == "reject":
                self._finish_exception(exc_id, "reject", note, actor,
                                       "财务驳回：维持挂账，不退款不发货")
                self.conn.commit()
                return

            if action == "refund":
                # 模拟原路退回；任何状态都只退钱，不动名额
                self.conn.execute(
                    "INSERT INTO order_events(order_id,at,kind,detail,amount_cents) "
                    "VALUES(?,?, 'refund', ?, ?)",
                    (o["id"], self.now_iso,
                     f"财务批准退款（{e['type']}）：{note}", amount),
                )
                self._finish_exception(exc_id, "refund", note, actor,
                                       f"退款 {amount} 分（模拟渠道）")
                self.conn.commit()
                return

            if action == "ship":
                if e["type"] != "late_payment":
                    raise PresaleError("只有释放后迟到付款可批准补发货")
                # 占用回补/空闲名额：必须还有可售库存；released 计数相应回落
                if self.available_qty(a) < o["qty"]:
                    raise PresaleError("已无回补名额，无法批准发货（请改退）")
                take_released = min(o["qty"], a["released"])
                self.conn.execute(
                    "UPDATE activities SET paid=paid+?, released=released-? WHERE id=?",
                    (o["qty"], take_released, a["id"]),
                )
                self.conn.execute(
                    """UPDATE orders SET status='paid', disposition='ship',
                       disposition_note=?, paid_balance_cents=? WHERE id=?""",
                    (note, o["balance_cents"] * o["qty"], o["id"]),
                )
                # 迟到回执此时才入账
                if e["payment_id"]:
                    self.conn.execute(
                        "UPDATE payments SET applied_to_order=1 WHERE id=?",
                        (e["payment_id"],),
                    )
                self._event(o["id"], "replenished",
                            f"财务批准迟到付款，占用回补名额 {o['qty']} 个并恢复配货")
                self._event(o["id"], "paid_ready", "尾款结清（异常款批准发货）")
                self._finish_exception(exc_id, "ship", note, actor,
                                       "批准占用回补名额发货")
                self.conn.commit()
                return

            raise PresaleError(f"未知处理动作 {action}")
        except Exception:
            self.conn.rollback()
            raise

    def _finish_exception(self, exc_id, disposition, note, actor, detail):
        self.conn.execute(
            "UPDATE exceptions SET status='resolved' WHERE id=?", (exc_id,)
        )
        e = self.conn.execute("SELECT * FROM exceptions WHERE id=?", (exc_id,)).fetchone()
        self.conn.execute(
            """UPDATE orders SET disposition=?, disposition_note=? WHERE id=?""",
            (disposition, note, e["order_id"]),
        )
        self._event(e["order_id"], "exception_resolved",
                    f"{actor}：{detail}")

    # ---------- 逾期释放（与支付竞争同一把写锁） ----------
    def sweep_expired(self) -> list[int]:
        released_orders = []
        try:
            self._tx()
            now_s = self.now_iso
            overdue_acts = self.conn.execute(
                "SELECT * FROM activities WHERE status='active' AND balance_close_at < ?",
                (now_s,),
            ).fetchall()
            for a in overdue_acts:
                orders = self.conn.execute(
                    "SELECT * FROM orders WHERE activity_id=? AND status IN ('reserved','partial')",
                    (a["id"],),
                ).fetchall()
                for o in orders:
                    self._release_order_locked(o, a,
                        f"超过尾款截止 {a['balance_close_at']}，名额回收并回补可售")
                    released_orders.append(o["id"])
            self.conn.commit()
            return released_orders
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 取消预售（运营/客服请求批准） ----------
    def cancel_activity(self, activity_id: int, note: str, actor: str) -> None:
        try:
            self._tx()
            a = self._activity(activity_id)
            if a["status"] == "cancelled":
                raise PresaleError("活动已取消")
            self.conn.execute(
                "UPDATE activities SET status='cancelled',cancel_note=? WHERE id=?",
                (note, activity_id),
            )
            pendings = self.conn.execute(
                "SELECT * FROM orders WHERE activity_id=? AND status IN ('reserved','partial')",
                (activity_id,),
            ).fetchall()
            for o in pendings:
                self.conn.execute(
                    "UPDATE orders SET status='cancelled' WHERE id=?", (o["id"],)
                )
                self.conn.execute(
                    "UPDATE activities SET reserved=reserved-? WHERE id=?",
                    (o["qty"], activity_id),
                )
                self._event(o["id"], "activity_cancelled", f"{actor} 取消预售：{note}")
                total = o["paid_deposit_cents"] + o["paid_balance_cents"]
                if total:
                    self.conn.execute(
                        "UPDATE orders SET cancel_refunded_cents=? WHERE id=?",
                        (total, o["id"]),
                    )
                    self._event(o["id"], "refund",
                                "活动取消，定金及部分尾款原路退回（模拟）", total)
            # 已付清订单继续履约
            for o in self.conn.execute(
                "SELECT * FROM orders WHERE activity_id=? AND status IN ('paid','fulfilled')",
                (activity_id,),
            ):
                self._event(o["id"], "activity_cancelled",
                            "活动取消但尾款已付清，本单照常配货发货")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 配货发货 ----------
    def fulfill(self, order_id: int) -> None:
        try:
            self._tx()
            o = self._order(order_id)
            if o["status"] != "paid":
                raise PresaleError("仅已付清待配货订单可发货")
            self.conn.execute("UPDATE orders SET status='fulfilled' WHERE id=?", (order_id,))
            self._event(order_id, "fulfilled", "配货完成")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 客服处理请求 ----------
    ROLES = ("ops", "finance", "csr")

    def open_service_request(self, order_id: int, request_type: str,
                             reason: str, actor: str) -> int:
        if actor != "csr":
            raise PresaleError("仅客服可发起处理请求")
        try:
            self._tx()
            self._order(order_id)
            cur = self.conn.execute(
                """INSERT INTO service_requests(order_id,opened_by,at,request_type,
                   reason,status) VALUES(?,?,?,?,?, 'pending')""",
                (order_id, actor, self.now_iso, request_type, reason),
            )
            rid = cur.lastrowid
            self._event(order_id, "service_opened",
                        f"客服请求 #{rid}（{request_type}）：{reason}")
            self.conn.commit()
            return rid
        except Exception:
            self.conn.rollback()
            raise

    def review_service_request(self, request_id: int, approve: bool,
                               note: str, actor: str) -> None:
        if actor not in ("ops", "finance"):
            raise PresaleError("仅运营/财务可审核处理请求")
        try:
            self._tx()
            r = self.conn.execute(
                "SELECT * FROM service_requests WHERE id=?", (request_id,)
            ).fetchone()
            if r is None:
                raise PresaleError("处理请求不存在")
            if r["status"] != "pending":
                raise PresaleError("该请求已审核")
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE service_requests SET status=?,reviewed_by=?,reviewed_at=?,"
                "review_note=? WHERE id=?",
                (status, actor, self.now_iso, note, request_id),
            )
            o = self._order(r["order_id"])
            self._event(o["id"], "service_reviewed",
                        f"请求 #{request_id} 被 {actor} {status}：{note}")
            if approve and r["request_type"] == "release_early" and \
                    o["status"] in ("reserved", "partial"):
                # 走释放逻辑（复用 sweep 的单订单路径）
                self.conn.execute(
                    "UPDATE orders SET status='released', released_at=? WHERE id=?",
                    (self.now_iso, o["id"]),
                )
                self.conn.execute(
                    "UPDATE activities SET reserved=reserved-?, released=released+? WHERE id=?",
                    (o["qty"], o["qty"], o["activity_id"]),
                )
                self._event(o["id"], "released", "客服申请、运营批准提前释放名额")
                if o["paid_deposit_cents"]:
                    self._event(o["id"], "refund", "定金退回（模拟）",
                                o["paid_deposit_cents"])
            elif approve and r["request_type"] == "cancel_activity":
                pass  # 事务提交后执行取消
            self.conn.commit()
            if approve and r["request_type"] == "cancel_activity":
                self.cancel_activity(o["activity_id"],
                                     f"客服申请 #{request_id}：{r['reason']}", actor)
        except Exception:
            self.conn.rollback()
            raise

    def confirm_spec_change(self, order_id: int, note: str, actor: str) -> None:
        """客户经客服确认接受新规格；价格仍按下单锁定版本。"""
        if actor not in ("ops",):
            raise PresaleError("规格变更确认由运营执行")
        try:
            self._tx()
            o = self._order(order_id)
            a = self._activity(o["activity_id"])
            rv = self.conn.execute(
                "SELECT * FROM rule_versions WHERE activity_id=? AND version=?",
                (a["id"], a["current_version"]),
            ).fetchone()
            self.conn.execute(
                "UPDATE orders SET spec_snapshot=? WHERE id=?",
                (rv["spec"], order_id),
            )
            self._event(order_id, "spec_updated",
                        f"{note}；定金/尾款仍按 v{o['rule_version']} 锁定价结算")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
