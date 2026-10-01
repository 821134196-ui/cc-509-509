"""预置临界时刻样例。

启动一条命令后即可看到：尾款窗口进行中/未开始/已截止、部分支付、
在途延迟回执、释放后迟到付款、重复/超额到账、规则版本锁定、规格变更、
取消预售退款、异常款队列与名额回补。
"""
from __future__ import annotations

import os
from datetime import timedelta

from .db import connect, Clock
from .service import PresaleService


def _add_activity(svc, *, sku, name, color, image, stock, dep, bal, spec,
                   dep_open, dep_close, bal_open, bal_close, note, t0, version=1):
    now = t0.isoformat()
    cur = svc.conn.execute(
        """INSERT INTO activities(sku,name,color,image,stock,
           deposit_open_at,deposit_close_at,balance_open_at,balance_close_at,
           current_version,status)
           VALUES(?,?,?,?,?,?,?,?,?,?, 'active')""",
        (sku, name, color, image, stock,
         (t0 + dep_open).isoformat(), (t0 + dep_close).isoformat(),
         (t0 + bal_open).isoformat(), (t0 + bal_close).isoformat(), version),
    )
    aid = cur.lastrowid
    svc.conn.execute(
        """INSERT INTO rule_versions(activity_id,version,deposit_cents,balance_cents,
           spec,created_at,note) VALUES(?,?,?,?,?,?,?)""",
        (aid, 1, dep, bal, spec, now, note),
    )
    svc.conn.commit()
    return aid


def seed(db_path: str) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    conn = connect(db_path)
    clock = Clock(conn)
    svc = PresaleService(conn, clock)
    t0 = clock.now().replace(microsecond=0)

    # ---------- AURORA：尾款窗口 3 分钟后开始，8 分钟后截止 ----------
    aurora = _add_activity(
        svc, sku="AURORA-BT", name="极光降噪耳机", color="#2563eb",
        image="aurora.svg", stock=5, dep=10000, bal=50000,
        spec="曜石黑 / 标准版",
        dep_open=-timedelta(days=2), dep_close=timedelta(days=2),
        bal_open=timedelta(minutes=3), bal_close=timedelta(minutes=8),
        note="首发规则", t0=t0)
    o1 = svc.create_order(aurora)                       # 锁 v1，待付尾款
    o2 = svc.create_order(aurora)                       # 锁 v1
    # 尾款窗 3 分钟后才开：拨到开窗后完成一笔部分支付，再拨回现在
    clock.advance(210)
    svc.send_balance_receipt(o2, 20000)                 # 部分支付 ¥200/¥500
    svc.deliver_pending()
    clock.reset_to(t0)

    # 发布 v2：调价 + 规格变更，只影响之后的新订单
    svc.publish_version(aurora, 12000, 56000, "云雾白 / 标准版",
                        "原料调价并切换配色")
    o4 = svc.create_order(aurora)                       # 锁 v2
    # o1 的尾款回执延迟 5 分钟到达（开窗后、截止前，届时自动结清）
    svc.send_balance_receipt(o1, 50000, delay_seconds=300)
    # o5 的回执延迟 12 分钟到达——晚于截止(8 分钟)，演示“释放后迟到付款”
    o5 = svc.create_order(aurora)
    svc.send_balance_receipt(o5, 50000, delay_seconds=720)

    # ---------- COMET：尾款期明天才开始，尾款台显示“未开始” ----------
    comet = _add_activity(
        svc, sku="COMET-W", name="彗星轻智能手表", color="#0f766e",
        image="comet.svg", stock=2, dep=20000, bal=80000,
        spec="砂岩银 / 42mm",
        dep_open=-timedelta(days=2), dep_close=timedelta(days=2),
        bal_open=timedelta(days=1), bal_close=timedelta(days=3),
        note="首批", t0=t0)
    svc.create_order(comet)

    # ---------- PULSE：窗口进行中；重复到账 + 取消预售 ----------
    pulse = _add_activity(
        svc, sku="PULSE-BAND", name="脉冲运动手环", color="#be123c",
        image="pulse.svg", stock=4, dep=5000, bal=30000,
        spec="熔岩红 / S/M",
        dep_open=-timedelta(days=3), dep_close=timedelta(days=2),
        bal_open=-timedelta(days=1), bal_close=timedelta(days=1),
        note="常规批次", t0=t0)
    p1 = svc.create_order(pulse)
    p2 = svc.create_order(pulse)
    svc.send_balance_receipt(p2, 30000)                 # 正常结清 -> 配货
    svc.deliver_pending()
    dup_pending = svc.send_balance_receipt(p2, 30000)   # 买家/渠道又扣了一笔
    # send_balance_receipt 返回 receipt_no；找到其 pending 行
    row = conn.execute(
        "SELECT id FROM pending_receipts WHERE receipt_no=? ORDER BY id DESC LIMIT 1",
        (dup_pending,)).fetchone()
    svc.deliver_pending()                               # -> overpayment 异常款
    svc.send_duplicate(row["id"])                       # 同号回执再推一次 -> 忽略
    # 取消整个预售：p1 取消退款，p2 已付清继续履约
    svc.cancel_activity(pulse, "厂商排产延期，活动终止", "运营(种子)")

    # ---------- NEBULA：尾款窗口已截止，释放 + 迟到付款 + 部分支付逾期 ----------
    # 下单时段也设在过去：把时钟拨回 30 分钟完成下单与部分支付
    nebula = _add_activity(
        svc, sku="NEBULA-SPK", name="星云桌面音箱", color="#7c3aed",
        image="nebula.svg", stock=3, dep=15000, bal=60000,
        spec="午夜紫 / 单机",
        dep_open=-timedelta(days=2), dep_close=-timedelta(minutes=20),
        bal_open=-timedelta(minutes=15), bal_close=-timedelta(minutes=2),
        note="首发", t0=t0)
    clock.reset_to(t0 - timedelta(minutes=30))
    n1 = svc.create_order(nebula)
    n2 = svc.create_order(nebula)
    # 拨到尾款窗口内（开窗 -15 分、截止 -2 分）：n2 只付一部分
    clock.reset_to(t0 - timedelta(minutes=10))
    svc.send_balance_receipt(n2, 10000)                 # 只付了 ¥100
    svc.deliver_pending()
    # n1 在窗口内付了全款，但渠道回执要到截止后才送到（支付/释放竞争样例）
    paid_in_window = (t0 - timedelta(minutes=5)).isoformat()
    arrive_after_close = (t0 + timedelta(minutes=1)).isoformat()
    svc.queue_receipt(n1, "balance", 60000, "B-LATE-NEB-01",
                      paid_in_window, arrive_after_close)
    # 拨回现在：先扫描释放（n1、n2；n2 挂 partial_timeout），
    # 等 +1 分钟回执到达时订单已释放 -> late_payment 异常款
    clock.reset_to(t0)
    svc.sweep_expired()

    clock.reset_to(t0)
    conn.commit()
    conn.close()
    print(f"种子完成：{db_path}（虚拟时间基准 {t0.isoformat()}）")
    print("临界样例：")
    print(" - 极光降噪耳机：尾款窗口 3 分钟后开始；1 张回执 5 分钟后到（窗口内），"
          "1 张 12 分钟后到（与释放竞争）；另有部分支付订单")
    print(" - 彗星手表：尾款期未开始，尾款台不显示金额")
    print(" - 脉冲手环：已取消（待付订单退款、已付订单继续履约），含重复到账异常款")
    print(" - 星云音箱：已截止并释放；含释放后迟到付款、部分支付逾期两笔异常款")
