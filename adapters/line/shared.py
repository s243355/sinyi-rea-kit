# -*- coding: utf-8 -*-
"""共用通知帳號：一個官方帳號服務所有仲介——仲介不用自己去申請官方帳號。

仲介這邊只有三步：
  1. 加官方帳號好友（收到歡迎卡；回覆不計額度）
  2. 按「同意並開始」→ 自動開一個自己的資料夾（負責人 2026-10-04：「要上線了，給大家使用」，不再要綁定碼）；
     租戶代號由 LINE 帳號雜湊而來：封鎖後再加回來、刪除後再開始，都是同一個（試用期不會重算）。
     舊的 6 碼綁定碼還是可以用（管理員搬資料用）。每天新加入有上限（REA_SIGNUP_DAILY_CAP），防灌爆額度。
  3. 之後新案配對只通知他本人；卡片上有「分享給客戶」，由他用自己的 LINE 轉給客人
     （客人看到的是仲介本人傳的，比機器人推更有信任感；也因此不需要審核閘）
  隨時打「新案」→ 用回覆拿今天的配對（不計額度）；額度用完的通知，他下次開口時也用回覆補上。

額度：整個帳號一本帳（系統分區 sys-line-shared），上限＝這個帳號的 LINE 方案；
每位仲介有公平份額（per_agent_monthly），一個人用不光大家的額度。帳本只存收件人雜湊。
資料：每位仲介的事件、訊息、待送佇列仍落在他自己的租戶資料夾；系統分區只有綁定表與帳本。

客戶群：共用帳號被拉進群組時，先說明後自動退出、不落檔。
「機器人進客戶群自動整理客需」要等產品負責人決定（要對客人告知），在那之前不收群組訊息。

開發機：
    set REA_SHARED_LINE_SECRET=...   set REA_SHARED_LINE_TOKEN=...
    python -m adapters.line.shared --port 8080      → webhook 網址填 https://<網域>/line-shared/webhook
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

from adapters.line.client import LineClient
from adapters.line.onboarding import welcome
from adapters.line.credentials import MemoryCredentialStore, TenantLineConfig, is_line_id
from adapters.line.push_budget import BudgetPolicy, PushBudget
from adapters.line.push_gate import GatePolicy, GateResult, PushGate
from adapters.line.signature import verify
from adapters.line.webhook import MAX_BODY, Handler, WebhookApp
from adapters.storage import TenantStore, is_reserved_tenant, validate_tenant_id

SYS_TENANT = "sys-line-shared"
SECRET_ENV = "REA_SHARED_LINE_SECRET"
TOKEN_ENV = "REA_SHARED_LINE_TOKEN"
WEBHOOK_PATH = "/line-shared/webhook"

BIND_RE = re.compile(r"^綁定\s*(\d{6})$")
BARE_CODE_RE = re.compile(r"^\d{6}$")
UNBIND = {"解除綁定", "取消綁定"}
MAX_BIND_FAILS = 5            # 一小時內輸錯幾次就先擋（6 碼約百萬種，不能讓人一直猜）

SIGNUP_WORDS = {"同意並開始", "開始使用"}
TERMS_VERSION = "2026-10-04"           # 服務條款與個資委託處理條款的版本（同意紀錄存在仲介自己的資料夾）
SIGNUP_CAP = int(os.environ.get("REA_SIGNUP_DAILY_CAP", "50"))
# 誰可以按「同意並開始」自己加入：invite＝只有管理員（以及還沒設管理員時的第一個人）；其他人要管理員發的綁定碼
# open＝任何加好友的人（共用通知帳號給很多仲介用時才開）。自架預設 invite：避免陌生人用掉你的推播與搜尋額度。
SIGNUP_MODE_ENV = "REA_SIGNUP_MODE"
INVITE_ONLY = ("這個帳號目前只開放受邀的人使用 🙏\n"
               "請向管理員索取 6 位數綁定碼，直接傳給我就能開始。")


def signup_mode(env=os.environ) -> str:
    return "open" if env.get(SIGNUP_MODE_ENV, "").strip().lower() == "open" else "invite"
QUOTA = ("line", "quota.json")         # LINE 官方帳號的月額度（每天自動向 LINE 查；升級方案後自動跟上）
GROUP_BYE = ("謝謝邀請 🙏 通知帳號目前只服務一對一：新物件會私訊通知業務本人，"
             "再由業務分享給大家。我先離開群組，不會留下任何群組訊息。")


@dataclass(frozen=True)
class SharedChannelConfig:
    channel_secret: str = field(repr=False)
    channel_access_token: str = field(repr=False)
    plan_cap: int = 200             # 這個官方帳號的 LINE 方案月額度（免費方案 200）
    per_agent_monthly: int = 90     # 每位仲介每月最多收幾則主動通知（報告 1＋加報最多 2／天；人多了要升方案或調低）
    per_agent_daily: int = 3

    @classmethod
    def from_env(cls, **kw) -> "SharedChannelConfig":
        secret, token = os.environ.get(SECRET_ENV, ""), os.environ.get(TOKEN_ENV, "")
        if not secret or not token:
            raise RuntimeError(f"缺環境變數 {SECRET_ENV}／{TOKEN_ENV}")
        return cls(secret, token, **kw)

    def budget_policy(self) -> BudgetPolicy:
        # 只用「match」一級：收件人都是仲介本人；每人每天最多 3 則（報告＋兩次加報）、每月公平份額
        return BudgetPolicy(plan_cap=self.plan_cap, control_reserve=0,
                            level_quota={"match": self.plan_cap, "greeting": 0, "announce": 0, "internal": 0},
                            control_daily={"owner": 0, "review": 0}, recipient_daily={"match": self.per_agent_daily},
                            recipient_monthly={},          # 收件人是仲介本人（每天的今日報告），不套客戶每月 4 則
                            recipient_monthly_cap=self.per_agent_monthly)


class BindingRegistry:
    """LINE 使用者 ↔ 租戶。一個租戶（一個人）綁一個 LINE 帳號；換手機或換帳號就重新綁。"""
    CODES = ("bind_codes.json",)
    BINDINGS = ("bindings.json",)
    FAILS = ("bind_fails.json",)
    SIGNUPS = ("signups.json",)
    TENANT_SIDE = ("line", "shared_binding.json")
    CONSENT = ("line", "consent.json")

    def __init__(self, store: TenantStore, now: Callable[[], datetime.datetime] = datetime.datetime.now):
        self.store = store
        self._now = now

    def _read(self, parts: tuple[str, ...]) -> dict:
        return self.store.read_json(SYS_TENANT, *parts, default={}) or {}

    def _write(self, parts: tuple[str, ...], data: dict) -> None:
        self.store.write_json(SYS_TENANT, *parts, data=data)

    def issue_code(self, tenant_id: str, ttl_minutes: int = 15) -> str:
        validate_tenant_id(tenant_id)
        if is_reserved_tenant(tenant_id):
            raise ValueError("系統分區不能綁定")
        now = self._now()
        with self.store.lock(SYS_TENANT, "bindings"):
            codes = {c: v for c, v in self._read(self.CODES).items()
                     if v.get("exp", "") > now.isoformat() and v.get("tenant") != tenant_id}   # 同租戶舊碼作廢
            code = f"{secrets.randbelow(10 ** 6):06d}"
            while code in codes:
                code = f"{secrets.randbelow(10 ** 6):06d}"
            codes[code] = {"tenant": tenant_id,
                           "exp": (now + datetime.timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")}
            self._write(self.CODES, codes)
        return code

    def redeem(self, user_id: str, code: str) -> tuple[bool, str]:
        """回 (成功?, 給使用者看的話)。"""
        if not is_line_id(user_id) or not user_id.startswith("U"):
            return False, "只能在一對一聊天裡綁定"
        now = self._now()
        hour_ago = (now - datetime.timedelta(hours=1)).isoformat()
        with self.store.lock(SYS_TENANT, "bindings"):
            fails = self._read(self.FAILS)
            mine = [t for t in fails.get(user_id, []) if t > hour_ago]
            if len(mine) >= MAX_BIND_FAILS:
                return False, "綁定碼錯太多次了，請一小時後再試，或重新取得新的綁定碼。"
            codes = self._read(self.CODES)
            hit = codes.get(code)
            if not hit or hit.get("exp", "") <= now.isoformat():
                fails[user_id] = mine + [now.isoformat(timespec="seconds")]
                self._write(self.FAILS, fails)
                return False, "綁定碼不對或已過期（有效 15 分鐘），請重新取得。"
            tenant = hit["tenant"]
            del codes[code]
            self._write(self.CODES, codes)
            fails.pop(user_id, None)
            self._write(self.FAILS, fails)
            bindings = self._read(self.BINDINGS)
            for uid, t in list(bindings.items()):           # 一人一帳號：這個租戶舊的 LINE 帳號解除
                if t.get("tenant") == tenant and uid != user_id:
                    del bindings[uid]
            old = bindings.get(user_id, {}).get("tenant")
            ts = now.isoformat(timespec="seconds")
            bindings[user_id] = {"tenant": tenant, "at": ts}
            self._write(self.BINDINGS, bindings)
        if old and old != tenant:
            self._drop_tenant_side(old)
        self.store.write_json(tenant, *self.TENANT_SIDE, data={"user_id": user_id, "bound_at": ts})
        return True, "✅ 綁定完成！下面這張卡片可以直接點選開始。"

    @staticmethod
    def tenant_for_user(user_id: str) -> str:
        """自己加入的租戶代號：LINE 帳號的雜湊（同一個人永遠是同一個；資料夾名稱看不出 LINE 帳號）。"""
        return "a" + hashlib.sha256(("rea-kit signup:" + user_id).encode()).hexdigest()[:15]

    def signup(self, user_id: str, *, daily_cap: int = SIGNUP_CAP) -> tuple[str | None, str]:
        """按「同意並開始」：不用綁定碼就開始用。回 (租戶代號, 給使用者看的話)；不能開回 (None, 理由)。"""
        if not is_line_id(user_id) or not user_id.startswith("U"):
            return None, "只能在一對一聊天裡開始使用"
        now = self._now()
        ts, day = now.isoformat(timespec="seconds"), now.date().isoformat()
        tenant = self.tenant_for_user(user_id)
        with self.store.lock(SYS_TENANT, "bindings"):
            bindings = self._read(self.BINDINGS)
            if user_id in bindings:
                return bindings[user_id]["tenant"], "你已經在使用了，直接點下面的功能。"
            returning = self.store.tenant_dir(tenant).exists()
            book = self._read(self.SIGNUPS)
            if book.get("day") != day:
                book = {"day": day, "n": 0}
            if not returning and book["n"] >= daily_cap:
                return None, "今天新加入的名額滿了 🙏 明天再按一次「同意並開始」就可以。"
            for uid, t in list(bindings.items()):           # 一人一帳號
                if t.get("tenant") == tenant and uid != user_id:
                    del bindings[uid]
            bindings[user_id] = {"tenant": tenant, "at": ts, "self": True}
            self._write(self.BINDINGS, bindings)
            if not returning:
                book["n"] += 1
                self._write(self.SIGNUPS, book)
        self.store.write_json(tenant, *self.TENANT_SIDE, data={"user_id": user_id, "bound_at": ts})
        self.store.write_json(tenant, *self.CONSENT, data={"terms": TERMS_VERSION, "agreed_at": ts})
        return tenant, ("✅ 歡迎回來！資料都還在，直接點下面的功能。" if returning
                        else "✅ 開始使用！先按「開始新手體驗」，三步驟設定好，明天早上就會收到第一份今日報告。")

    def tenant_of(self, user_id: str) -> str | None:
        return (self._read(self.BINDINGS).get(user_id) or {}).get("tenant")

    def tenants(self) -> list[str]:
        """目前有綁定 LINE 的租戶（排程用）。"""
        return sorted({t.get("tenant") for t in self._read(self.BINDINGS).values() if t.get("tenant")})

    def user_of(self, tenant_id: str) -> str | None:
        rec = self.store.read_json(tenant_id, *self.TENANT_SIDE, default={}) or {}
        uid = rec.get("user_id")
        return uid if uid and self.tenant_of(uid) == tenant_id else None    # 兩邊對得上才算

    def unbind_user(self, user_id: str) -> str | None:
        with self.store.lock(SYS_TENANT, "bindings"):
            bindings = self._read(self.BINDINGS)
            rec = bindings.pop(user_id, None)
            self._write(self.BINDINGS, bindings)
        if rec:
            self._drop_tenant_side(rec["tenant"])
            return rec["tenant"]
        return None

    def unbind_tenant(self, tenant_id: str) -> None:
        """刪除租戶時一定要呼叫：系統分區的綁定表不在租戶資料夾裡，delete_tenant 刪不到。"""
        with self.store.lock(SYS_TENANT, "bindings"):
            bindings = {u: t for u, t in self._read(self.BINDINGS).items() if t.get("tenant") != tenant_id}
            self._write(self.BINDINGS, bindings)
            codes = {c: v for c, v in self._read(self.CODES).items() if v.get("tenant") != tenant_id}
            self._write(self.CODES, codes)
        self._drop_tenant_side(tenant_id)

    def _drop_tenant_side(self, tenant_id: str) -> None:
        p = self.store.path(tenant_id, *self.TENANT_SIDE)
        if p.exists():
            p.unlink()


class SharedLineApp:
    def __init__(self, store: TenantStore, channel: SharedChannelConfig, registry: BindingRegistry | None = None, *,
                 client_factory: Callable[[str], LineClient] | None = None, handlers: Iterable[Handler] = (),
                 background: bool = False, now: Callable[[], datetime.datetime] = datetime.datetime.now):
        self.store = store
        self.channel = channel
        self.registry = registry or BindingRegistry(store, now=now)
        self._now = now
        self._client_factory = client_factory or (lambda token: LineClient(token))
        self._client = None
        self._tenant_locks: dict[str, threading.Lock] = {}
        self._tenant_locks_guard = threading.Lock()
        # 每位仲介的事件仍走一般 webhook 的落檔與處理（落在他自己的租戶資料夾）
        self.inner = WebhookApp(MemoryCredentialStore(), store, client_factory=lambda cfg: self.client,
                                gate_factory=self.gate_for, handlers=handlers, background=background, now=now)

    @property
    def client(self) -> LineClient:
        if self._client is None:
            self._client = self._client_factory(self.channel.channel_access_token)
        return self._client

    def plan_cap(self) -> int:
        """LINE 官方帳號這個月的額度：每天向 LINE 查一次存起來（升級方案後自動跟上）；沒查過就用設定值。"""
        v = (self.store.read_json(SYS_TENANT, *QUOTA, default={}) or {}).get("value")
        return int(v) if isinstance(v, int) and v > 0 else self.channel.plan_cap

    def budget(self) -> PushBudget:
        channel = dataclasses.replace(self.channel, plan_cap=self.plan_cap())
        return PushBudget(self.store, SYS_TENANT, channel.budget_policy(), now=self._now)

    def sync_quota(self) -> int | None:
        """向 LINE 查月額度（只讀）。沒上限的方案記成很大的數。查不到回 None、維持原本的。"""
        data = self.client.quota()
        if not isinstance(data, dict):
            return None
        if data.get("type") == "none":
            value = 1_000_000
        elif data.get("type") == "limited" and isinstance(data.get("value"), int) and data["value"] > 0:
            value = data["value"]
        else:
            return None
        self.store.write_json(SYS_TENANT, *QUOTA, data={"value": value, "checked": self._now().isoformat(timespec="seconds")})
        return value

    def config_for(self, tenant_id: str, user_id: str) -> TenantLineConfig:
        return TenantLineConfig(tenant_id, self.channel.channel_secret, self.channel.channel_access_token,
                                owner_user_id=user_id)

    def gate_for(self, cfg: TenantLineConfig, client: LineClient) -> PushGate:
        # 收件人只會是仲介本人（控制通道）→ 不審核；佇列落租戶資料夾，帳本用整個帳號那一本
        return PushGate(self.store, cfg, client, budget=self.budget(),
                        policy=GatePolicy.from_dict({"modes": {"match": "auto"}}), now=self._now)

    def may_sign_up(self, user_id: str) -> bool:
        """invite 模式：管理員自己、或還沒設任何管理員（第一次設定）時才能自己加入。"""
        if (getattr(self, "signup_mode", None) or signup_mode()) == "open":
            return True
        from web.admin_app import admins
        current = admins()
        return not current or user_id in current

    # ── 對內：工作排程用來通知仲介 ──
    def notify(self, tenant_id: str, messages: list[dict], *, kind: str = "agent_digest",
               keep_payload: bool = True) -> GateResult:
        user = self.registry.user_of(tenant_id)
        if not user:
            return GateResult("skipped", reason="這位仲介還沒綁定通知帳號")
        gate = self.gate_for(self.config_for(tenant_id, user), self.client)
        return gate.submit(to=user, messages=messages[:5], level="match", kind=kind, recipient_label="你本人",
                           keep_payload=keep_payload)

    # ── webhook ──
    def handle(self, body: bytes, signature: str) -> tuple[int, str]:
        if len(body) > MAX_BODY:
            return 413, "too large"
        if not verify(self.channel.channel_secret, body, signature):
            self._bad_signature()
            return 403, "bad signature"
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, "bad json"
        if not isinstance(data, dict) or not isinstance(data.get("events", []), list):
            return 400, "bad events"
        for ev in data.get("events") or []:
            if isinstance(ev, dict):
                try:
                    self._route(ev)
                except Exception as e:          # 一筆壞掉不拖垮整批
                    self._audit("route_error", type=ev.get("type"), err=f"{type(e).__name__}: {str(e)[:120]}")
        return 200, "ok"

    def _route(self, ev: dict) -> None:
        src = ev.get("source") or {}
        token = ev.get("replyToken")
        reply = lambda text: self.client.reply(token, [{"type": "text", "text": text}]) if token else None
        if src.get("type") in ("group", "room"):
            if ev.get("type") == "join":
                reply(GROUP_BYE)
                self.client.leave_chat(src.get("groupId") or src.get("roomId") or "")
                self._audit("left_group")
            return                                  # 群組訊息一律不落檔（見檔頭）
        user = src.get("userId", "")
        msg = ev.get("message") or {}
        text = str(msg.get("text") or "").strip() if ev.get("type") == "message" and msg.get("type") == "text" else ""
        m = BIND_RE.match(text)
        code = m.group(1) if m else None
        if code is None and BARE_CODE_RE.match(text) and not self.registry.tenant_of(user):
            code = text          # 還沒綁定的人直接輸入 6 位數也算（已綁定的人輸入數字可能是在回答題目，不當成綁定）
        if code:
            ok, words = self.registry.redeem(user, code)
            if ok and token:
                self.client.reply(token, [{"type": "text", "text": words}, welcome(bound=True)])
            else:
                reply(words)
            self._audit("bind_ok" if ok else "bind_fail")
            return
        tenant = self.registry.tenant_of(user)
        # 管理員面板：管理員（設定檔裡的 LINE 帳號）不管綁在哪個帳號、有沒有綁，都能拿登入連結
        if text and token and getattr(self, "admin_link", None):
            from web.admin_app import ADMIN_WORDS, admins, login_card
            if text in ADMIN_WORDS and user in admins():
                url = self.admin_link(user)
                if url:
                    self.client.reply(token, [login_card(url)])
                    self._audit("admin_link")
                    return
        if ev.get("type") == "unfollow":
            if tenant:
                self.registry.unbind_user(user)
                self._audit("unbound_by_block")
            return
        if not tenant:
            if text in SIGNUP_WORDS and not self.may_sign_up(user):
                reply(INVITE_ONLY)
                self._audit("signup_invite_only")
            elif text in SIGNUP_WORDS:
                tenant, words = self.registry.signup(user, daily_cap=getattr(self, "signup_cap", SIGNUP_CAP))
                if tenant and token:
                    self.client.reply(token, [{"type": "text", "text": words}, welcome(bound=True)])
                else:
                    reply(words)
                self._audit("signup_ok" if tenant else "signup_refused")
            elif ev.get("type") in ("follow", "message") and token:
                self.client.reply(token, [welcome(terms_url=getattr(self, "terms_url", None))])
            return                                  # 還沒開始用的人：不落檔、不處理
        if text in UNBIND:
            self.registry.unbind_user(user)
            reply("已解除綁定，之後不會再通知你。要再使用，按「同意並開始」就好（資料還在）。")
            return
        if ev.get("type") == "follow":
            if token:
                self.client.reply(token, [welcome(bound=True)])
            return
        with self._tenant_lock(tenant):             # 同一位仲介的訊息一次處理一則（服務有多條執行緒，草稿檔不會互相蓋掉）
            self.inner.ingest(tenant, self.config_for(tenant, user), [ev])

    def _bad_signature(self) -> None:
        """壞簽章每分鐘最多落一筆（count＝這一分鐘幾次）；公開網址被亂打時不會把硬碟寫滿。"""
        minute = self._now().strftime("%Y-%m-%dT%H:%M")
        with self._tenant_locks_guard:
            last, n = getattr(self, "_badsig", (None, 0))
            self._badsig = (minute, n + 1 if last == minute else 1)
            first = last != minute
        if first:
            self._audit("bad_signature", minute=minute)

    def _tenant_lock(self, tenant_id: str):
        with self._tenant_locks_guard:
            return self._tenant_locks.setdefault(tenant_id, threading.Lock())

    def _audit(self, ev: str, **kw) -> None:
        # 系統分區只記事件種類，不記誰、不記內容
        self.store.append_jsonl(SYS_TENANT, "audit.jsonl",
                                record={"ts": self._now().isoformat(timespec="seconds"), "ev": ev, **kw})

    def wsgi(self, environ: dict, start_response: Callable, fallback: Callable | None = None) -> list[bytes]:
        if environ.get("PATH_INFO") == WEBHOOK_PATH and environ.get("REQUEST_METHOD") == "POST":
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = -1
            if n < 0:
                start_response("400 Bad Request", [("Content-Type", "text/plain")])
                return [b"bad length"]
            if n > MAX_BODY:
                code, msg = 413, "too large"
            else:
                body = environ["wsgi.input"].read(n) if n else b""
                code, msg = self.handle(body, environ.get("HTTP_X_LINE_SIGNATURE", ""))
            b = msg.encode("utf-8")
            reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 413: "Payload Too Large"}
            start_response(f"{code} {reason.get(code, 'Error')}",
                           [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(b)))])
            return [b]
        if fallback:
            return fallback(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "9")])
        return [b"not found"]


def main(argv: list[str] | None = None) -> int:
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    ap = argparse.ArgumentParser(description="開發用：共用通知帳號 webhook")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data-dir", default=None)
    a = ap.parse_args(argv)
    store = TenantStore(a.data_dir)
    app = SharedLineApp(store, SharedChannelConfig.from_env(), background=True)

    class _Threading(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    with make_server(a.host, a.port, app.wsgi, server_class=_Threading) as srv:
        print(f"共用通知帳號 webhook：http://{a.host}:{a.port}{WEBHOOK_PATH}（資料：{store.root}）")
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
