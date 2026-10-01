"""重复回执：同一 receipt_no 多次到达只入账一次。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest

from app import clock, payment_adapter, services

from tests.conftest_support import fresh, get_order, make_activity


class DuplicateReceiptTest(unittest.TestCase):
    def _paid_deposit(self, conn):
        aid = make_activity(conn, quota=5, balance_open=-100, balance_close=10000)
        o = services.create_order(conn, aid, "黑")
        services.apply_payment(conn, o["code"], "deposit", 5000, "R-DUP-D")
        return o

    def test_duplicate_receipt_ignored(self):
        conn = fresh()
        o = self._paid_deposit(conn)
        r1 = services.apply_payment(conn, o["code"], "balance", 15000, "R-DUP-B")
        self.assertEqual(r1["result"], "applied")
        self.assertEqual(get_order(conn, o["code"])["status"], services.PAID)

        # 网关重放同一回执
        r2 = payment_adapter.replay(conn, "R-DUP-B")
        self.assertEqual(r2["result"], "duplicate")
        r3 = payment_adapter.replay(conn, "R-DUP-B")
        self.assertEqual(r3["result"], "duplicate")

        rows = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(paid),0) s FROM payment WHERE receipt_no='R-DUP-B'"
        ).fetchone()
        self.assertEqual(rows["n"], 1, "同一回执号只能有一条流水")
        self.assertEqual(rows["s"], 15000)
        # 已付合计不能因重复回执变多
        bal = conn.execute(
            "SELECT COALESCE(SUM(paid),0) s FROM payment WHERE order_id=? AND status='applied'",
            (o["id"],)).fetchone()["s"]
        self.assertEqual(bal, 20000)

    def test_partial_then_duplicate_then_rest(self):
        conn = fresh()
        o = self._paid_deposit(conn)
        services.apply_payment(conn, o["code"], "balance", 9000, "R-P1")
        self.assertEqual(get_order(conn, o["code"])["status"], services.BALANCE_PARTIAL)
        # 重复的部分款回执
        dup = payment_adapter.replay(conn, "R-P1")
        self.assertEqual(dup["result"], "duplicate")
        # 剩余部分到账，应正好付清而不是 9000+9000
        fin = services.apply_payment(conn, o["code"], "balance", 6000, "R-P2")
        self.assertEqual(fin["result"], "applied")
        self.assertEqual(get_order(conn, o["code"])["status"], services.PAID)


if __name__ == "__main__":
    unittest.main()
