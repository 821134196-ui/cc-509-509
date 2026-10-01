"""定金规则版本锁定：下单后发布新版本，不影响老订单；规格变更同理。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest

from app import clock, services

from tests.conftest_support import fresh, get_order, make_activity


class RuleVersionTest(unittest.TestCase):
    def test_order_snapshots_rule_version(self):
        conn = fresh()
        t = clock.now()
        aid = make_activity(conn, quota=10, deposit=5000, balance=15000,
                            specs=(("黑", 0), ("白", 1000)))

        o1 = services.create_order(conn, aid, "白")
        self.assertEqual(o1["rule_version"], 1)
        self.assertEqual(o1["balance_amount"], 16000)
        self.assertEqual(o1["total_amount"], 21000)

        # 活动运营发布新规则：定金 80、尾款 120
        rule = services.get_active_rule(conn, aid)
        ver = services.add_rule_version(
            conn, aid, 8000, 12000,
            rule["deposit_open"], rule["deposit_close"],
            rule["balance_open"], rule["balance_close"], 60)
        self.assertEqual(ver, 2)

        o2 = services.create_order(conn, aid, "白")
        self.assertEqual(o2["rule_version"], 2)
        self.assertEqual(o2["deposit_amount"], 8000)
        self.assertEqual(o2["balance_amount"], 13000)

        # 老订单金额、截止时间、版本号不变
        old = get_order(conn, o1["code"])
        self.assertEqual(old["rule_version"], 1)
        self.assertEqual(old["deposit_amount"], 5000)
        self.assertEqual(old["balance_amount"], 16000)
        self.assertEqual(old["release_grace_sec"], 30)

    def test_spec_change_does_not_affect_existing_orders(self):
        conn = fresh()
        aid = make_activity(conn, quota=10, specs=(("黑", 0), ("白", 1000)))
        o = services.create_order(conn, aid, "白")
        before = conn.execute("SELECT spec,extra_cents FROM sku WHERE id=?",
                              (o["sku_id"],)).fetchone()
        self.assertEqual(before["spec"], "白")

        # 规格变更：白款加价取消、新增金款、黑款下架
        services.change_specs(conn, aid, [("白", 0), ("金", 3000)])
        after = conn.execute("SELECT spec,extra_cents FROM sku WHERE id=?",
                             (o["sku_id"],)).fetchone()
        # 历史订单引用的 sku 行就是当时下单的行；加价调整只影响新下单展示
        self.assertEqual(after["spec"], "白")
        self.assertEqual(after["extra_cents"], 0)
        # 老订单尾款快照不变
        self.assertEqual(get_order(conn, o["code"])["balance_amount"], 16000)
        specs = {r["spec"]: r["extra_cents"]
                 for r in conn.execute("SELECT spec,extra_cents FROM sku WHERE activity_id=?",
                                       (aid,))}
        self.assertEqual(specs.get("金"), 3000)
        self.assertIn("[已下架] 黑", specs)


if __name__ == "__main__":
    unittest.main()
