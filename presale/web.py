"""HTTP 界面：标准库 http.server，无第三方依赖。

角色：ops 运营（规则版本/取消活动/规格确认）、finance 财务（异常款审核）、
csr 客服（只读订单 + 发起处理请求）。付款按钮模拟买家与支付渠道，不属于员工角色。
"""
from __future__ import annotations

import html
import os
import threading
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .db import connect, Clock
from .service import (
    PresaleService, PresaleError, PaymentRejected, STATUS_CN, EVENT_CN, Window,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")

ROLE_CN = {"ops": "运营", "finance": "财务", "csr": "客服"}


def money(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def h(s) -> str:
    return html.escape(str(s if s is not None else ""))


def fmt(ts: str) -> str:
    if not ts:
        return "—"
    return datetime.fromisoformat(ts).strftime("%m-%d %H:%M:%S")


class App:
    def __init__(self, db_path: str):
        self.conn = connect(db_path)
        self.clock = Clock(self.conn)
        self.svc = PresaleService(self.conn, self.clock)
        self._stop = threading.Event()
        # Web 多线程共用一个连接：用写锁串行化所有变更（BEGIN..commit），
        # 防止一个线程的 rollback 误回滚另一个线程的事务。
        self.writelock = threading.RLock()
        # 后台：投递到期回执 + 逾期释放
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def _worker_loop(self):
        while not self._stop.wait(2):
            try:
                with self.writelock:
                    self.svc.deliver_pending()
                    self.svc.sweep_expired()
            except Exception:
                # 演示环境后台失败不致命；页面操作会再次触发
                pass

    def shutdown(self):
        self._stop.set()


CSS = """
:root{--ink:#111;--muted:#6b7280;--line:#e5e7eb;--bg:#fff;--soft:#fafafa;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
a{color:#1d4ed8;text-decoration:none} a:hover{text-decoration:underline}
header{border-bottom:1px solid var(--line);padding:10px 20px;display:flex;
 gap:16px;align-items:center;flex-wrap:wrap;background:#fff}
header .brand{font-weight:700}
nav a{margin-right:14px}
.clock{font-variant-numeric:tabular-nums;font-weight:600;margin-left:auto}
.wrap{max-width:1080px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 14px} h2{font-size:16px;margin:22px 0 8px}
.grid{display:grid;gap:14px}.cards{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fff}
.card .swatch{height:6px;border-radius:3px;margin:-14px -14px 10px}
.product{display:flex;gap:14px}
.product img{width:120px;height:120px;object-fit:contain;border:1px solid var(--line);
 border-radius:8px;background:#fff}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:600}
.amount{font-weight:700;color:#000;font-variant-numeric:tabular-nums}
.amount.paid{color:#000}
.muted{color:var(--muted)} .tag{display:inline-block;padding:1px 8px;border-radius:999px;
 border:1px solid var(--line);font-size:12px;background:#fff}
.tag.reserved{border-color:#b45309;color:#92400e}
.tag.partial{border-color:#a16207;color:#854d0e;background:#fffbeb}
.tag.paid{border-color:#15803d;color:#166534}
.tag.fulfilled{border-color:#15803d;background:#f0fdf4;color:#166534}
.tag.released,.tag.cancelled{border-color:#b91c1c;color:#991b1b}
.win{display:grid;grid-template-columns:90px 1fr;gap:6px 12px;margin:6px 0}
.timeline{border-left:2px solid var(--line);margin:10px 0 10px 8px;padding-left:18px}
.timeline .ev{position:relative;margin:0 0 12px}
.timeline .ev::before{content:"";position:absolute;left:-25px;top:5px;width:9px;height:9px;
 border-radius:50%;background:#fff;border:2px solid #9ca3af}
.timeline .ev.money::before{border-color:#000}
.timeline .ev.danger::before{border-color:#b91c1c;background:#fee2e2}
.window-marker{border:1px dashed #9ca3af;border-radius:8px;padding:8px 12px;margin:6px 0;
 background:#fff}
.window-marker.open{border-style:solid;border-color:#111}
.window-marker.late{border-color:#b91c1c;color:#991b1b}
form.inline{display:inline}
button,.btn{border:1px solid #111;background:#fff;color:#111;border-radius:6px;
 padding:4px 12px;cursor:pointer;font-size:13px}
button.primary{background:#111;color:#fff}
button.danger{border-color:#b91c1c;color:#991b1b}
button:disabled{opacity:.45;cursor:not-allowed}
input,select,textarea{border:1px solid #d1d5db;border-radius:6px;padding:4px 8px;font-size:13px}
.kv{display:grid;grid-template-columns:110px 1fr;gap:4px 10px}
.stockboard td.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:700;color:#000}
.note{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.flash{border:1px solid #111;border-radius:8px;padding:8px 12px;margin-bottom:12px;background:#fff}
.flash.err{border-color:#b91c1c;color:#991b1b}
.big{font-size:22px;font-weight:800;color:#000;font-variant-numeric:tabular-nums}
"""


class Handler(BaseHTTPRequestHandler):
    app: App = None

    def log_message(self, fmt, *args):
        pass

    # ---------- 请求工具 ----------
    def _role(self) -> str:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        role = c["role"].value if "role" in c else "ops"
        return role if role in ROLE_CN else "ops"

    def _send(self, body, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra_headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str):
        self._send("", 303, extra_headers=[("Location", path)])

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def page(self, title: str, body: str, role: str, flash="") -> str:
        svc = self.app.svc
        now = svc.now().strftime("%Y-%m-%d %H:%M:%S")
        open_exc = len(svc.list_exceptions("open"))
        pend = len(svc.pending_receipts())
        role_switch = " ".join(
            f'<a class="tag" href="/role?r={r}">{ROLE_CN[r]}{"●" if r == role else ""}</a>'
            for r in ROLE_CN)
        f = f'<div class="flash err">{h(flash)}</div>' if flash else ""
        return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{h(title)}</title><style>{CSS}</style></head><body>
<header>
 <span class="brand">预售名额演示</span>
 <nav><a href="/">活动</a><a href="/balance">尾款台</a>
 <a href="/exceptions">异常款{f' ({open_exc})' if open_exc else ''}</a>
 <a href="/requests">处理请求</a>
 <a href="/stock">库存看板</a></nav>
 <span>{role_switch}</span>
 <span class="clock">虚拟时间 {now} UTC
   {f'· <span class="muted">{pend} 张回执在途</span>' if pend else ''}</span>
</header>
<div class="wrap">{f}{body}</div></body></html>"""

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        role = self._role()
        try:
            if u.path == "/":
                self._send(self.page("预售活动", self.view_activities(role), role))
            elif u.path == "/activity":
                self._send(self.page("活动详情", self.view_activity(int(q["id"]), role), role))
            elif u.path == "/order":
                self._send(self.page("订单", self.view_order(int(q["id"]), role), role))
            elif u.path == "/balance":
                self._send(self.page("尾款台", self.view_balance(role), role))
            elif u.path == "/exceptions":
                self._send(self.page("异常款队列", self.view_exceptions(role), role))
            elif u.path == "/requests":
                self._send(self.page("处理请求", self.view_requests(role), role))
            elif u.path == "/stock":
                self._send(self.page("库存看板", self.view_stock(role), role))
            elif u.path == "/role":
                self._send("", 303, extra_headers=[
                    ("Location", q.get("next", "/")),
                    ("Set-Cookie", f"role={q.get('r','ops')}; Path=/"),
                ])
            elif u.path.startswith("/static/"):
                self._static(u.path)
            else:
                self._send(self.page("404", "<p>页面不存在</p>", role), 404)
        except PresaleError as e:
            self._send(self.page("出错了", f"<p>{h(e)}</p>", role), 400)

    def do_POST(self):
        u = urlparse(self.path)
        form = self._form()
        role = self._role()
        try:
            with self.app.writelock:
                self.action(u.path, form, role)
        except PresaleError as e:
            back = form.get("back", "/")
            self._send(self.page("操作被拒绝",
                                 f'<div class="flash err">{h(e)}</div>'
                                 f'<p><a href="{h(back)}">返回</a></p>', role), 400)

    def action(self, path, f, role):
        s = self.app.svc
        back = f.get("back", "/")
        if path == "/order/create":
            oid = s.create_order(int(f["activity_id"]), int(f.get("qty") or 1))
            self._redirect(f"/order?id={oid}")
        elif path == "/clock":
            s.clock.advance(int(f["minutes"]) * 60)
            s.deliver_pending(); s.sweep_expired()
            self._redirect(back)
        elif path == "/sweep":
            if role != "ops":
                raise PresaleError("仅运营可手动执行逾期扫描")
            ids = s.sweep_expired()
            self._redirect(back)
        elif path == "/pay":
            cents = round(float(f["amount_yuan"]) * 100)
            delay = int(f.get("delay_min") or 0) * 60
            s.sweep_expired()  # 让“窗口刚过”的状态先落地，竞争交给写锁
            receipt = s.send_balance_receipt(int(f["order_id"]), cents, delay)
            if delay == 0:
                s.deliver_pending()
            self._redirect(back)
        elif path == "/pay/dup":
            s.send_duplicate(int(f["pending_id"]))
            self._redirect(back)
        elif path == "/deliver":
            s.deliver_now(int(f["pending_id"]))
            self._redirect(back)
        elif path == "/fulfill":
            if role != "ops":
                raise PresaleError("仅运营可操作配货发货")
            s.fulfill(int(f["order_id"]))
            self._redirect(back)
        elif path == "/version":
            if role != "ops":
                raise PresaleError("仅运营可发布规则版本")
            s.publish_version(int(f["activity_id"]),
                              round(float(f["deposit"]) * 100),
                              round(float(f["balance"]) * 100),
                              f["spec"], f.get("note", ""))
            self._redirect(f"/activity?id={f['activity_id']}")
        elif path == "/cancel":
            if role != "ops":
                raise PresaleError("仅运营可取消预售（客服请发处理请求）")
            s.cancel_activity(int(f["activity_id"]), f.get("note", ""), ROLE_CN[role])
            self._redirect(f"/activity?id={f['activity_id']}")
        elif path == "/spec/confirm":
            if role != "ops":
                raise PresaleError("规格变更确认由运营执行")
            s.confirm_spec_change(int(f["order_id"]), f.get("note", ""), role)
            self._redirect(back)
        elif path == "/request":
            if role != "csr":
                raise PresaleError("仅客服可发起处理请求")
            s.open_service_request(int(f["order_id"]), f["request_type"],
                                   f.get("reason", ""), role)
            self._redirect(back)
        elif path == "/request/review":
            if role not in ("ops", "finance"):
                raise PresaleError("仅运营/财务可审核")
            s.review_service_request(int(f["id"]), f.get("approve") == "1",
                                     f.get("note", ""), role)
            self._redirect("/requests")
        elif path == "/exception/resolve":
            if role != "finance":
                raise PresaleError("仅财务可审核异常款")
            s.resolve_exception(int(f["id"]), f["decision"], f.get("note", ""), ROLE_CN[role])
            self._redirect("/exceptions")
        else:
            self._redirect(back)

    # ---------- 静态资源 ----------
    def _static(self, path):
        rel = path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            self._send("not found", 404, "text/plain")
            return
        ctype = "image/svg+xml" if full.endswith(".svg") else "application/octet-stream"
        with open(full, "rb") as fp:
            self._send(fp.read(), 200, ctype)

    # ---------- 视图 ----------
    def _window_line(self, a, kind, role):
        s = self.app.svc
        now = s.now()
        if kind == "deposit":
            st = Window(now).state(a["deposit_open_at"], a["deposit_close_at"])
            o, c = a["deposit_open_at"], a["deposit_close_at"]
            label = "定金下单窗口"
        else:
            st = Window(now).state(a["balance_open_at"], a["balance_close_at"])
            o, c = a["balance_open_at"], a["balance_close_at"]
            label = "尾款支付窗口"
        cls = {"before": "", "open": "open", "after": "late"}[st]
        txt = {"before": "未开始", "open": "● 进行中", "after": "已截止"}[st]
        return (f'<div class="window-marker {cls}"><b>{label}</b>：{txt}<br>'
                f'<span class="muted">{fmt(o)} → {fmt(c)} (UTC)</span></div>')

    def view_activities(self, role):
        s = self.app.svc
        cards = []
        for a in s.list_activities():
            avail = s.available_qty(a)
            rv = s.rule_versions(a["id"])[-1]
            status = '<span class="tag cancelled">已取消</span>' if a["status"] == "cancelled" else ""
            cards.append(f"""
 <div class="card">
  <div class="swatch" style="background:{h(a['color'])}"></div>
  <div class="product">
   <img src="/static/images/{h(a['image'])}" alt="">
   <div>
    <a href="/activity?id={a['id']}"><b>{h(a['name'])}</b></a> {status}<br>
    <span class="muted">{h(a['sku'])} · 规则 v{a['current_version']}</span><br>
    <span class="amount">{money(rv['deposit_cents'])} 定金 + {money(rv['balance_cents'])} 尾款</span><br>
    <span class="muted">名额 {a['stock']} · 可售 <b>{avail}</b></span>
   </div>
  </div>
  {self._window_line(a, 'deposit', role)}
  {self._window_line(a, 'balance', role)}
 </div>""")
        return f'<h1>预售活动</h1><div class="grid cards">{"".join(cards)}</div>'

    def view_activity(self, aid, role):
        s = self.app.svc
        a = s.get_activity(aid)
        cur_rv = s.rule_versions(aid)[-1]
        out = [f'<h1>{h(a["name"])} <span class="muted">{h(a["sku"])}</span></h1>']
        out.append(f"""
 <div class="card" style="border-left:4px solid {h(a['color'])}">
  <div class="product"><img src="/static/images/{h(a['image'])}" alt="">
   <div class="kv">
    <span class="muted">当前规则</span><b>v{a['current_version']}</b>
    <span class="muted">定金</span><span class="amount">{money(cur_rv['deposit_cents'])}</span>
    <span class="muted">尾款</span><span class="amount">{money(cur_rv['balance_cents'])}</span>
    <span class="muted">规格</span><span>{h(cur_rv['spec'])}</span>
   </div></div>
  {self._window_line(a, 'deposit', role)}
  {self._window_line(a, 'balance', role)}
  <p><a href="/stock">库存：预留 {a['reserved']} · 已付 {a['paid']} · 释放 {a['released']} / 共 {a['stock']}</a></p>
 </div>""")

        # 下单
        wd = Window(s.now()).state(a["deposit_open_at"], a["deposit_close_at"])
        if a["status"] == "active" and wd == "open":
            out.append(f"""
 <form method="post" action="/order/create" class="card">
  <input type="hidden" name="activity_id" value="{aid}">
  <b>模拟买家下单</b>（定金随单支付，锁定当前规则 v{a['current_version']}）
  数量 <input name="qty" value="1" size="3">
  <button class="primary">占名额并付定金</button>
 </form>""")

        # 规则版本历史 + 新版本（运营）
        out.append('<h2>定金规则版本（下单时锁定）</h2><table><tr><th>版本</th><th>定金</th>'
                   '<th>尾款</th><th>规格</th><th>说明</th><th>发布时间</th></tr>')
        for rv in s.rule_versions(aid):
            cur = ' <span class="tag">当前</span>' if rv["version"] == a["current_version"] else ""
            out.append(f"<tr><td>v{rv['version']}{cur}</td>"
                       f"<td class='amount'>{money(rv['deposit_cents'])}</td>"
                       f"<td class='amount'>{money(rv['balance_cents'])}</td>"
                       f"<td>{h(rv['spec'])}</td><td>{h(rv['note'])}</td>"
                       f"<td>{fmt(rv['created_at'])}</td></tr>")
        out.append("</table>")
        if role == "ops" and a["status"] == "active":
            out.append(f"""
 <form method="post" action="/version" class="card">
  <input type="hidden" name="activity_id" value="{aid}">
  <b>发布新规则版本</b>（不影响已下单订单）<br>
  定金 <input name="deposit" value="{cur_rv['deposit_cents']/100:.2f}" size="6">
  尾款 <input name="balance" value="{cur_rv['balance_cents']/100:.2f}" size="6">
  规格 <input name="spec" value="{h(cur_rv['spec'])}" size="26">
  说明 <input name="note" size="20" placeholder="规格变更/调价">
  <button>发布 v{a['current_version']+1}</button>
 </form>
 <form method="post" action="/cancel" class="card">
  <input type="hidden" name="activity_id" value="{aid}">
  <b>取消预售</b> 原因 <input name="note" size="30">
  <button class="danger">取消并退定金/部分尾款</button>
 </form>""")

        # 订单列表
        out.append('<h2>订单</h2><table><tr><th>订单号</th><th>规则</th><th>数量</th>'
                   '<th>定金</th><th>尾款已付/应付</th><th>状态</th><th></th></tr>')
        for o in s.list_orders(aid):
            due = o["balance_cents"] * o["qty"]
            out.append(f"""<tr>
 <td><a href="/order?id={o['id']}">{h(o['order_no'])}</a></td>
 <td>v{o['rule_version']}</td><td>{o['qty']}</td>
 <td class="amount">{money(o['paid_deposit_cents'])}</td>
 <td><span class="amount">{money(o['paid_balance_cents'])}</span>
     <span class="muted">/ {money(due)}</span></td>
 <td><span class="tag {o['status']}">{STATUS_CN[o['status']]}</span></td>
 <td><a href="/order?id={o['id']}">详情</a></td></tr>""")
        out.append("</table>")
        return "".join(out)

    def view_order(self, oid, role):
        s = self.app.svc
        o = s.get_order(oid)
        a = s.get_activity(o["activity_id"])
        due_total = o["balance_cents"] * o["qty"]
        remain = due_total - o["paid_balance_cents"]
        paid_total = o["paid_deposit_cents"] + o["paid_balance_cents"]
        grand = (o["deposit_cents"] + o["balance_cents"]) * o["qty"]
        out = [f'<h1>订单 {h(o["order_no"])} '
               f'<span class="tag {o["status"]}">{STATUS_CN[o["status"]]}</span></h1>']
        out.append(f"""
 <div class="card" style="border-left:4px solid {h(a['color'])}">
  <div class="product"><img src="/static/images/{h(a['image'])}" alt="">
   <div><b>{h(a['name'])}</b><br><span class="muted">{h(o['spec_snapshot'])}
   · 下单锁定规则 v{o['rule_version']} · ×{o['qty']}</span></div></div>
  <table style="margin-top:10px">
   <tr><th></th><th>定金</th><th>尾款</th><th>已付合计</th></tr>
   <tr><td class="muted">金额</td>
    <td class="amount">{money(o['deposit_cents'] * o['qty'])}</td>
    <td class="amount">{money(due_total)}</td>
    <td class="amount big">{money(paid_total)}</td></tr>
   <tr><td class="muted">已付</td>
    <td class="amount">{money(o['paid_deposit_cents'])}</td>
    <td class="amount">{money(o['paid_balance_cents'])}</td>
    <td class="muted">订单总额 {money(grand)}</td></tr>
  </table>
  <p class="muted" style="margin-bottom:0">已付合计 = 已付定金 + 已付尾款；
   未付尾款不计入。{f'活动取消已退 {money(o["cancel_refunded_cents"])}。'
                    if o['cancel_refunded_cents'] else ''}</p>
 </div>""")

        # 窗口
        out.append('<h2>付款窗口</h2>')
        out.append(self._window_line(a, "deposit", role))
        out.append(self._window_line(a, "balance", role))

        # 尾款台操作
        bs = Window(s.now()).state(a["balance_open_at"], a["balance_close_at"])
        if o["status"] in ("reserved", "partial") and a["status"] == "active":
            if bs == "before":
                out.append('<p class="note">尾款期未开始，应付金额将在 '
                           f'{fmt(a["balance_open_at"])} (UTC) 后显示并可付。</p>')
            else:
                after_note = ("已过截止时间；付款将形成迟到回执（先触发逾期扫描再投递，"
                              "用于演示支付与释放的竞争）" if bs == "after" else "")
                out.append(f"""
 <form method="post" action="/pay" class="card">
  <input type="hidden" name="order_id" value="{oid}">
  <input type="hidden" name="back" value="/order?id={oid}">
  <b>模拟尾款支付</b>（应付 <span class="amount">{money(remain)}</span>，
   截止 {fmt(a['balance_close_at'])}）<br>
  金额 <input name="amount_yuan" value="{remain/100:.2f}" size="7"> 元
  回执延迟 <input name="delay_min" value="0" size="3"> 分钟
  <button class="primary">发送支付回执</button>
  <p class="muted">{after_note}。可先付部分金额验证部分支付。</p>
 </form>""")

        # 在途回执：可手动投递 / 重放（重复回执）
        pending = [p for p in s.pending_receipts() if p["order_id"] == oid]
        if pending:
            out.append('<h2>在途回执（模拟渠道延迟）</h2><table>'
                       '<tr><th>回执号</th><th>金额</th><th>付款时刻</th><th>预计到达</th>'
                       '<th>操作</th></tr>')
            for p in pending:
                out.append(f"""<tr><td>{h(p['receipt_no'])}</td>
 <td class="amount">{money(p['amount_cents'])}</td><td>{fmt(p['paid_at'])}</td>
 <td>{fmt(p['arrive_at'])}</td><td>
 <form class="inline" method="post" action="/deliver"><input type="hidden" name="pending_id"
  value="{p['id']}"><input type="hidden" name="back" value="/order?id={oid}">
  <button>立即补投</button></form>
 <form class="inline" method="post" action="/pay/dup"><input type="hidden" name="pending_id"
  value="{p['id']}"><input type="hidden" name="back" value="/order?id={oid}">
  <button class="danger">重放(重复回执)</button></form>
 </td></tr>""")
            out.append("</table>")

        # 角色操作
        if role == "ops" and o["status"] == "paid":
            out.append(f"""
 <form method="post" action="/fulfill"><input type="hidden" name="order_id" value="{oid}">
 <button class="primary">配货并发货</button></form>""")
        if role == "ops" and a["status"] == "active" and a["current_version"] > o["rule_version"] \
                and o["status"] in ("reserved", "partial"):
            out.append(f"""
 <form method="post" action="/spec/confirm" class="card">
  <input type="hidden" name="order_id" value="{oid}">
  客户确认按新规格 v{a['current_version']} 履约（价格仍按 v{o['rule_version']}）
  <input name="note" value="客户电话确认接受新规格" size="28">
  <button>确认规格变更</button></form>""")
        if role == "csr" and o["status"] in ("reserved", "partial"):
            out.append(f"""
 <form method="post" action="/request" class="card">
  <input type="hidden" name="order_id" value="{oid}">
  <input type="hidden" name="back" value="/order?id={oid}">
  <b>客服处理请求</b>（只能提交，由运营/财务审核）<br>
  类型 <select name="request_type">
   <option value="release_early">申请提前释放名额并退款</option>
   <option value="cancel_activity">申请取消整个预售活动</option>
   <option value="manual_review">转人工核查付款问题</option></select>
  原因 <input name="reason" size="28">
  <button>发起请求</button></form>""")
        if role == "csr":
            out.append('<p class="muted">客服权限：可查看订单、时间线与回执，可发起处理请求；'
                       '不能改价、退款或审核异常款。</p>')

        # 时间轴
        out.append('<h2>订单时间线</h2><div class="timeline">')
        # 窗口标记先画
        for label, o_ts, c_ts, key in (
            ("定金窗口", a["deposit_open_at"], a["deposit_close_at"], "dep"),
            ("尾款窗口", a["balance_open_at"], a["balance_close_at"], "bal"),
        ):
            st = Window(s.now()).state(o_ts, c_ts)
            cls = {"before": "", "open": "open", "after": "late"}[st]
            cn = {"before": "（未开始）", "open": "（进行中 ▶）", "after": "（已截止）"}[st]
            out.append(f'<div class="window-marker {cls}" style="margin-left:-10px">'
                       f'{label}{cn} {fmt(o_ts)} → {fmt(c_ts)}</div>')
        danger = {"released", "activity_cancelled", "late_receipt", "overdue_receipt",
                  "overpaid_receipt", "duplicate_receipt", "exception_opened"}
        for ev in s.timeline(oid):
            cls = "danger" if ev["kind"] in danger else ("money" if ev["amount_cents"] else "")
            amt = f' <span class="amount">{money(ev["amount_cents"])}</span>' \
                if ev["amount_cents"] else ""
            label = EVENT_CN.get(ev["kind"], ev["kind"])
            out.append(f'<div class="ev {cls}"><b>{fmt(ev["at"])}</b> {h(label)}{amt}'
                       f'{("<br><span class=muted>" + h(ev["detail"]) + "</span>") if ev["detail"] else ""}'
                       '</div>')
        out.append('</div>')

        # 回执台账
        pays = s.payments_of(oid)
        if pays:
            out.append('<h2>支付回执台账</h2><table><tr><th>阶段</th><th>回执号</th>'
                       '<th>金额</th><th>付款时刻</th><th>到达时刻</th><th>入账</th>'
                       '<th>备注</th></tr>')
            for p in pays:
                mark = "✅ 是" if p["applied_to_order"] else "— 否（挂异常/忽略）"
                out.append(f"<tr><td>{'定金' if p['phase']=='deposit' else '尾款'}</td>"
                           f"<td>{h(p['receipt_no'])}</td>"
                           f"<td class='amount'>{money(p['amount_cents'])}</td>"
                           f"<td>{fmt(p['paid_at'])}</td><td>{fmt(p['arrived_at'])}</td>"
                           f"<td>{mark}</td><td>{h(p['note'])}</td></tr>")
            out.append("</table>")
        return "".join(out)

    def view_balance(self, role):
        s = self.app.svc
        now = s.now()
        out = ['<h1>尾款台 · 待付尾款订单</h1>',
               '<p class="muted">尾款期开始后才显示应付金额与截止时间；逾期订单由后台自动扫描释放。</p>']
        rows = []
        for o in s.list_orders():
            if o["status"] not in ("reserved", "partial"):
                continue
            a = s.get_activity(o["activity_id"])
            if a["status"] != "active":
                continue
            st = Window(now).state(a["balance_open_at"], a["balance_close_at"])
            remain = o["balance_cents"] * o["qty"] - o["paid_balance_cents"]
            rows.append((o, a, st, remain))
        out.append('<table><tr><th>订单</th><th>商品</th><th>状态</th><th>应付尾款</th>'
                   '<th>尾款窗口</th><th>截止时间</th></tr>')
        if not rows:
            out.append('<tr><td colspan="6" class="muted">暂无待付尾款订单</td></tr>')
        for o, a, st, remain in rows:
            if st == "before":
                due = '<span class="muted">尾款期未开始</span>'
                win = '<span class="tag">未开始</span>'
                close = f'<span class="muted">{fmt(a["balance_close_at"])}</span>'
            elif st == "open":
                due = f'<span class="amount big">{money(remain)}</span>'
                if o["paid_balance_cents"]:
                    due += f'<br><span class="muted">已部分支付 {money(o["paid_balance_cents"])}</span>'
                win = '<span class="tag paid">进行中</span>'
                close = f'<b>{fmt(a["balance_close_at"])}</b>'
            else:
                due = '<span class="muted">等待逾期扫描释放</span>'
                win = '<span class="tag released">已截止</span>'
                close = f'<span class="muted">{fmt(a["balance_close_at"])}</span>'
            out.append(f'<tr><td><a href="/order?id={o["id"]}">{h(o["order_no"])}</a></td>'
                       f'<td>{h(a["name"])} <span class="muted">v{o["rule_version"]}</span></td>'
                       f'<td><span class="tag {o["status"]}">{STATUS_CN[o["status"]]}</span></td>'
                       f'<td>{due}</td><td>{win}</td><td>{close}</td></tr>')
        out.append("</table>")
        return "".join(out)

    EXC_CN = {"late_payment": "释放后迟到付款", "overpayment": "结清后重复/超额到账",
              "overpaid_balance": "本次金额超应付", "partial_timeout": "部分支付后逾期",
              "cancel_payment": "取消后到账"}

    def view_exceptions(self, role):
        s = self.app.svc
        out = ['<h1>异常款队列</h1>']
        if role != "finance":
            out.append('<p class="note">当前为只读视图：异常款仅<b>财务</b>可审核'
                       '（退款 / 批准回补发货 / 驳回）。可切换右上角角色为财务。</p>')
        for st, label in (("open", "待处理"), ("resolved", "已处理")):
            items = [e for e in s.list_exceptions(st)]
            if not items:
                continue
            out.append(f'<h2>{label}（{len(items)}）</h2>')
            out.append('<table><tr><th>#</th><th>类型</th><th>订单</th><th>金额</th>'
                       '<th>说明</th><th>开立时间</th><th>处理</th></tr>')
            for e in items:
                action = ""
                if st == "open" and role == "finance":
                    ship = ('<button name="decision" value="ship">批准占用回补名额发货</button>'
                            if e["type"] == "late_payment" else "")
                    action = f"""<form method="post" action="/exception/resolve">
                     <input type="hidden" name="id" value="{e['id']}">
                     <input name="note" placeholder="审核备注" size="16">
                     <button name="decision" value="refund" class="danger">退款</button>
                     {ship}
                     <button name="decision" value="reject">驳回</button></form>"""
                elif st == "resolved":
                    action = f'<span class="tag">{h(e["status"])}</span>'
                out.append(f"""<tr><td>{e['id']}</td>
 <td><b>{self.EXC_CN.get(e['type'], e['type'])}</b></td>
 <td><a href="/order?id={e['order_id']}">{h(e['order_no'])}</a><br>
 <span class="muted">{h(e['activity_name'])}</span></td>
 <td class="amount">{money(e['amount_cents']) if e['amount_cents'] else '—'}</td>
 <td>{h(e['detail'])}</td><td>{fmt(e['opened_at'])}</td><td>{action}</td></tr>""")
            out.append("</table>")
        return "".join(out)

    REQ_CN = {"release_early": "提前释放名额", "cancel_activity": "取消预售",
              "spec_change_confirm": "规格变更确认", "manual_review": "人工核查"}

    def view_requests(self, role):
        s = self.app.svc
        out = ['<h1>客服处理请求</h1>']
        if role == "csr":
            out.append('<p class="note">客服只能发起请求；审核请由运营/财务账号操作。</p>')
        for st, label in (("pending", "待审核"), ("approved", "已批准"),
                          ("rejected", "已驳回")):
            items = s.list_requests(st)
            if not items:
                continue
            out.append(f'<h2>{label}（{len(items)}）</h2><table>'
                       '<tr><th>#</th><th>类型</th><th>订单</th><th>原因</th>'
                       '<th>发起人/时间</th><th>审核</th></tr>')
            for r in items:
                review = ""
                if st == "pending" and role in ("ops", "finance"):
                    review = f"""<form method="post" action="/request/review">
                     <input type="hidden" name="id" value="{r['id']}">
                     <input name="note" size="18" placeholder="审核备注">
                     <button name="approve" value="1" class="primary">批准</button>
                     <button name="approve" value="0" class="danger">驳回</button></form>"""
                elif st != "pending":
                    review = f'{h(r["reviewed_by"])}：{h(r["review_note"])}'
                out.append(f"""<tr><td>{r['id']}</td>
 <td>{self.REQ_CN.get(r['request_type'], r['request_type'])}</td>
 <td><a href="/order?id={r['order_id']}">{h(r['order_no'])}</a></td>
 <td>{h(r['reason'])}</td><td>{h(r['opened_by'])} · {fmt(r['at'])}</td>
 <td>{review}</td></tr>""")
            out.append("</table>")
        return "".join(out)

    def view_stock(self, role):
        s = self.app.svc
        out = ['<h1>库存看板</h1>',
               '<p class="muted">三值含义：<b>预留</b>=已占名额（定金/部分尾款）；'
               '<b>已付</b>=尾款结清进入配货；<b>释放</b>=逾期/取消回收且当前可回补的名额'
               '（再次售出或批准补发货后相应扣减）。'
               '可售 = 总名额 − 预留 − 已付。</p>',
               '<table class="stockboard"><tr><th>活动</th><th>总名额</th>'
               '<th>预留</th><th>已付</th><th>释放</th><th>可售</th><th>校验</th></tr>']
        for a in s.list_activities():
            avail = s.available_qty(a)
            check = a["reserved"] + a["paid"] + a["released"]
            ok = check <= a["stock"]
            out.append(f"""<tr>
 <td><span style="display:inline-block;width:10px;height:10px;border-radius:2px;
  background:{h(a['color'])};margin-right:6px"></span>
  <a href="/activity?id={a['id']}">{h(a['name'])}</a></td>
 <td class="num">{a['stock']}</td>
 <td class="num">{a['reserved']}</td>
 <td class="num">{a['paid']}</td>
 <td class="num">{a['released']}</td>
 <td class="num">{avail}</td>
 <td>{'✅ 未超卖' if ok else '❌ 超卖'}</td></tr>""")
        out.append("</table>")
        out.append("""
 <h2>时间控制（演示）</h2><form class="card" method="post" action="/clock">
  把虚拟时钟向前拨
  <button name="minutes" value="10">+10 分钟</button>
  <button name="minutes" value="60">+1 小时</button>
  <button name="minutes" value="1440">+1 天</button>
  <span class="muted">（真实 1 秒 = 虚拟 10 秒，到期自动投递回执/释放名额）</span>
 </form>
 <form class="card" method="post" action="/sweep">
  <button>立即执行逾期扫描</button> <span class="muted">运营角色可用</span></form>""")
        return "".join(out)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    app = App(db_path)
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"预售演示已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        httpd.server_close()
