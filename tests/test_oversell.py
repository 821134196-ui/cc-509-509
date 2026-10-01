"""库存不超卖：并发下单，成功数必须恰等于名额。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tempfile
import threading
import unittest
from pathlib import Path

from app import db, services

from tests.conftest_support import make_activity


class OversellTest(unittest.TestCase):
    def test_concurrent_orders_never_oversell(self):
        tmp = Path(tempfile.mkdtemp()) / "t.db"
        conn = db.connect(tmp)
        db.init_db(conn)
        QUOTA = 5
        aid = make_activity(conn, quota=QUOTA, balance_open=-10, balance_close=100000)
        conn.close()

        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(40)

        def worker(i):
            c = db.connect(tmp)
            barrier.wait()
            try:
                services.create_order(c, aid, "黑")
                with lock:
                    results.append(("ok", i))
            except services.ServiceError:
                with lock:
                    results.append(("full", i))
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), QUOTA, "成功订单数必须等于名额，不能超卖")

        c = db.connect(tmp)
        held = c.execute(
            "SELECT COUNT(*) n FROM \"order\" WHERE activity_id=? AND status IN (%s)"
            % ",".join("?" * len(services.HOLDING_STATES)),
            (aid, *services.HOLDING_STATES),
        ).fetchone()["n"]
        self.assertEqual(held, QUOTA)
        with self.assertRaises(services.ServiceError):
            services.create_order(c, aid, "黑")
        c.close()


if __name__ == "__main__":
    unittest.main()
