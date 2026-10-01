"""测试共用：内存库 + 一个简单预售活动。"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clock, db, services  # noqa: E402


class FixedClock:
    """测试用固定时钟。"""

    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        return self.t


def make_activity(conn, quota=3, deposit=5000, balance=15000,
                  deposit_open=-3600, deposit_close=86400,
                  balance_open=100, balance_close=200, grace=30,
                  specs=(("黑", 0),)):
    t = clock.now()
    a = conn.execute(
        """INSERT INTO activity (slug,name,description,image,color,total_quota,status,created_at)
           VALUES ('t','测试','','/x.svg','#2563eb',?,'on_sale',?)""",
        (quota, clock.fmt(t)),
    ).lastrowid
    conn.execute(
        """INSERT INTO deposit_rule (activity_id,version,deposit_amount,balance_amount,
             deposit_open,deposit_close,balance_open,balance_close,release_grace_sec,created_at)
           VALUES (?,1,?,?,?,?,?,?,?,?)""",
        (a, deposit, balance,
         clock.fmt(clock.add(t, deposit_open)), clock.fmt(clock.add(t, deposit_close)),
         clock.fmt(clock.add(t, balance_open)), clock.fmt(clock.add(t, balance_close)),
         grace, clock.fmt(t)),
    )
    for spec, extra in specs:
        conn.execute("INSERT INTO sku (activity_id,spec,extra_cents) VALUES (?,?,?)",
                     (a, spec, extra))
    conn.execute(
        "INSERT INTO shipment_batch (activity_id,label,ship_after,capacity,created_at) VALUES (?,?,?,?,?)",
        (a, "首批", clock.fmt(clock.add(t, 1000)), None, clock.fmt(t)),
    )
    return a


def fresh():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


def pay(conn, order, stage, amount, rn, at):
    return services.apply_payment(conn, order["code"], stage, amount, rn, at=at)


def get_order(conn, code):
    return conn.execute('SELECT * FROM "order" WHERE code=?', (code,)).fetchone()
