"""取消预售：未付清订单取消并挂款待退；取消后到账进入异常款，绝不发货。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest

from app import clock, payment_adapter, services

from tests.conftest_support import FixedClock, fresh, get_order, make_activity


class CancelActivityTest(unittest.TestCase):
    def test_cancel_then_late_payment_is_exception(self):
        conn = fresh()
        aid = make_activity(conn, quota=5, balance_open=-100, balance_close=10000)
        o1 = services.create_order(conn, aid, "黑")
        services.apply_payment(conn, o1["code"], "deposit", 5000, "D1")
        o2 = services.create_order(conn, aid, "黑")  # 未付定金
        paid = services.create_order(conn, aid, "黑")
        services.apply_payment(conn, paid["code"], "deposit", 5000, "D3")
        services.apply_payment(conn, paid["code"], "balance", 15000, "B3")

        n = services.cancel_activity(conn, aid)
        self.assertEqual(get_order(conn, o1["code"])["status"], services.CANCELLED)
        self.assertEqual(get_order(conn, o2["code"])["status"], services.CANCELLED)
        # 已付清订单不受影响，继续配货
        self.assertEqual(get_order(conn, paid["code"])["status"], services.PAID)
        self.assertGreaterEqual(n, 2)

        # o1 定金应挂账退款
        case = conn.execute(
            "SELECT * FROM exception_case WHERE order_id=? AND kind='cancelled_activity'",
            (o1["id"],)).fetchone()
        self.assertIsNotNone(case)
        self.assertEqual(case["amount"], 5000)

        # 取消后迟到的尾款回执 -> cancelled_activity，不是 other，也不发货
        res = services.apply_payment(conn, o1["code"], "balance", 15000, "LATE")
        self.assertEqual(res["result"], "exception")
        self.assertEqual(res["kind"], "cancelled_activity")
        self.assertEqual(get_order(conn, o1["code"])["status"], services.CANCELLED)

        # 活动取消后不能再下单
        with self.assertRaises(services.ServiceError):
            services.create_order(conn, aid, "黑")


if __name__ == "__main__":
    unittest.main()
