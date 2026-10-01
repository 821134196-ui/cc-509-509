"""零依赖 HTTP 服务 + 后台时间推进（开窗 / 投递延迟回执 / 逾期释放）。"""
from __future__ import annotations

import http.server
import sqlite3
import threading
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import clock, db, payment_adapter, queries, seed as seed_mod, services, templates

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

FINANCE_ONLY = {"finance"}
SERVICE_ALLOWED = {"customer_service"}


class App:
    def __init__(self, db_path: str, tick_sec: float = 1.0):
        self.db_path = db_path
        self.conn = db.connect(db_path)
        db.init_db(self.conn)
        self.tick_sec = tick_sec
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    # 每个请求使用独立连接，避免线程间共享
    def cx(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def tick_once(self):
        conn = self.cx()
        try:
            services.enter_balance_window(conn)
            payment_adapter.deliver_due(conn)
            services.release_overdue(conn)
        finally:
            conn.close()

    def _loop(self):
        while not self._stop.wait(self.tick_sec):
            try:
                self.tick_once()
            except Exception as e:  # 后台循环不能因单笔异常退出
                print("[tick]", repr(e))


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    # ---- 基础工具 ----
    def _role(self) -> str:
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        return ck["role"].value if "role" in ck else "customer"

    def _send(self, text: str, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra_headers=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, flash=None):
        loc = path
        if flash:
            sep = "&" if "?" in path else "?"
            loc += f'{sep}msg={quote(flash[1])}&kind={flash[0]}'
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def _flash(self, qs):
        if qs.get("msg"):
            return (qs.get("kind", "ok"), qs["msg"][0])
        return None

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _require(self, roles, back="/"):
        if self._role() not in roles:
            self._redirect(back, ("err", "权限不足：该操作仅限 " +
                           "、".join(roles) + " 视角"))
            return False
        return True

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p == "/":
                return self.home(qs)
            if p.startswith("/role/"):
                role = p.rsplit("/", 1)[-1]
                if role in ("customer", "finance", "customer_service"):
                    return self._send("", 302, extra_headers=[
                        ("Location", "/"),
                        ("Set-Cookie", f"role={role}; Path=/")])
            if p.startswith("/images/"):
                return self.static(p)
            if p.startswith("/activity/"):
                return self.activity(int(p.split("/")[2]), qs)
            if p.startswith("/orders/"):
                return self.order(p.split("/")[2], qs)
            if p == "/exceptions":
                return self.exceptions(qs)
            self._send("not found", 404, "text/plain; charset=utf-8")
        except services.ServiceError as e:
            self._redirect("/", ("err", str(e)))
        except Exception as e:
            self._send(f"server error: {e!r}", 500, "text/plain; charset=utf-8")

    def do_POST(self):
        p = urlparse(self.path).path
        f = self._form()
        try:
            if p == "/pay/charge":
                if not self._require({"customer"}, f.get("code", "/")):
                    return
                return self.pay_charge(f)
            if p == "/pay/replay":
                if not self._require({"customer"}, "/"):
                    return
                rn = f.get("receipt", "")
                res = payment_adapter.replay(self.app.cx(), rn)
                return self._redirect("/exceptions" if res["result"] == "exception" else "/",
                                      ("ok", f"重放回执 {rn}：{res['result']}（重复回执幂等忽略）"
                                       if res["result"] == "duplicate"
                                       else f"重放结果：{res['result']}"))
            if p.startswith("/activity/"):
                parts = p.split("/")
                aid, action = int(parts[2]), parts[3]
                if action == "orders":
                    if not self._require({"customer"}, f"/activity/{aid}"):
                        return
                    return self.create_order(aid, f)
                if action == "rules":
                    if not self._require(FINANCE_ONLY, f"/activity/{aid}"):
                        return
                    return self.add_rules(aid, f)
                if action == "specs":
                    if not self._require(FINANCE_ONLY, f"/activity/{aid}"):
                        return
                    specs = []
                    for line in f.get("specs", "").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        name, _, extra = line.partition(":")
                        specs.append((name.strip(), int(extra.strip() or 0)))
                    services.change_specs(self.app.cx(), aid, specs)
                    return self._redirect(f"/activity/{aid}",
                                          ("ok", "规格已变更，仅影响之后下单"))
                if action == "cancel":
                    if not self._require(FINANCE_ONLY, f"/activity/{aid}"):
                        return
                    n = services.cancel_activity(self.app.cx(), aid)
                    return self._redirect(f"/activity/{aid}",
                                          ("ok", f"活动已取消，{n} 个未付清订单已处理"))
            if p.startswith("/orders/") and p.endswith("/service-request"):
                oid = int(p.split("/")[2])
                if not self._require({"customer_service"}, "/"):
                    return
                services.create_service_request(self.app.cx(), oid,
                                                f.get("type", "investigate"),
                                                f.get("reason", ""))
                code = self.app.cx().execute(
                    'SELECT code FROM "order" WHERE id=?', (oid,)).fetchone()["code"]
                return self._redirect(f"/orders/{code}", ("ok", "客服处理请求已提交，等待财务处理"))
            if p.startswith("/exceptions/") and p.endswith("/review"):
                cid = int(p.split("/")[2])
                if not self._require(FINANCE_ONLY, "/exceptions"):
                    return
                action = {"approve_refund": "approve_refund",
                          "force_ship": "force_ship", "reject": "reject"}[f.get("action")]
                services.review_exception(self.app.cx(), cid, action)
                return self._redirect("/exceptions", ("ok", "审核完成"))
            if p.startswith("/batches/"):
                bid = int(p.split("/")[2])
                action = p.split("/")[3]
                conn = self.app.cx()
                if action == "allocate":
                    ids = services.allocate(conn, conn.execute(
                        "SELECT activity_id FROM shipment_batch WHERE id=?", (bid,)).fetchone()[0], bid)
                    return self._redirect("/", ("ok", f"已配货 {len(ids)} 单到批次"))
                n = services.ship_batch(conn, bid)
                return self._redirect("/", ("ok", f"批次发货 {n} 单"))
            self._send("not found", 404, "text/plain; charset=utf-8")
        except services.ServiceError as e:
            self._redirect("/", ("err", str(e)))
        except Exception as e:
            self._send(f"server error: {e!r}", 500, "text/plain; charset=utf-8")

    # ---- 页面 ----
    def home(self, qs):
        conn = self.app.cx()
        acts = conn.execute("SELECT * FROM activity ORDER BY id").fetchall()
        self._send(templates.home(conn, self._role(), acts, self._flash(qs)))

    def static(self, p):
        fp = (STATIC / p.replace("/images/", "images/")).resolve()
        if not str(fp).startswith(str(STATIC)) or not fp.exists():
            return self._send("not found", 404, "text/plain")
        ctype = "image/svg+xml" if fp.suffix == ".svg" else "application/octet-stream"
        self._send(fp.read_text(), 200, ctype)

    def activity(self, aid, qs):
        conn = self.app.cx()
        act = conn.execute("SELECT * FROM activity WHERE id=?", (aid,)).fetchone()
        if not act:
            return self._send("not found", 404, "text/plain")
        orders = conn.execute(
            """SELECT o.*, s.spec FROM "order" o JOIN sku s ON s.id=o.sku_id
               WHERE o.activity_id=? ORDER BY o.id""", (aid,)).fetchall()
        rules = conn.execute(
            "SELECT * FROM deposit_rule WHERE activity_id=? ORDER BY version DESC", (aid,)).fetchall()
        batches = conn.execute(
            "SELECT * FROM shipment_batch WHERE activity_id=? ORDER BY id", (aid,)).fetchall()
        inv = queries.inventory(conn, aid)
        self._send(templates.activity_page(conn, self._role(), act, orders, inv,
                                           rules, batches, clock.now(), self._flash(qs)))

    def order(self, code, qs):
        conn = self.app.cx()
        o = queries.order_detail(conn, code)
        if not o:
            return self._send("not found", 404, "text/plain")
        pays = queries.order_payments(conn, o["id"])
        self._send(templates.order_page(conn, self._role(), o, pays,
                                        clock.now(), self._flash(qs)))

    def exceptions(self, qs):
        conn = self.app.cx()
        cases = conn.execute(
            "SELECT * FROM exception_case ORDER BY status DESC, id DESC").fetchall()
        self._send(templates.exceptions_page(conn, self._role(), cases, self._flash(qs)))

    # ---- 动作 ----
    def create_order(self, aid, f):
        conn = self.app.cx()
        o = services.create_order(conn, aid, f.get("spec", ""))
        self._redirect(f"/orders/{o['code']}", ("ok", "已下单并占用名额，规则版本已锁定"))

    def add_rules(self, aid, f):
        conn = self.app.cx()
        latest = services.get_active_rule(conn, aid)
        ver = services.add_rule_version(
            conn, aid, int(f.get("deposit", 0)), int(f.get("balance", 0)),
            latest["deposit_open"], latest["deposit_close"],
            latest["balance_open"], latest["balance_close"],
            int(f.get("grace", 0)))
        self._redirect(f"/activity/{aid}", ("ok", f"已发布规则 v{ver}，仅影响之后下单"))

    def pay_charge(self, f):
        conn = self.app.cx()
        code, stage = f.get("code", ""), f.get("stage", "balance")
        amount = int(f.get("amount", 0))
        delay = float(f.get("delay", 0) or 0)
        rn = payment_adapter.charge(conn, code, stage, amount, delay_sec=delay)
        if delay <= 0:
            payment_adapter.deliver_due(conn)
            pay = conn.execute("SELECT status FROM payment WHERE receipt_no=?", (rn,)).fetchone()
            st = pay["status"] if pay else "recorded"
            if st == "duplicate":
                msg = "重复回执，已忽略"
            elif st == "exception":
                msg = "付款进入异常款队列，等待财务审核"
            elif st == "applied":
                msg = "支付成功"
            else:
                msg = "已收到回执"
        else:
            msg = f"付款已发起，回执将延迟 {delay:g} 秒到达（可继续刷新观察）"
        self._redirect(f"/orders/{code}", ("ok" if "异常" not in msg else "err", msg))


def serve(db_path: str, port: int = 8000, reseed: bool = True):
    if reseed or not Path(db_path).exists():
        conn = db.connect(db_path)
        db.init_db(conn)
        seed_mod.seed(conn)
        conn.close()
    app = App(db_path)
    Handler.app = app
    app.start()
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"预售系统已启动: http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.server_close()
