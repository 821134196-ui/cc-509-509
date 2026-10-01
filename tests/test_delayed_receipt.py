"""支付回执延迟：尾款延迟到达，截止前正常入账；截止（含宽限）后进入异常款。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest

from app import clock, payment_adapter, services

from tests.conftest_support import FixedClock, fresh, get_order, make_activity


class DelayedReceiptTest(unittest.TestCase):
    def _prepared(self, conn, fc):
        aid = make_activity(conn, quota=5, balance_open=-100, balance_close=100, grace=30)
        o = services.create_order(conn, aid, "黑", at=fc.now())
        services.apply_payment(conn, o["code"], "deposit", 5000, "D1", at=fc.now())
        return o

    def test_receipt_before_deadline_applies(self):
        conn, fc = fresh(), FixedClock(clock.now())
        o = self._prepared(conn, fc)
        payment_adapter.charge(conn, o["code"], "balance", 15000, delay_sec=50, at=fc.now())
        fc.advance(50)  # 在尾款截止(+100s)之前到达
        n = payment_adapter.deliver_due(conn, at=fc.now())
        self.assertEqual(n, 1)
        self.assertEqual(get_order(conn, o["code"])["status"], services.PAID)

    def test_receipt_after_release_becomes_exception(self):
        conn, fc = fresh(), FixedClock(clock.now())
        o = self._prepared(conn, fc)
        payment_adapter.charge(conn, o["code"], "balance", 15000, delay_sec=140, at=fc.now())
        fc.advance(140)  # 超过截止+宽限(130s)，名额已被释放
        released = services.release_overdue(conn, at=fc.now())
        self.assertEqual(released, 1)
        self.assertEqual(get_order(conn, o["code"])["status"], services.RELEASED)

        n = payment_adapter.deliver_due(conn, at=fc.now())
        self.assertEqual(n, 1)
        # 迟到款绝不能把订单变成已付/发货
        self.assertEqual(get_order(conn, o["code"])["status"], services.RELEASED)
        case = conn.execute(
            "SELECT * FROM exception_case WHERE order_id=? AND kind='late_after_release'",
            (o["id"],)).fetchone()
        self.assertIsNotNone(case)
        self.assertEqual(case["amount"], 15000)
        self.assertEqual(case["status"], "open")
        pay = conn.execute("SELECT status FROM payment WHERE receipt_no LIKE 'PAY%'").fetchone()
        self.assertEqual(pay["status"], "exception")

    def test_partial_balance_then_release_refund_queue(self):
        conn, fc = fresh(), FixedClock(clock.now())
        o = self._prepared(conn, fc)
        services.apply_payment(conn, o["code"], "balance", 8000, "B1", at=fc.now())
        self.assertEqual(get_order(conn, o["code"])["status"], services.BALANCE_PARTIAL)
        fc.advance(140)
        self.assertEqual(services.release_overdue(conn, at=fc.now()), 1)
        case = conn.execute(
            "SELECT * FROM exception_case WHERE order_id=? AND kind='overpay'",
            (o["id"],)).fetchone()
        self.assertIsNotNone(case, "部分支付后释放，已付尾款必须挂异常款待退款")
        self.assertEqual(case["amount"], 8000)


class OverpayTest(unittest.TestCase):
    def test_balance_overpay_paid_but_queued(self):
        """尾款多付：订单付清可发货，多出部分挂异常款待财务退款。"""
        conn = fresh()
        aid = make_activity(conn, quota=5, balance_open=-100, balance_close=10000)
        o = services.create_order(conn, aid, "黑")
        services.apply_payment(conn, o["code"], "deposit", 5000, "D")
        services.apply_payment(conn, o["code"], "balance", 10000, "B1")
        r = services.apply_payment(conn, o["code"], "balance", 8000, "B2")  # 应15000，实付18000
        self.assertEqual(r["result"], "applied")
        self.assertEqual(get_order(conn, o["code"])["status"], services.PAID)
        case = conn.execute(
            "SELECT * FROM exception_case WHERE order_id=? AND kind='overpay'",
            (o["id"],)).fetchone()
        self.assertIsNotNone(case)
        self.assertEqual(case["amount"], 3000)

    def test_deposit_wrong_amount_rejected(self):
        conn = fresh()
        aid = make_activity(conn, quota=5, balance_open=10000, balance_close=20000)
        o = services.create_order(conn, aid, "黑")
        with self.assertRaises(services.ServiceError):
            services.apply_payment(conn, o["code"], "deposit", 4999, "D")
        # 订单未变，无流水残留
        self.assertEqual(get_order(conn, o["code"])["status"], services.AWAITING_DEPOSIT)
        self.assertEqual(conn.execute("SELECT COUNT(*) n FROM payment").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
