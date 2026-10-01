"""领域规则测试：不超卖、延迟回执、规则版本锁定、重复支付、支付/释放竞争。

直接用服务层 + SQLite 验证；时间冻结（TICK_MULTIPLIER=0），靠 reset_to 拨钟。
运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

import presale.db as dbmod
from presale.db import connect, Clock
from presale.service import PresaleService, PresaleError


def iso(dt):
    return dt.isoformat()


class PresaleTestCase(unittest.TestCase):
    def setUp(self):
        # 冻结虚拟时钟：真实时间流逝不影响测试
        self._old_mult = dbmod.TICK_MULTIPLIER
        dbmod.TICK_MULTIPLIER = 0
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        self.conn = connect(self.path)
        self.clock = Clock(self.conn)
        self.t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self.clock.reset_to(self.t0)
        self.svc = PresaleService(self.conn, self.clock)

    def tearDown(self):
        self.conn.close()
        dbmod.TICK_MULTIPLIER = self._old_mult
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.remove(p)

    # ---------- 辅助 ----------
    def add_activity(self, stock=3, deposit=1000, balance=4000,
                     dep_open=-60, dep_close=86400,
                     bal_open=-60, bal_close=600, spec="黑 / 128G",
                     sku="T", status="active"):
        t0 = self.t0
        cur = self.conn.execute(
            """INSERT INTO activities(sku,name,color,image,stock,
               deposit_open_at,deposit_close_at,balance_open_at,balance_close_at,
               current_version,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (sku, "测试商品", "#000000", "aurora.svg", stock,
             iso(t0 + timedelta(seconds=dep_open)), iso(t0 + timedelta(seconds=dep_close)),
             iso(t0 + timedelta(seconds=bal_open)), iso(t0 + timedelta(seconds=bal_close)),
             1, status),
        )
        aid = cur.lastrowid
        self.conn.execute(
            """INSERT INTO rule_versions(activity_id,version,deposit_cents,
               balance_cents,spec,created_at,note) VALUES(1,?,?,?,?,?,'v1')""",
            (aid, 1, deposit, balance, spec, iso(t0)),
        ) if False else self.conn.execute(
            """INSERT INTO rule_versions(activity_id,version,deposit_cents,
               balance_cents,spec,created_at,note) VALUES(?,?,?,?,?,?,?)""",
            (aid, 1, deposit, balance, spec, iso(t0), "v1"),
        )
        self.conn.commit()
        return aid

    def counters(self, aid):
        a = self.svc.get_activity(aid)
        return a["reserved"], a["paid"], a["released"], a["stock"]


class TestNoOversell(PresaleTestCase):
    def test_concurrent_create_never_oversells(self):
        """20 个线程抢 3 个名额：恰好 3 单成功，计数守恒。"""
        aid = self.add_activity(stock=3)
        results = []

        def worker():
            # 每个线程独立连接，真正走数据库级锁竞争
            conn = connect(self.path)
            clk = Clock(conn)
            svc = PresaleService(conn, clk)
            try:
                svc.create_order(aid)
                results.append("ok")
            except PresaleError:
                results.append("reject")
            except Exception as e:
                results.append(f"error:{e!r}")
            conn.close()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("ok"), 3)
        self.assertEqual(results.count("reject"), 17)
        self.assertEqual(self.counters(aid), (3, 0, 0, 3))

    def test_released_quota_is_replenished(self):
        """释放名额回补可售：2 名额 -> 释放 -> 可再下单，计数始终守恒。"""
        aid = self.add_activity(stock=2, bal_close=-1)
        o1 = self.svc.create_order(aid)
        self.svc.create_order(aid)
        self.assertEqual(self.counters(aid), (2, 0, 0, 2))
        with self.assertRaises(PresaleError):
            self.svc.create_order(aid)

        # 窗口已过：两单都释放，名额回补
        self.svc.sweep_expired()
        self.assertEqual(self.counters(aid), (0, 0, 2, 2))
        # 回补后可再售 2 个；售出一个，“释放”计数相应回落
        self.svc.create_order(aid)
        self.assertEqual(self.counters(aid), (1, 0, 1, 2))
        self.svc.create_order(aid)
        self.assertEqual(self.counters(aid), (2, 0, 0, 2))
        with self.assertRaises(PresaleError):
            self.svc.create_order(aid)

    def test_finance_ship_uses_replenished_quota(self):
        """财务批准迟到付款发货：占用回补名额，无名额时拒绝。"""
        aid = self.add_activity(stock=1, bal_close=-1)
        o1 = self.svc.create_order(aid)
        self.svc.sweep_expired()
        self.assertEqual(self.counters(aid), (0, 0, 1, 1))
        # 释放后迟到全款
        rn = self.svc.send_balance_receipt(o1, 4000)
        self.svc.deliver_pending()
        exc = [e for e in self.svc.list_exceptions("open")
               if e["type"] == "late_payment"][0]
        # 有 1 个回补名额 -> 批准发货
        self.svc.resolve_exception(exc["id"], "ship", "核实付款在窗口内", "财务")
        self.assertEqual(self.counters(aid), (0, 1, 0, 1))
        o = self.svc.get_order(o1)
        self.assertEqual(o["status"], "paid")
        self.assertEqual(o["paid_balance_cents"], 4000)
        # 已处理异常单不能再处理（杜绝既退又发）
        with self.assertRaises(PresaleError):
            self.svc.resolve_exception(exc["id"], "refund", "再退一次", "财务")
        # released 订单不能直接发货
        aid2 = self.add_activity(stock=1, bal_close=-1, sku="T2")
        o2 = self.svc.create_order(aid2)
        self.svc.sweep_expired()
        with self.assertRaises(PresaleError):
            self.svc.fulfill(o2)


class TestDelayedReceipt(PresaleTestCase):
    def test_receipt_delayed_until_window_close(self):
        """回执在途期间不入账；窗口内迟到仍可结清；截止后到挂异常。"""
        # 窗口 300 秒后关闭
        aid = self.add_activity(stock=2, bal_close=300)
        o1 = self.svc.create_order(aid)
        o2 = self.svc.create_order(aid)

        # o1：回执延迟 60 秒（窗口内）
        self.svc.send_balance_receipt(o1, 4000, delay_seconds=60)
        # o2：回执延迟 600 秒（截止后）
        self.svc.send_balance_receipt(o2, 4000, delay_seconds=600)
        self.assertEqual(self.svc.get_order(o1)["status"], "reserved")
        self.assertEqual(self.svc.get_order(o2)["status"], "reserved")
        self.assertEqual(len(self.svc.payments_of(o1)), 1)  # 仅定金台账

        # 60 秒：o1 自动结清进配货；o2 回执仍在途，订单不动
        self.clock.advance(60)
        self.svc.deliver_pending()
        self.assertEqual(self.svc.get_order(o1)["status"], "paid")
        self.assertEqual(self.counters(aid), (1, 1, 0, 2))
        self.assertEqual(self.svc.get_order(o2)["status"], "reserved")
        # 窗口未到点，扫描不误伤
        self.svc.sweep_expired()
        self.assertEqual(self.svc.get_order(o2)["status"], "reserved")

        # +301 秒：过截止，扫描释放 o2
        self.clock.advance(241)
        self.svc.sweep_expired()
        self.assertEqual(self.svc.get_order(o2)["status"], "released")
        self.assertEqual(self.counters(aid), (0, 1, 1, 2))
        # +600 秒：o2 的回执才到 -> late_payment 异常款
        self.clock.advance(299)
        self.svc.deliver_pending()
        late = [e for e in self.svc.list_exceptions("open")
                if e["order_id"] == o2 and e["type"] == "late_payment"]
        self.assertEqual(len(late), 1)
        # 迟到的钱没有入账、没有转已付、没有发货
        self.assertEqual(self.svc.get_order(o2)["status"], "released")
        self.assertEqual(self.counters(aid), (0, 1, 1, 2))
        pays = [p for p in self.svc.payments_of(o2) if p["phase"] == "balance"]
        self.assertEqual(pays[0]["applied_to_order"], 0)

        # 财务退款后状态依旧 released（只退钱不占名额）
        self.svc.resolve_exception(late[0]["id"], "refund", "原路退回", "财务")
        self.assertEqual(self.counters(aid), (0, 1, 1, 2))
        self.assertEqual(self.svc.get_order(o2)["status"], "released")
        kinds = {e["kind"] for e in self.svc.timeline(o2)}
        self.assertIn("released", kinds)
        self.assertNotIn("paid_ready", kinds)

    def test_partial_payment_then_timeout(self):
        """部分支付：显示欠款；逾期释放后已付部分挂异常待退。"""
        aid = self.add_activity(stock=1, balance=4000, bal_close=300)
        o1 = self.svc.create_order(aid)
        self.svc.send_balance_receipt(o1, 1500)
        self.svc.deliver_pending()
        o = self.svc.get_order(o1)
        self.assertEqual(o["status"], "partial")
        self.assertEqual(o["paid_balance_cents"], 1500)

        self.clock.advance(301)
        self.svc.sweep_expired()
        self.assertEqual(self.svc.get_order(o1)["status"], "released")
        exc = [e for e in self.svc.list_exceptions("open")
               if e["type"] == "partial_timeout"]
        self.assertEqual(len(exc), 1)
        self.assertEqual(exc[0]["amount_cents"], 1500)
        self.assertEqual(self.counters(aid), (0, 0, 1, 1))


class TestRuleVersion(PresaleTestCase):
    def test_version_locked_at_order_time(self):
        """下单锁版本；之后调价/改规格不影响老订单，只影响新订单。"""
        aid = self.add_activity(stock=3, deposit=1000, balance=4000,
                                spec="黑 / 128G")
        old = self.svc.create_order(aid)
        oo = self.svc.get_order(old)
        self.assertEqual((oo["deposit_cents"], oo["balance_cents"]), (1000, 4000))
        self.assertEqual(oo["spec_snapshot"], "黑 / 128G")

        self.svc.publish_version(aid, 1500, 5000, "白 / 256G", "涨价改规格")
        new = self.svc.create_order(aid)
        on = self.svc.get_order(new)
        self.assertEqual(on["rule_version"], 2)
        self.assertEqual((on["deposit_cents"], on["balance_cents"]), (1500, 5000))
        self.assertEqual(on["spec_snapshot"], "白 / 256G")

        # 老订单仍按 v1 尾款结清
        self.svc.send_balance_receipt(old, 4000)
        self.svc.deliver_pending()
        self.assertEqual(self.svc.get_order(old)["status"], "paid")
        # 新订单按 v2：付 v1 的钱只算部分支付
        self.svc.send_balance_receipt(new, 4000)
        self.svc.deliver_pending()
        self.assertEqual(self.svc.get_order(new)["status"], "partial")
        self.assertEqual(self.svc.get_order(new)["paid_balance_cents"], 4000)


class TestDuplicatePayment(PresaleTestCase):
    def test_same_receipt_never_applied_twice(self):
        aid = self.add_activity(stock=1)
        o1 = self.svc.create_order(aid)
        pid = self.svc.send_balance_receipt(o1, 4000)
        row = self.conn.execute(
            "SELECT id FROM pending_receipts WHERE receipt_no=?", (pid,)).fetchone()
        self.svc.deliver_pending()
        self.assertEqual(self.svc.get_order(o1)["status"], "paid")

        # 渠道重放完全相同的回执：只记忽略事件
        result = self.svc.send_duplicate(row["id"])
        self.assertEqual(result, "duplicate_ignored")
        bal_pays = [p for p in self.svc.payments_of(o1) if p["phase"] == "balance"]
        applied = [p for p in bal_pays if p["applied_to_order"]]
        self.assertEqual(len(applied), 1)
        self.assertEqual(self.svc.get_order(o1)["paid_balance_cents"], 4000)
        kinds = [e["kind"] for e in self.svc.timeline(o1)]
        self.assertEqual(kinds.count("duplicate_receipt"), 1)

        # 已付清订单再来一笔新扣款：进异常款待退，不发货不二次入账
        self.svc.send_balance_receipt(o1, 4000)
        self.svc.deliver_pending()
        over = [e for e in self.svc.list_exceptions("open")
                if e["type"] == "overpayment"]
        self.assertEqual(len(over), 1)
        self.assertEqual(self.svc.get_order(o1)["paid_balance_cents"], 4000)

    def test_amount_over_due_rejected_to_exception(self):
        aid = self.add_activity(stock=1, balance=4000)
        o1 = self.svc.create_order(aid)
        self.svc.send_balance_receipt(o1, 5000)  # 多付 1000
        self.svc.deliver_pending()
        self.assertEqual(self.svc.get_order(o1)["status"], "reserved")
        exc = [e for e in self.svc.list_exceptions("open")
               if e["type"] == "overpaid_balance"]
        self.assertEqual(len(exc), 1)


class TestPaymentReleaseRace(PresaleTestCase):
    def test_concurrent_sweep_and_deliver_never_refund_and_ship(self):
        """过期边界：扫释放与投递回执并发，每个订单只能有一个结局。"""
        n = 12
        aid = self.add_activity(stock=n, bal_close=-1)
        order_ids = [self.svc.create_order(aid) for _ in range(n)]
        # 所有回执“付款时刻”在窗口内，但此刻尚未投递（系统已过截止）
        paid_at = iso(self.t0 - timedelta(seconds=30))
        arrive = iso(self.t0 - timedelta(seconds=5))
        for i, oid in enumerate(order_ids):
            self.svc.queue_receipt(oid, "balance", 4000, f"RACE-{i:03d}",
                                   paid_at, arrive)

        barrier = threading.Barrier(n)

        def worker(i):
            conn = connect(self.path)
            clk = Clock(conn)
            svc = PresaleService(conn, clk)
            barrier.wait()
            for _ in range(3):
                try:
                    if i % 2:
                        svc.deliver_pending()
                        svc.sweep_expired()
                    else:
                        svc.sweep_expired()
                        svc.deliver_pending()
                except Exception:
                    pass
            conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 重新读取校验
        reserved = paid = released = 0
        for oid in order_ids:
            o = self.svc.get_order(oid)
            kinds = {e["kind"] for e in self.svc.timeline(oid)}
            # 核心不变量：同一订单绝不可能“又发货/结清 又释放退款”
            self.assertFalse(
                {"paid_ready", "fulfilled"} & kinds and "released" in kinds,
                f"订单 {oid} 同时出现发货与释放")
            self.assertFalse(
                o["status"] == "paid" and "refund" in kinds,
                f"订单 {oid} 既退款又发货")
            self.assertIn(o["status"], ("released", "paid"))
            reserved += o["status"] == "reserved"
            paid += o["status"] in ("paid", "fulfilled")
            released += o["status"] == "released"
        a = self.svc.get_activity(aid)
        self.assertEqual((a["reserved"], a["paid"], a["released"]),
                         (reserved, paid, released))
        self.assertLessEqual(a["reserved"] + a["paid"] + a["released"], a["stock"])

    def test_payment_wins_inside_window(self):
        """窗口内：支付与扫描并发，付款成功的订单不被释放。"""
        n = 8
        aid = self.add_activity(stock=n, bal_close=600)
        order_ids = [self.svc.create_order(aid) for _ in range(n)]
        for i, oid in enumerate(order_ids):
            self.svc.queue_receipt(oid, "balance", 4000, f"WIN-{i:03d}",
                                   iso(self.t0), iso(self.t0))
        barrier = threading.Barrier(n)

        def worker(i):
            conn = connect(self.path)
            clk = Clock(conn)
            svc = PresaleService(conn, clk)
            barrier.wait()
            if i % 2:
                svc.deliver_pending(); svc.sweep_expired()
            else:
                svc.sweep_expired(); svc.deliver_pending()
            conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.counters(aid), (0, n, 0, n))
        for oid in order_ids:
            self.assertEqual(self.svc.get_order(oid)["status"], "paid")


class TestCancelAndRoles(PresaleTestCase):
    def test_cancel_refunds_unpaid_and_keeps_paid_orders(self):
        aid = self.add_activity(stock=2)
        o1 = self.svc.create_order(aid)
        o2 = self.svc.create_order(aid)
        self.svc.send_balance_receipt(o2, 4000)
        self.svc.deliver_pending()
        self.svc.cancel_activity(aid, "厂商延期", "运营")
        self.assertEqual(self.svc.get_order(o1)["status"], "cancelled")
        self.assertEqual(self.svc.get_order(o1)["cancel_refunded_cents"], 1000)
        self.assertEqual(self.svc.get_order(o2)["status"], "paid")
        # 取消后到账的钱落 cancel_payment 异常
        self.svc.send_balance_receipt(o1, 4000)
        self.svc.deliver_pending()
        self.assertTrue(any(e["type"] == "cancel_payment"
                            for e in self.svc.list_exceptions("open")))

    def test_role_boundaries(self):
        aid = self.add_activity(stock=1)
        o1 = self.svc.create_order(aid)
        # 客服不能审异常款/取消活动/发货
        from presale.service import PresaleError as PE
        with self.assertRaises(PE):
            self.svc.resolve_exception(1, "refund", "x", "csr")
        # 客服可发起处理请求
        rid = self.svc.open_service_request(o1, "release_early", "客户要求", "csr")
        # 客服不能自审
        with self.assertRaises(PE):
            self.svc.review_service_request(rid, True, "x", "csr")
        # 运营批准 -> 释放
        self.svc.review_service_request(rid, True, "同意", "ops")
        self.assertEqual(self.svc.get_order(o1)["status"], "released")
        self.assertEqual(self.counters(aid), (0, 0, 1, 1))


if __name__ == "__main__":
    unittest.main()
