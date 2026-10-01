"""HTML 渲染：白底、商品色点缀、黑色金额，无渐变。"""
from __future__ import annotations

import html
from datetime import timezone

from . import clock, queries
from .services import (AWAITING_BALANCE, AWAITING_DEPOSIT, BALANCE_PARTIAL, PAID,
                       ALLOCATED, SHIPPED, RELEASED, REFUNDING, REFUNDED, CANCELLED,
                       DEPOSIT_PAID)

ROLE_CN = {"customer": "买家", "finance": "财务", "customer_service": "客服"}


def yuan(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def bt(s: str | None) -> str:
    if not s:
        return "—"
    dt = clock.parse(s).astimezone()
    return dt.strftime("%m-%d %H:%M:%S")


CSS = """
:root{--ink:#111827;--sub:#6b7280;--line:#e5e7eb;--bg:#ffffff;--soft:#f9fafb;
  --reserved:#b45309;--paid:#15803d;--released:#6b7280;--danger:#b91c1c;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.55}
a{color:inherit;text-decoration:none}
.wrap{max-width:1040px;margin:0 auto;padding:0 20px 80px}
header.top{border-bottom:1px solid var(--line);background:#fff;position:sticky;top:0;z-index:5}
.top .wrap{display:flex;align-items:center;gap:16px;padding-top:12px;padding-bottom:12px}
.brand{font-weight:800;font-size:17px}
.brand small{color:var(--sub);font-weight:400;margin-left:8px}
.spacer{flex:1}
.rolebar{display:flex;gap:6px;align-items:center;color:var(--sub);font-size:13px}
.rolebar a{padding:4px 10px;border:1px solid var(--line);border-radius:999px}
.rolebar a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
h1{font-size:22px;margin:26px 0 14px}
h2{font-size:17px;margin:26px 0 10px}
.card{border:1px solid var(--line);border-radius:12px;padding:18px;margin:12px 0;background:#fff}
.card.soft{background:var(--soft)}
.grid{display:grid;gap:14px}
.g3{grid-template-columns:repeat(3,1fr)}
.g2{grid-template-columns:1.3fr 1fr}
@media(max-width:820px){.g3,.g2{grid-template-columns:1fr}}
.act{display:flex;gap:16px;border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
.act img{width:180px;height:112px;object-fit:cover;border-radius:8px;border:1px solid var(--line)}
.act h3{margin:0 0 4px;font-size:17px}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--sub)}
.tag.live{color:#15803d;border-color:#bbf7d0;background:#f0fdf4}
.tag.off{color:#b91c1c;border-color:#fecaca;background:#fef2f2}
.amount{font-variant-numeric:tabular-nums;font-weight:800;color:#000}
.amount.big{font-size:30px}
.muted{color:var(--sub)}
.row{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.btn{display:inline-block;border:1px solid var(--ink);background:var(--ink);color:#fff;
  padding:8px 16px;border-radius:8px;font-size:14px;cursor:pointer}
.btn.ghost{background:#fff;color:var(--ink)}
.btn.danger{border-color:var(--danger);background:var(--danger)}
.btn:disabled{opacity:.45}
input,select{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:14px;background:#fff}
label{font-size:13px;color:var(--sub);display:block;margin:8px 0 3px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--sub);font-weight:600;font-size:12px}
.status{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.s-awaiting_deposit,.s-deposit_paid,.s-awaiting_balance,.s-balance_partial{color:#b45309;border-color:#fde68a;background:#fffbeb}
.s-paid,.s-allocated,.s-shipped{color:#15803d;border-color:#bbf7d0;background:#f0fdf4}
.s-released,.s-refunded,.s-cancelled{color:#6b7280;border-color:#e5e7eb;background:#f9fafb}
.s-refunding{color:#b91c1c;border-color:#fecaca;background:#fef2f2}
/* 时间轴 */
.tl{list-style:none;margin:12px 0 0;padding:0 0 0 22px;border-left:2px solid var(--line)}
.tl li{position:relative;padding:0 0 16px 14px}
.tl li::before{content:"";position:absolute;left:-28px;top:4px;width:10px;height:10px;
  border-radius:50%;background:#fff;border:2px solid #9ca3af}
.tl li.sys::before{border-color:#2563eb}
.tl li.fin::before{border-color:#b91c1c}
.tl .when{color:var(--sub);font-size:12px;margin-right:8px;font-variant-numeric:tabular-nums}
/* 付款窗口条 */
.winbar{position:relative;height:10px;background:#f3f4f6;border-radius:999px;margin:26px 0 6px}
.winbar .seg{position:absolute;top:0;bottom:0;border-radius:999px}
.winmarks{display:flex;justify-content:space-between;color:var(--sub);font-size:12px}
.cursor{position:absolute;top:-5px;width:2px;height:20px;background:#000}
/* 库存看板 */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:10px 0}
@media(max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{border:1px solid var(--line);border-radius:10px;padding:12px}
.kpi .n{font-size:26px;font-weight:800;color:#000;font-variant-numeric:tabular-nums}
.kpi .k{font-size:12px;color:var(--sub);margin-top:2px}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:middle}
.stacked{display:flex;height:26px;gap:2px;border-radius:6px;overflow:hidden;border:1px solid var(--line);background:#fff;margin:8px 0}
.stacked div{display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:700;min-width:0}
.legend{display:flex;gap:16px;font-size:13px;color:var(--sub);margin-top:6px}
.flash{padding:10px 14px;border-radius:8px;margin:12px 0;border:1px solid}
.flash.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
.flash.err{background:#fef2f2;border-color:#fecaca;color:#991b1b}
.small{font-size:12px}
.right{text-align:right}
"""


def layout(role: str, body: str, flash: tuple | None = None) -> str:
    f = ""
    if flash:
        kind, msg = flash
        f = f'<div class="flash {kind}">{esc(msg)}</div>'
    roles = "".join(
        f'<a class="on" href="/role/{r}">{ROLE_CN[r]}</a>' if r == role
        else f'<a href="/role/{r}">{ROLE_CN[r]}</a>'
        for r in ("customer", "customer_service", "finance"))
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>预售系统</title><style>{CSS}</style></head><body>
<header class="top"><div class="wrap">
  <a class="brand" href="/">预售管理台 <small>定金 · 尾款 · 批次</small></a>
  <span class="spacer"></span>
  <span class="rolebar">视角 {roles}</span>
</div></header><div class="wrap">{f}{body}</div></body></html>"""


def status_badge(st: str) -> str:
    return f'<span class="status s-{st}">{queries.STATUS_CN.get(st, st)}</span>'


def home(conn, role: str, activities: list, flash=None) -> str:
    cards = []
    for a in activities:
        inv = queries.inventory(conn, a["id"])
        live = a["status"] == "on_sale"
        tag = '<span class="tag live">销售中</span>' if live else '<span class="tag off">已取消</span>'
        cards.append(f"""
        <a class="act" href="/activity/{a['id']}">
          <img src="{esc(a['image'])}" alt="">
          <div style="flex:1">
            <h3>{esc(a['name'])} {tag}</h3>
            <div class="muted small">{esc(a['description'])}</div>
            <div class="row" style="margin-top:8px">
              <span class="small muted">库存：预留 {inv['reserved']} · 已付 {inv['paid']} · 释放 {inv['released']}（可售 {inv['available']}）</span>
            </div>
          </div>
        </a>""")
    return layout(role, f"""
      <h1>预售活动</h1>
      <p class="muted">下单即占用名额并锁定定金规则版本；尾款期开始后显示应付金额与截止时间；逾期名额自动释放。</p>
      {''.join(cards)}
      <p class="small" style="margin-top:20px"><a href="/exceptions">→ 异常款队列</a>　|　当前视角：{ROLE_CN[role]}</p>
    """, flash)


def _window_bar(order, nowdt) -> str:
    """定金窗口—尾款窗口的时间轴，标出当前位置。"""
    d0 = clock.parse(order["deposit_open"]).timestamp()
    d1 = clock.parse(order["deposit_close"]).timestamp()
    b0 = clock.parse(order["balance_open"]).timestamp()
    b1 = clock.parse(order["release_deadline"]).timestamp()
    start, end = min(d0, b0), max(d1, b1)
    span = max(end - start, 1)
    def pct(x): return max(0, min(100, (x - start) / span * 100))
    cursor = pct(nowdt.timestamp())
    return f"""
    <div class="winbar">
      <div class="seg" style="left:{pct(d0)}%;width:{max(0,pct(d1)-pct(d0))}%;background:#fcd34d"></div>
      <div class="seg" style="left:{pct(b0)}%;width:{max(0,pct(b1)-pct(b0))}%;background:#6ee7b7"></div>
      <div class="cursor" style="left:{cursor}%" title="当前时刻"></div>
    </div>
    <div class="winmarks">
      <span>定金开始 {bt(order['deposit_open'])}</span>
      <span>尾款开始 {bt(order['balance_open'])}</span>
      <span style="color:#b91c1c">尾款截止(含宽限) {bt(order['release_deadline'])}</span>
    </div>"""


def order_page(conn, role: str, order, payments, nowdt, flash=None) -> str:
    bal_paid = queries.balance_paid(conn, order["id"])
    dep_paid = queries.deposit_paid_amount(conn, order["id"])
    total_paid = bal_paid + dep_paid
    remain = max(0, order["balance_amount"] - bal_paid)
    events = conn.execute(
        "SELECT * FROM timeline_event WHERE order_id=? ORDER BY id", (order["id"],)
    ).fetchall()
    tl = "".join(
        f'<li class="{ "sys" if e["actor"]=="system" else "fin" if e["actor"]=="finance" else ""}">'
        f'<span class="when">{bt(e["at"])}</span>'
        f'<span class="tag">{esc({"customer":"买家","system":"系统","finance":"财务","customer_service":"客服"}[e["actor"]])}</span> '
        f'{esc(e["detail"])}</li>'
        for e in events)

    in_balance = order["status"] in (DEPOSIT_PAID, AWAITING_BALANCE, BALANCE_PARTIAL) and \
        clock.parse(order["balance_open"]) <= nowdt <= clock.parse(order["release_deadline"])

    # 金额三联：定金 / 尾款 / 已付合计
    amounts = f"""
    <div class="grid g3">
      <div class="card soft"><div class="muted small">定金</div>
        <div class="amount">{yuan(order['deposit_amount'])}</div>
        <div class="small {'muted' if dep_paid==0 else ''}">已付 {yuan(dep_paid)}</div></div>
      <div class="card soft"><div class="muted small">尾款{('（规格加价已含）')}</div>
        <div class="amount">{yuan(order['balance_amount'])}</div>
        <div class="small">已付 {yuan(bal_paid)} · 剩余 <b class="amount">{yuan(remain)}</b></div></div>
      <div class="card soft"><div class="muted small">已付合计</div>
        <div class="amount big">{yuan(total_paid)}</div>
        <div class="small muted">订单合计 {yuan(order['total_amount'])}</div></div>
    </div>"""

    # 尾款支付卡
    paycard = ""
    if role == "customer":
        if order["status"] == AWAITING_DEPOSIT:
            paycard = f"""
            <h2>支付定金</h2>
            <form class="card" method="post" action="/pay/charge">
              <input type="hidden" name="code" value="{esc(order['code'])}">
              <input type="hidden" name="stage" value="deposit">
              <div class="row">
                <div><label>支付金额（分）</label>
                  <input name="amount" value="{order['deposit_amount']}"></div>
                <div><label>回执延迟（秒，模拟慢回调）</label>
                  <input name="delay" value="0"></div>
                <button class="btn" style="margin-top:22px">发起付款</button></div>
            </form>"""
        elif in_balance:
            warn = ('<p class="small" style="color:#b45309">尾款可部分支付；'
                    '截止前未付清将释放名额，已付部分进入异常款审核。</p>')
            paycard = f"""
            <h2>尾款支付</h2>
            <div class="card" style="border-color:#fcd34d">
              <div class="row"><div>
                <div class="muted small">应付尾款</div>
                <div class="amount big">{yuan(remain)}</div></div>
                <div class="right"><div class="muted small">付款截止</div>
                  <div class="amount" style="color:#b91c1c">{bt(order['release_deadline'])}</div></div></div>
              {warn}
              <form method="post" action="/pay/charge">
                <input type="hidden" name="code" value="{esc(order['code'])}">
                <input type="hidden" name="stage" value="balance">
                <div class="row">
                  <div><label>本次支付（分）</label><input name="amount" value="{remain}"></div>
                  <div><label>回执延迟（秒）</label><input name="delay" value="0"></div>
                  <button class="btn" style="margin-top:22px">支付尾款</button></div>
              </form>
            </div>"""

    # 客服处理请求
    svc = ""
    if role == "customer_service":
        svc = f"""
        <h2>客服处理（仅可发起请求）</h2>
        <form class="card" method="post" action="/orders/{order['id']}/service-request">
          <div class="row">
            <div style="flex:1"><label>请求类型</label>
              <select name="type"><option value="investigate">核查订单</option>
              <option value="refund">请求退款</option><option value="cancel">请求取消</option></select></div>
            <div style="flex:2"><label>原因</label><input name="reason" style="width:100%"></div>
            <button class="btn ghost" style="margin-top:22px">提交请求</button></div>
          <p class="small muted">客服无权直接改账或退款，请求提交后由财务处理。</p>
        </form>"""

    receipt_rows = "".join(
        f"""<tr><td>{esc(p['receipt_no'])}</td><td>{'定金' if p['stage']=='deposit' else '尾款'}</td>
        <td class="right amount">{yuan(p['amount'])}</td><td>{esc(p['status'])}</td>
        <td>{bt(p['raw_arrived_at'])}</td><td>{bt(p['applied_at'])}</td>
        <td>{('<form method="post" action="/pay/replay"><input type="hidden" name="receipt" value="'+esc(p['receipt_no'])+'"><button class="btn ghost small">重放(测重复)</button></form>') if role=='customer' and p['status']=='applied' else ''}</td>
        </tr>""" for p in payments)

    body = f"""
    <p><a href="/activity/{order['activity_id']}">← 返回活动</a></p>
    <h1>订单 {esc(order['code'])} {status_badge(order['status'])}</h1>
    <p class="muted">{esc(order['activity_name'])} · 规格 {esc(order['spec'])} ·
       下单锁定规则 <b>v{order['rule_version']}</b>
       {'· 批次 ' + esc(order['batch_label']) if order['batch_label'] else ''}</p>
    {amounts}
    <h2>付款窗口时间轴</h2>
    {_window_bar(order, nowdt)}
    {paycard}{svc}
    <h2>订单时间线</h2>
    <ul class="tl">{tl}</ul>
    <h2>支付回执（{len(payments)}）</h2>
    <table><thead><tr><th>回执号</th><th>阶段</th><th class="right">金额</th><th>状态</th>
      <th>到达</th><th>入账</th><th></th></tr></thead>
      <tbody>{receipt_rows or '<tr><td colspan=7 class="muted">暂无回执</td></tr>'}</tbody></table>
    """
    return layout(role, body, flash)


def activity_page(conn, role, act, orders, inv, rules, batches, nowdt, flash=None) -> str:
    total = max(inv["total"], 1)
    def seg(value, color, label):
        w = value / total * 100
        if w <= 0:
            return ""
        return (f'<div style="background:{color};width:{w:.2f}%" title="{label} {value}">'
                f'{("✕" if w < 5 else value)}</div>')
    bar = f"""
    <div class="stacked">
      {seg(inv['reserved'], 'var(--reserved)', '预留')}
      {seg(inv['paid'], 'var(--paid)', '已付')}
      {seg(inv['refunding'], 'var(--danger)', '退款中')}
      {seg(inv['available'], '#e5e7eb', '可售')}
    </div>
    <div class="legend">
      <span><i class="dot" style="background:var(--reserved)"></i>预留 {inv['reserved']}</span>
      <span><i class="dot" style="background:var(--paid)"></i>已付 {inv['paid']}</span>
      <span><i class="dot" style="background:var(--danger)"></i>退款中 {inv['refunding']}</span>
      <span><i class="dot" style="background:#e5e7eb"></i>可售 {inv['available']}</span>
      <span><i class="dot" style="background:#9ca3af"></i>累计释放/取消 {inv['released']}</span>
    </div>"""

    kpis = f"""
    <div class="kpis">
      <div class="kpi"><div class="n">{inv['total']}</div><div class="k">预售名额</div></div>
      <div class="kpi"><div class="n" style="color:var(--reserved)">{inv['reserved']}</div><div class="k">预留（占名额未付清）</div></div>
      <div class="kpi"><div class="n" style="color:var(--paid)">{inv['paid']}</div><div class="k">已付（含配货/发货）</div></div>
      <div class="kpi"><div class="n" style="color:#6b7280">{inv['released']}</div><div class="k">释放/取消/退款</div></div>
    </div>{bar}"""

    # 下单表单（买家）
    skus = conn.execute("SELECT * FROM sku WHERE activity_id=? ORDER BY id", (act["id"],)).fetchall()
    order_form = ""
    if role == "customer" and act["status"] == "on_sale":
        opts = "".join(
            f'<option value="{esc(s["spec"])}">{esc(s["spec"])}'
            f'{("（+"+yuan(s["extra_cents"])+"）") if s["extra_cents"] else ""}</option>'
            for s in skus if not s["spec"].startswith("[已下架]"))
        order_form = f"""
        <form class="card soft" method="post" action="/activity/{act['id']}/orders">
          <div class="row">
            <div><label>规格</label><select name="spec">{opts}</select></div>
            <button class="btn" style="margin-top:22px">下单占用名额</button></div>
          <p class="small muted">下单时锁定当前定金规则版本，之后规则或规格变更不影响本单。</p>
        </form>"""

    # 规则版本（财务可发布新版本；所有人可看）
    rule_rows = "".join(
        f"""<tr><td>v{r['version']}</td><td class="amount">{yuan(r['deposit_amount'])}</td>
        <td class="amount">{yuan(r['balance_amount'])}</td><td>{bt(r['balance_open'])}</td>
        <td>{bt(r['balance_close'])}</td><td>{r['release_grace_sec']} 秒</td></tr>"""
        for r in rules)
    rules_box = f"""
    <h2>定金规则版本</h2>
    <table><thead><tr><th>版本</th><th>定金</th><th>尾款</th><th>尾款开始</th><th>尾款截止</th><th>释放宽限</th></tr></thead>
      <tbody>{rule_rows}</tbody></table>"""
    if role == "finance" and act["status"] == "on_sale":
        latest = rules[0]
        rules_box += f"""
        <details class="card soft"><summary class="muted small">财务：发布新规则版本（仅影响之后下单）</summary>
        <form method="post" action="/activity/{act['id']}/rules">
          <div class="grid g3">
            <div><label>定金（分）</label><input name="deposit" value="{latest['deposit_amount']}"></div>
            <div><label>尾款（分）</label><input name="balance" value="{latest['balance_amount']}"></div>
            <div><label>释放宽限（秒）</label><input name="grace" value="{latest['release_grace_sec']}"></div>
          </div>
          <button class="btn ghost" style="margin-top:10px">发布新版本</button>
        </form></details>"""
    if role == "finance" and act["status"] == "on_sale":
        rules_box += f"""
        <form method="post" action="/activity/{act['id']}/cancel"
              onsubmit="return confirm('取消活动？未付清订单将取消并退款审核，已付清订单继续配货。')">
          <button class="btn danger" style="margin-top:8px">取消该预售活动</button></form>
        <details class="card soft"><summary class="muted small">财务：变更商品规格（仅影响之后下单，老订单保持快照）</summary>
        <form method="post" action="/activity/{act['id']}/specs">
          <label>每行一个“规格:加价(分)”，省略某行即下架该规格</label>
          <textarea name="specs" rows="4" style="width:100%;padding:8px;border:1px solid var(--line);border-radius:8px">{''.join(esc(s['spec']) + ':' + str(s['extra_cents']) + chr(10) for s in skus if not s['spec'].startswith('[已下架]'))}</textarea>
          <button class="btn ghost" style="margin-top:8px">应用规格变更</button>
        </form></details>"""

    # 订单表
    def order_row(o):
        bal_paid = queries.balance_paid(conn, o["id"])
        dep_paid = queries.deposit_paid_amount(conn, o["id"])
        return f"""<tr>
          <td><a href="/orders/{o['code']}"><b>{esc(o['code'])}</b></a></td>
          <td>{esc(o['spec'])}</td><td>v{o['rule_version']}</td>
          <td class="right amount">{yuan(dep_paid)}</td>
          <td class="right amount">{yuan(bal_paid)}</td>
          <td class="right amount">{yuan(dep_paid + bal_paid)}</td>
          <td>{status_badge(o['status'])}</td>
          <td class="small muted">{bt(o['created_at'])}</td></tr>"""
    rows = "".join(order_row(o) for o in orders)

    # 批次 & 财务配货
    batch_box = ""
    if batches:
        b_rows = "".join(
            f"""<tr><td>{esc(b['label'])}</td><td>{bt(b['ship_after'])}</td>
            <td>{b['capacity'] if b['capacity'] is not None else '不限'}</td>
            <td>{conn.execute('SELECT COUNT(*) c FROM "order" WHERE batch_id=?',(b['id'],)).fetchone()['c']}</td>
            <td>{('<form method="post" action="/batches/'+str(b['id'])+'/allocate"><button class="btn ghost small">配货(已付清→批次)</button></form> ') if role=="finance" else ''}
                {('<form method="post" action="/batches/'+str(b['id'])+'/ship"><button class="btn small">发货</button></form>') if role=="finance" else ''}</td></tr>"""
            for b in batches)
        batch_box = f"""
        <h2>发货批次</h2>
        <table><thead><tr><th>批次</th><th>最早发货</th><th>容量</th><th>已配货</th><th>财务操作</th></tr></thead>
          <tbody>{b_rows}</tbody></table>"""

    live = act["status"] == "on_sale"
    tag = '<span class="tag live">销售中</span>' if live else '<span class="tag off">已取消</span>'
    body = f"""
    <p><a href="/">← 全部活动</a></p>
    <div class="act" style="pointer-events:none">
      <img src="{esc(act['image'])}" alt="">
      <div><h1 style="margin:0">{esc(act['name'])} {tag}</h1>
        <p class="muted">{esc(act['description'])}</p></div>
    </div>
    <h2>库存看板</h2>
    {kpis}
    {order_form}
    <h2>订单（{len(orders)}）</h2>
    <table><thead><tr><th>单号</th><th>规格</th><th>规则</th>
      <th class="right">定金已付</th><th class="right">尾款已付</th><th class="right">已付合计</th>
      <th>状态</th><th>下单时间</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="8" class="muted">暂无订单</td></tr>'}</tbody></table>
    {batch_box}
    {rules_box}
    """
    return layout(role, body, flash)


KIND_CN = {
    "late_after_release": "释放后迟到付款",
    "overpay": "多付/部分款待退",
    "cancelled_activity": "取消活动后付款",
    "other": "其他异常",
}


def exceptions_page(conn, role, cases, flash=None) -> str:
    rows = []
    for c in cases:
        o = conn.execute('SELECT * FROM "order" WHERE id=?', (c["order_id"],)).fetchone()
        actions = ""
        if role == "finance" and c["status"] == "open":
            actions = f"""
            <form method="post" action="/exceptions/{c['id']}/review" style="display:flex;gap:6px">
              <button name="action" value="approve_refund" class="btn small">批准退款</button>
              <button name="action" value="force_ship" class="btn ghost small">核准发货</button>
              <button name="action" value="reject" class="btn ghost small">驳回</button>
            </form>"""
        rows.append(f"""<tr>
          <td><a href="/orders/{o['code']}">{esc(o['code'])}</a></td>
          <td>{KIND_CN.get(c['kind'], c['kind'])}</td>
          <td class="right amount">{yuan(c['amount'])}</td>
          <td class="small">{esc(c['note'])}</td>
          <td><span class="status">{esc(c['status'])}</span></td>
          <td class="small">{esc(c['raised_by'])}</td>
          <td>{actions or '<span class="muted small">仅财务可审核</span>'}</td></tr>""")
    body = f"""
    <p><a href="/">← 全部活动</a></p>
    <h1>异常款队列（{len(cases)}）</h1>
    <p class="muted">释放后迟到付款、活动取消后到账、多付与部分退款均在此挂账。
       <b>财务</b>可审核（退款/核准发货/驳回）；<b>客服</b>只能查看并在订单内发起处理请求。</p>
    <table><thead><tr><th>订单</th><th>异常类型</th><th class="right">金额</th><th>说明</th>
      <th>状态</th><th>来源</th><th>审核</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="7" class="muted">没有待处理异常款</td></tr>'}</tbody></table>
    """
    return layout(role, body, flash)
