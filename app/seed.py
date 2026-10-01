"""预置演示数据：围绕“当前时刻”构造一批临界样例。

时间轴（相对启动时刻 T）：
  活动A（主推）定金窗口早已开始；尾款窗口 T-20s 开启，尾款截止 T+90s，宽限 30s
    - 若干订单处于不同状态；含部分支付、含一条延迟 45s 才到的尾款回执、
      含一笔释放后才到的迟到款（另一个活动构造释放场景）
  活动B（次批）尾款早已截止：构造“逾期释放 + 迟到回执进异常款”
  活动C 已取消：构造取消后付款进异常款
"""
from __future__ import annotations

import sqlite3

from . import clock, payment_adapter, services

C = {
    # 金额，分
    "deposit": 5000,
    "balance": 15000,
}


def seed(conn: sqlite3.Connection) -> dict:
    t = clock.now()
    conn.execute("DELETE FROM service_request")
    conn.execute("DELETE FROM timeline_event")
    conn.execute("DELETE FROM exception_case")
    conn.execute("DELETE FROM payment")
    conn.execute("DELETE FROM payment_outbox")
    conn.execute('DELETE FROM "order"')
    conn.execute("DELETE FROM shipment_batch")
    conn.execute("DELETE FROM sku")
    conn.execute("DELETE FROM deposit_rule")
    conn.execute("DELETE FROM activity")

    # ---- 活动 A：主推，尾款窗口刚开始，临近截止 ----
    a = conn.execute(
        """INSERT INTO activity (slug,name,description,image,color,total_quota,status,created_at)
           VALUES ('aura-headphone','Aura 降噪耳机 预售','定金 50 元，尾款 150 元（规格加价另计）',
                   '/images/aura.svg', '#2563eb', 10, 'on_sale', ?)""",
        (clock.fmt(t),),
    ).lastrowid
    conn.execute(
        """INSERT INTO deposit_rule (activity_id,version,deposit_amount,balance_amount,
             deposit_open,deposit_close,balance_open,balance_close,release_grace_sec,created_at)
           VALUES (?,1,5000,15000,?,?,?,?,30,?)""",
        (a, clock.fmt(clock.add(t, -86400 * 7)), clock.fmt(clock.add(t, 86400)),
         clock.fmt(clock.add(t, -20)), clock.fmt(clock.add(t, 90)), clock.fmt(t)),
    )
    for spec, extra in (("曜石黑", 0), ("月光白", 1000)):
        conn.execute("INSERT INTO sku (activity_id,spec,extra_cents) VALUES (?,?,?)",
                     (a, spec, extra))
    conn.execute(
        """INSERT INTO shipment_batch (activity_id,label,ship_after,capacity,created_at)
           VALUES (?,?,?,20,?)""",
        (a, "首批 · 10 月 20 日", clock.fmt(clock.add(t, -1)), clock.fmt(t)),
    )

    def mk(activity, spec, status, **kw):
        rule = services.get_active_rule(conn, activity)
        sku = conn.execute("SELECT * FROM sku WHERE activity_id=? AND spec=?",
                           (activity, spec)).fetchone()
        balance = rule["balance_amount"] + sku["extra_cents"]
        fields = dict(
            code=services._code("PS"), activity_id=activity, sku_id=sku["id"],
            rule_version=rule["version"], deposit_amount=rule["deposit_amount"],
            balance_amount=balance, total_amount=rule["deposit_amount"] + balance,
            deposit_open=rule["deposit_open"], deposit_close=rule["deposit_close"],
            balance_open=rule["balance_open"], balance_close=rule["balance_close"],
            release_deadline=clock.fmt(clock.add(clock.parse(rule["balance_close"]),
                                                 rule["release_grace_sec"])),
            release_grace_sec=rule["release_grace_sec"], status=status,
            created_at=clock.fmt(clock.add(t, -3600)),
        )
        fields.update(kw)
        cols = ",".join(f'"{k}"' if k == "order" else k for k in fields)
        cur = conn.execute(
            f'INSERT INTO "order" ({",".join(fields)}) VALUES ({",".join("?"*len(fields))})',
            tuple(fields.values()),
        )
        return cur.lastrowid

    # o1: 尾款窗口中，尚未付尾款 —— 尾款页主样例（90s 后截止）
    o1 = mk(a, "曜石黑", services.AWAITING_BALANCE,
            deposit_paid_at=clock.fmt(clock.add(t, -3000)))
    services._event(conn, o1, "customer", "order_created", "下单，锁定规则 v1", clock.add(t, -3600))
    services._event(conn, o1, "customer", "deposit_paid", "定金 50.00 元已付", clock.add(t, -3000))
    services._event(conn, o1, "system", "balance_window_open",
                    "尾款期开始，应付尾款 150.00 元", clock.add(t, -20))

    # o2: 部分支付尾款 8000/16000（月光白含加价）
    o2 = mk(a, "月光白", services.BALANCE_PARTIAL,
            deposit_paid_at=clock.fmt(clock.add(t, -2900)))
    conn.execute(
        """INSERT INTO payment (order_id,stage,receipt_no,amount,paid,status,raw_arrived_at,applied_at)
           VALUES (?, 'deposit', ?,5000,5000,'applied',?,?)""",
        (o2, "R-DEP-" + str(o2), clock.fmt(clock.add(t, -2900)), clock.fmt(clock.add(t, -2900))),
    )
    conn.execute(
        """INSERT INTO payment (order_id,stage,receipt_no,amount,paid,status,raw_arrived_at,applied_at)
           VALUES (?, 'balance', ?,8000,8000,'applied',?,?)""",
        (o2, "R-BAL-P1-" + str(o2), clock.fmt(clock.add(t, -10)), clock.fmt(clock.add(t, -10))),
    )
    services._event(conn, o2, "customer", "balance_partial",
                    "部分支付 80.00 元，已付 80.00/160.00，剩余 80.00 元", clock.add(t, -10))
    # o2 的剩余尾款：延迟 45 秒到达的回执（临界样例：在截止前能否到账）
    payment_adapter.charge(conn, conn.execute(
        'SELECT code FROM "order" WHERE id=?', (o2,)).fetchone()["code"],
        "balance", 8000, delay_sec=45, at=t)

    # o3: 已付清待配货
    o3 = mk(a, "曜石黑", services.PAID,
            deposit_paid_at=clock.fmt(clock.add(t, -2800)),
            paid_off_at=clock.fmt(clock.add(t, -5)))
    for stage, rn, amt, ts in (
        ("deposit", f"R-DEP-{o3}", 5000, -2800), ("balance", f"R-BAL-{o3}", 15000, -5)):
        conn.execute(
            """INSERT INTO payment (order_id,stage,receipt_no,amount,paid,status,raw_arrived_at,applied_at)
               VALUES (?,?,?,?,?,'applied',?,?)""",
            (o3, stage, rn, amt, amt, clock.fmt(clock.add(t, ts)), clock.fmt(clock.add(t, ts))),
        )
    services._event(conn, o3, "customer", "balance_paid", "尾款付清", clock.add(t, -5))

    # o4: 刚下单待付定金
    o4 = mk(a, "月光白", services.AWAITING_DEPOSIT)
    services._event(conn, o4, "customer", "order_created", "下单占用名额", t)

    # ---- 活动 B：尾款早已截止，构造释放 + 释放后迟到款 ----
    b = conn.execute(
        """INSERT INTO activity (slug,name,description,image,color,total_quota,status,created_at)
           VALUES ('breeze-fan','Breeze 循环扇 预售','尾款已逾期，演示名额释放与迟到回执',
                   '/images/breeze.svg', '#0d9488', 6, 'on_sale', ?)""",
        (clock.fmt(t),),
    ).lastrowid
    conn.execute(
        """INSERT INTO deposit_rule (activity_id,version,deposit_amount,balance_amount,
             deposit_open,deposit_close,balance_open,balance_close,release_grace_sec,created_at)
           VALUES (?,1,3000,9000,?,?,?,?,0,?)""",
        (b, clock.fmt(clock.add(t, -86400 * 10)), clock.fmt(clock.add(t, -7200)),
         clock.fmt(clock.add(t, -7200)), clock.fmt(clock.add(t, -60)), clock.fmt(t)),
    )
    conn.execute("INSERT INTO sku (activity_id,spec,extra_cents) VALUES (?,'标准白',0)", (b,))
    # b1: 已逾期释放
    b1 = mk(b, "标准白", services.RELEASED,
            deposit_paid_at=clock.fmt(clock.add(t, -7000)),
            released_at=clock.fmt(clock.add(t, -30)))
    conn.execute(
        """INSERT INTO payment (order_id,stage,receipt_no,amount,paid,status,raw_arrived_at,applied_at)
           VALUES (?, 'deposit', ?,3000,3000,'applied',?,?)""",
        (b1, f"R-DEP-{b1}", clock.fmt(clock.add(t, -7000)), clock.fmt(clock.add(t, -7000))),
    )
    services._event(conn, b1, "system", "released", "逾期未付尾款，名额释放", clock.add(t, -30))
    # b1 释放后迟到的尾款回执：延迟 20 秒到达 => 应进异常款，绝不发货
    payment_adapter.charge(conn, conn.execute(
        'SELECT code FROM "order" WHERE id=?', (b1,)).fetchone()["code"],
        "balance", 9000, delay_sec=20, at=t)

    # b2: 部分支付后逾期释放（释放时部分款进异常款待退款）
    b2 = mk(b, "标准白", services.DEPOSIT_PAID,
            deposit_paid_at=clock.fmt(clock.add(t, -6900)))
    conn.execute(
        """INSERT INTO payment (order_id,stage,receipt_no,amount,paid,status,raw_arrived_at,applied_at)
           VALUES (?, 'deposit', ?,3000,3000,'applied',?,?)""",
        (b2, f"R-DEP-{b2}", clock.fmt(clock.add(t, -6900)), clock.fmt(clock.add(t, -6900))),
    )
    services.enter_balance_window(conn, t)
    services.apply_payment(conn, conn.execute(
        'SELECT code FROM "order" WHERE id=?', (b2,)).fetchone()["code"],
        "balance", 4000, f"R-BAL-P-{b2}", at=clock.add(t, -40))
    services.release_overdue(conn, t)  # b2 截止已过 -> 释放且 4000 部分款进异常

    # ---- 活动 C：已取消，付款进入异常款 ----
    c = conn.execute(
        """INSERT INTO activity (slug,name,description,image,color,total_quota,status,created_at)
           VALUES ('comet-lamp','Comet 氛围灯 预售（活动已取消）','演示取消预售与取消后付款',
                   '/images/comet.svg', '#dc2626', 8, 'on_sale', ?)""",
        (clock.fmt(t),),
    ).lastrowid
    conn.execute(
        """INSERT INTO deposit_rule (activity_id,version,deposit_amount,balance_amount,
             deposit_open,deposit_close,balance_open,balance_close,release_grace_sec,created_at)
           VALUES (?,1,2000,8000,?,?,?,?,0,?)""",
        (c, clock.fmt(clock.add(t, -86400 * 3)), clock.fmt(clock.add(t, 86400)),
         clock.fmt(clock.add(t, 86400)), clock.fmt(clock.add(t, 86400 * 2)), clock.fmt(t)),
    )
    conn.execute("INSERT INTO sku (activity_id,spec,extra_cents) VALUES (?,'暖光',0)", (c,))
    c1 = mk(c, "暖光", services.DEPOSIT_PAID,
            deposit_paid_at=clock.fmt(clock.add(t, -5000)))
    conn.execute(
        """INSERT INTO payment (order_id,stage,receipt_no,amount,paid,status,raw_arrived_at,applied_at)
           VALUES (?, 'deposit', ?,2000,2000,'applied',?,?)""",
        (c1, f"R-DEP-{c1}", clock.fmt(clock.add(t, -5000)), clock.fmt(clock.add(t, -5000))),
    )
    services.cancel_activity(conn, c, clock.add(t, -100))
    # 取消后迟到的尾款（延迟 10 秒）=> 异常款
    payment_adapter.charge(conn, conn.execute(
        'SELECT code FROM "order" WHERE id=?', (c1,)).fetchone()["code"],
        "balance", 8000, delay_sec=10, at=t)

    return {"a": a, "b": b, "c": c, "o1": o1, "o2": o2, "o3": o3, "o4": o4,
            "b1": b1, "b2": b2, "c1": c1}
