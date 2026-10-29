# -*- coding: utf-8 -*-
"""多租戶 LINE webhook：POST /webhook/{tenant}

每個租戶自己的官方帳號，webhook 網址都填 https://<我們的網域>/webhook/<租戶id>。
一個請求進來：
  ① 找租戶設定（沒有＝404）→ ② 驗簽（不過＝403，不讀 body 內容）→ ③ 事件逐筆落檔（租戶自己的資料夾）
  → ④ 內建路由：審核鈕、本人指令（待審／額度／測試）、群組名冊 → ⑤ 外掛 handler（各功能自己註冊）
  → ⑥ 對方剛開口、我們又欠他訊息（核准了但當時沒額度）→ 用這次的回覆補送（不計額度）

先落檔再處理：處理壞了，原始事件還在，可以重放。
正式環境開 background=True：落檔完就回 200，處理丟背景執行緒——LINE 等太久會判定 webhook 失敗。

開發機跑起來（金鑰從 <資料根>/tenants/<租戶>/line/config.json 讀）：
    python -m adapters.line.webhook --port 8080
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from adapters.line import groups
from adapters.line.client import LineClient
from adapters.line.credentials import CredentialStore, FileCredentialStore, TenantLineConfig
from adapters.line.flex import parse_postback
from adapters.line.push_gate import PushGate
from adapters.line.signature import verify
from adapters.storage import TenantStore, is_reserved_tenant, validate_tenant_id
from core import brand

MAX_BODY = 1_000_000
WEBHOOK_PREFIX = "/webhook/"

CMD_PENDING = {"待審", "待審核", "審核"}
CMD_QUOTA = {"額度", "推播額度"}
CMD_PING = {"測試", "ping", "/ping"}


@dataclass
class EventContext:
    """交給 handler 的一筆事件。handler 用 ctx.reply() 回話；回過就不會再被別人回。"""
    tenant_id: str
    cfg: TenantLineConfig
    event: dict
    client: LineClient
    gate: PushGate
    store: TenantStore
    replied: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        src = self.event.get("source") or {}
        return src.get("groupId") or src.get("roomId") or src.get("userId") or ""

    @property
    def user_id(self) -> str:
        return (self.event.get("source") or {}).get("userId", "")

    @property
    def is_owner_dm(self) -> bool:
        src = self.event.get("source") or {}
        return src.get("type") == "user" and self.cfg.can_review(src.get("userId", ""))

    @property
    def text(self) -> str:
        msg = self.event.get("message") or {}
        return str(msg.get("text") or "").strip() if msg.get("type") == "text" else ""

    def reply(self, messages: list[dict]) -> bool:
        token = self.event.get("replyToken")
        if self.replied or not token or not messages:
            return False
        res = self.client.reply(token, messages[:5])
        self.replied = res.ok
        if not res.ok:
            self.notes.append(f"回覆失敗：{res.error}")
        return res.ok

    def reply_text(self, text: str) -> bool:
        return self.reply([{"type": "text", "text": text[:5000]}])


Handler = Callable[[EventContext], Any]


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


class WebhookApp:
    def __init__(self, credentials: CredentialStore, store: TenantStore, *,
                 client_factory: Callable[[TenantLineConfig], LineClient] | None = None,
                 gate_factory: Callable[[TenantLineConfig, LineClient], PushGate] | None = None,
                 handlers: Iterable[Handler] = (), background: bool = False, persist_messages: bool = True,
                 now: Callable[[], datetime.datetime] = datetime.datetime.now):
        self.credentials = credentials
        self.store = store
        self.client_factory = client_factory or (lambda cfg: LineClient(cfg.channel_access_token))
        # 共用通知帳號要換掉推播預算（整個帳號一本帳），所以推播閘怎麼建可以由外面給
        self.gate_factory: Callable[[TenantLineConfig, LineClient], PushGate] = gate_factory or (
            lambda cfg, client: PushGate(self.store, cfg, client, now=self._now))
        self.handlers: list[Handler] = list(handlers)
        self.persist_messages = persist_messages
        self._now = now
        self._seen: dict[str, collections.OrderedDict] = {}
        self._seen_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="line-evt") if background else None

    def add_handler(self, handler: Handler) -> None:
        self.handlers.append(handler)

    # ── 入口 ──
    def handle(self, tenant_id: str, body: bytes, signature: str) -> tuple[int, str]:
        try:
            validate_tenant_id(tenant_id)
        except ValueError:
            return 404, "not found"
        if is_reserved_tenant(tenant_id):
            return 404, "not found"
        cfg = self.credentials.get(tenant_id)
        if cfg is None:
            return 404, "not found"
        if len(body) > MAX_BODY:
            return 413, "too large"
        if not verify(cfg.channel_secret, body, signature):
            self.store.append_jsonl(tenant_id, "line", "webhook_audit.jsonl", record={
                "ts": self._now().isoformat(timespec="seconds"), "ev": "bad_signature", "bytes": len(body)})
            return 403, "bad signature"
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, "bad json"
        self.ingest(tenant_id, cfg, [e for e in (data.get("events") or []) if isinstance(e, dict)])
        return 200, "ok"         # LINE 的「Verify」按鈕送的是空 events，也要回 200

    def ingest(self, tenant_id: str, cfg: TenantLineConfig, events: list[dict]) -> None:
        """已驗簽、已確定屬於哪個租戶的事件：去重 → 落檔 → 處理。共用通知帳號也從這裡進。"""
        fresh = [e for e in events if not self._dup(tenant_id, e)]
        for ev in fresh:
            self._persist(tenant_id, ev)
        if fresh:
            if self._pool:
                self._pool.submit(self._process, tenant_id, cfg, fresh)
            else:
                self._process(tenant_id, cfg, fresh)

    # ── 落檔 ──
    def _dup(self, tenant_id: str, ev: dict) -> bool:
        """LINE 會重送（isRedelivery）；用 webhookEventId 擋重複，免得同一則被處理兩次。"""
        eid = ev.get("webhookEventId")
        if not eid:
            return False
        with self._seen_lock:
            seen = self._seen.setdefault(tenant_id, collections.OrderedDict())
            if eid in seen:
                return True
            seen[eid] = True
            while len(seen) > 5000:
                seen.popitem(last=False)
        return False

    def _persist(self, tenant_id: str, ev: dict) -> None:
        if not self.persist_messages:
            return
        now = self._now()
        rec = {k: v for k, v in ev.items() if k != "replyToken"}     # reply token 一次性，不落檔
        rec["_received"] = now.isoformat(timespec="seconds")
        self.store.append_jsonl(tenant_id, "line", "events", now.strftime("%Y-%m-%d") + ".jsonl", record=rec)
        msg = ev.get("message") or {}
        src = ev.get("source") or {}
        sid = src.get("groupId") or src.get("roomId") or src.get("userId")
        if ev.get("type") == "message" and msg.get("type") == "text" and sid:
            # 每個對話一個檔：之後抽客需、回話追蹤都讀這裡
            self.store.append_jsonl(tenant_id, "line", "messages", sid + ".jsonl", record={
                "t": now.isoformat(timespec="seconds"), "uid": src.get("userId", ""), "text": msg.get("text", "")})

    # ── 處理 ──
    def _process(self, tenant_id: str, cfg: TenantLineConfig, events: list[dict]) -> list[EventContext]:
        client = self.client_factory(cfg)
        gate = self.gate_factory(cfg, client)
        out = []
        groups.apply_events(self.store, tenant_id, events)
        for ev in events:
            ctx = EventContext(tenant_id, cfg, ev, client, gate, self.store)
            try:
                self._builtin(ctx)
                for h in self.handlers:
                    if ctx.replied:
                        break
                    h(ctx)
                self._repay_by_reply(ctx)
            except Exception as e:           # 一筆壞掉不能拖垮同批其他事件
                ctx.notes.append(f"{type(e).__name__}: {str(e)[:120]}")
                if not ctx.replied and ev.get("replyToken"):
                    try:
                        ctx.reply_text("剛剛這一步出了點問題（已記錄）。請再試一次，或打「取消」重來。")
                    except Exception:
                        pass
                self.store.append_jsonl(tenant_id, "line", "webhook_audit.jsonl", record={
                    "ts": self._now().isoformat(timespec="seconds"), "ev": "handler_error",
                    "type": ev.get("type"), "err": f"{type(e).__name__}: {str(e)[:120]}"})
            out.append(ctx)
        return out

    def _builtin(self, ctx: EventContext) -> None:
        ev = ctx.event
        if ev.get("type") == "postback":
            data = parse_postback((ev.get("postback") or {}).get("data", ""))
            action, item_id = data.get("gate"), data.get("id", "")
            if action in ("approve", "always"):
                res = ctx.gate.approve(item_id, actor=ctx.user_id, always=(action == "always"))
                if res.state == "sent":
                    ctx.reply_text("✅ 已送出")
                elif res.state == "deferred":
                    ctx.reply_text("⏳ 已核准，但" + res.reason)
                else:
                    ctx.reply_text(("⛔ " if res.state == "refused" else "⚠ ") + res.reason)
            elif action == "reject":
                res = ctx.gate.reject(item_id, actor=ctx.user_id)
                ctx.reply_text("🗑 不送了" if res.state == "rejected" else "⚠ " + res.reason)
            return
        if ev.get("type") != "message" or not ctx.text:
            return
        cmd = ctx.text.lower()
        if cmd in CMD_PING:
            ctx.reply_text(f"收到 ✅ 這個官方帳號已經接上{brand.NAME}。")
        elif ctx.is_owner_dm and cmd in CMD_PENDING:
            cards = ctx.gate.review_messages(limit=5)
            ctx.reply(cards or [_text("目前沒有等你審核的推播 👍")])
        elif ctx.is_owner_dm and cmd in CMD_QUOTA:
            b = ctx.gate.budget
            s = b.summary()
            if b.policy.recipient_monthly_cap:
                mine = b.recipient_used(ctx.user_id)
                ctx.reply_text(f"你本月的主動通知：{mine}/{b.policy.recipient_monthly_cap} 則\n"
                               "用完後打「新案」一樣看得到今天的配對（回覆不計額度）。")
                return
            lines = [f"本月推播 {s['used']}/{s['plan_cap']}（剩 {s['remaining']}）"]
            for lv, v in s["levels"].items():
                cap = v.get("quota", v.get("daily_cap"))
                unit = "／月" if "quota" in v else "／日"
                used = v["used_month"] if "quota" in v else v["used_today"]
                lines.append(f"・{v['desc']}：{used}/{cap}{unit}")
            lines.append("回覆不計額度：有事直接打字問我就好。")
            ctx.reply_text("\n".join(lines))

    def _repay_by_reply(self, ctx: EventContext) -> None:
        """對方剛開口、我們欠他已核准的訊息 → 這次回覆順便補上。"""
        if ctx.replied or ctx.event.get("type") != "message" or not ctx.event.get("replyToken"):
            return
        owed = ctx.gate.take_for_reply(ctx.source_id, limit=5)
        if owed:
            ctx.reply(owed)

    # ── WSGI（任何 WSGI 伺服器都能掛；web/ 的 FastAPI 也可直接呼叫 handle()）──
    def wsgi(self, environ: dict, start_response: Callable) -> list[bytes]:
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")
        if method == "GET" and path in ("/healthz", "/"):
            code, msg = 200, "ok"
        elif method == "POST" and path.startswith(WEBHOOK_PREFIX):
            tenant = path[len(WEBHOOK_PREFIX):].strip("/")
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            if n > MAX_BODY:
                code, msg = 413, "too large"
            else:
                body = environ["wsgi.input"].read(n) if n else b""
                code, msg = self.handle(tenant, body, environ.get("HTTP_X_LINE_SIGNATURE", ""))
        else:
            code, msg = 404, "not found"
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large"}
        b = msg.encode("utf-8")
        start_response(f"{code} {reason.get(code, 'Error')}",
                       [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(b)))])
        return [b]


def main(argv: list[str] | None = None) -> int:
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    ap = argparse.ArgumentParser(description="開發用 webhook 伺服器（正式環境請掛在 WSGI／ASGI 伺服器後面）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data-dir", default=None)
    a = ap.parse_args(argv)
    store = TenantStore(a.data_dir)
    app = WebhookApp(FileCredentialStore(store), store, background=True)

    class _Threading(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    with make_server(a.host, a.port, app.wsgi, server_class=_Threading) as srv:
        print(f"webhook 在 http://{a.host}:{a.port}/webhook/<租戶id>（資料：{store.root}）")
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
