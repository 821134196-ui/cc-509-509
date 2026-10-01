"""支付与名额释放竞争：任何订单都不能“既退款又发货”。

在尾款截止临界点并发执行付款与释放，逐单验证最终归属互斥：
- 赢下竞争的付款：订单 paid，可配货发货，不存在迟到异常款；
- 赢下竞争的释放：订单 released，迟到款进异常款，财务可退款；
  该订单永远不会进入配货/发货。
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clock, db, services

from tests.conftest_support import make_activity


class PaymentReleaseRaceTest(unittest.TestCase):
    def test_race_mutual_exclusion(self):
        tmp = Path(tempfile.mkdtemp()) / "race.db"
        conn = db.connect(tmp)
        db.init_db(conn)
        N = 30
        # 尾款窗口“此刻”截止，宽限 0：付款与释放站在同一条临界线上
        aid = make_activity(conn, quota=N + 5, balance_open=-1000,
                            balance_close=0, grace=0)
        codes = []
        for i in range(N):
            o = services.create_order(conn, aid, "黑")
            services.apply_payment(conn, o["code"], "deposit", 5000, f"D{i}")
            codes.append(o["code"])
        batch = conn.execute(
            "SELECT id FROM shipment_batch WHERE activity_id=?", (aid,)).fetchone()["id"]
        conn.close()

        barrier = threading.Barrier(N * 2)

        def payer(code, i):
            c = db.connect(tmp)
            barrier.wait()
            try:
                services.apply_payment(c, code, "balance", 15000, f"B{i}")
            except services.ServiceError:
                pass
            finally:
                c.close()

        def releaser():
            c = db.connect(tmp)
            barrier.wait()
            try:
                services.release_overdue(c)
            finally:
                c.close()

        threads = []
        for i, code in enumerate(codes):
            threads.append(threading.Thread(target=payer, args=(code, i)))
            threads.append(threading.Thread(target=releaser))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        c = db.connect(tmp)
        paid, released = [], []
        for code in codes:
            o = c.execute('SELECT * FROM "order" WHERE code=?', (code,)).fetchone()
            self.assertIn(o["status"], (services.PAID, services.RELEASED),
                          f"{code} 终态只能是 paid 或 released，实际 {o['status']}")
            late = c.execute(
                "SELECT COUNT(*) n FROM exception_case WHERE order_id=? AND kind='late_after_release'",
                (o["id"],)).fetchone()["n"]
            if o["status"] == services.PAID:
                paid.append(o["id"])
                self.assertEqual(late, 0, "付款成功的订单不能有迟到异常款")
            else:
                released.append(o["id"])
                self.assertEqual(late, 1, "释放后到账必须恰好挂一笔迟到异常款")
                pay = c.execute(
                    "SELECT status FROM payment WHERE order_id=? AND stage='balance'",
                    (o["id"],)).fetchone()
                self.assertEqual(pay["status"], "exception")

        # 全部已付订单进入配货并发货
        services.allocate(c, aid, batch)
        services.ship_batch(c, batch, at=clock.add(clock.now(), 1000))
        shipped = {r["id"] for r in c.execute(
            'SELECT id FROM "order" WHERE activity_id=? AND status=?',
            (aid, services.SHIPPED))}
        # 核心不变量：发货集合与释放集合互不相交
        self.assertTrue(shipped.isdisjoint(set(released)),
                        "被释放的订单绝不能被发货")
        self.assertEqual(shipped, set(paid))

        # 财务对每笔迟到异常款批准退款 => 退款集合同样不得与发货集合相交
        for oid in released:
            case = c.execute(
                "SELECT id FROM exception_case WHERE order_id=? AND status='open'",
                (oid,)).fetchone()
            services.review_exception(c, case["id"], "approve_refund")
        refunded = {r["id"] for r in c.execute(
            'SELECT id FROM "order" WHERE status=?', (services.REFUNDED,))}
        self.assertTrue(refunded.isdisjoint(shipped), "不能既退款又发货")
        self.assertEqual(refunded, set(released))
        c.close()

    def test_payment_wins_then_release_noop(self):
        """付款先提交：随后释放必须跳过该单。"""
        conn = db.connect(":memory:")
        db.init_db(conn)
        aid = make_activity(conn, quota=2, balance_open=-100, balance_close=0, grace=0)
        o = services.create_order(conn, aid, "黑")
        services.apply_payment(conn, o["code"], "deposit", 5000, "D")
        services.apply_payment(conn, o["code"], "balance", 15000, "B")
        self.assertEqual(services.release_overdue(conn), 0)
        self.assertEqual(
            conn.execute('SELECT status FROM "order" WHERE id=?', (o["id"],)).fetchone()["status"],
            services.PAID)

    def test_release_wins_then_payment_refundable_not_shippable(self):
        """释放先提交：迟到付款只能退款，财务强制核准前绝不发货。"""
        conn = db.connect(":memory:")
        db.init_db(conn)
        aid = make_activity(conn, quota=2, balance_open=-100, balance_close=0, grace=0)
        batch = conn.execute(
            "SELECT id FROM shipment_batch WHERE activity_id=?", (aid,)).fetchone()["id"]
        o = services.create_order(conn, aid, "黑")
        services.apply_payment(conn, o["code"], "deposit", 5000, "D")
        self.assertEqual(services.release_overdue(conn), 1)
        res = services.apply_payment(conn, o["code"], "balance", 15000, "B")
        self.assertEqual(res["result"], "exception")
        # 此时即便执行配货，也不会带上它
        self.assertEqual(services.allocate(conn, aid, batch), [])
        services.review_exception(
            conn, conn.execute(
                "SELECT id FROM exception_case WHERE order_id=?", (o["id"],)).fetchone()["id"],
            "approve_refund")
        st = conn.execute('SELECT status FROM "order" WHERE id=?', (o["id"],)).fetchone()["status"]
        self.assertEqual(st, services.REFUNDED)


if __name__ == "__main__":
    unittest.main()
