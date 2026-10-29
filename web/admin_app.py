# -*- coding: utf-8 -*-
"""管理員面板 /admin（負責人 2026-10-09：「要像我另一套營運系統那樣，有一個管理員面板」）。

登入不用密碼：管理員在 LINE 對官方帳號點「管理員面板」→ 回覆一個 10 分鐘內有效、只能用一次的登入連結
→ 開了就記住這台手機／電腦 30 天。管理員＝執行資料夾設定的 LINE 帳號（REA_ADMIN_USERS），
跟他的 LINE 綁在哪個仲介帳號無關，所以他的 LINE 可以當一般客人體驗，面板一鍵接回。

看得到：仲介名單（加入時間、方案、買方數、物件數、海巡地區、最後一份報告）、今天新加入、LINE 推播額度、
搜尋用量、海巡候選與判斷（公開貼文）、全市場盤點。看不到：客人的暱稱與需求內容（只給數量）。
動作（都要登入、帶表單驗證碼）：發綁定碼、把我的 LINE 接到某個帳號、把我的 LINE 改成新客人體驗。
只用標準庫、伺服器產生頁面、不放外部資源、不讓搜尋引擎收錄。
"""
from __future__ import annotations

import datetime
import html
import os
import secrets
import urllib.parse
from collections import Counter
from http.cookies import SimpleCookie

from adapters import team
from core import brand

SYS = "sys-line-shared"
TOKENS = ("admin", "login_tokens.json")
SESSIONS = ("admin", "sessions.json")
LINK_TTL = datetime.timedelta(minutes=10)
SESSION_TTL = datetime.timedelta(days=30)
COOKIE = "rk_admin"
ADMIN_WORDS = {"管理員面板", "管理員", "後台", "管理後台"}


def _signup_mode():
    from adapters.line.shared import signup_mode
    return signup_mode()


def admins(env=os.environ) -> set[str]:
    return {u.strip() for u in env.get("REA_ADMIN_USERS", "").split(",") if u.strip().startswith("U")}


class AdminAuth:
    def __init__(self, store, now=datetime.datetime.now):
        self.store, self.now = store, now

    def _rw(self, parts, fn):
        with self.store.lock(SYS, "admin-auth"):
            data = self.store.read_json(SYS, *parts, default={}) or {}
            now = self.now().isoformat(timespec="seconds")
            data = {k: v for k, v in data.items() if v.get("exp", "") > now}
            out = fn(data)
            self.store.write_json(SYS, *parts, data=data)
            return out

    def issue_link(self, user_id: str) -> str:
        token = secrets.token_urlsafe(24)
        exp = (self.now() + LINK_TTL).isoformat(timespec="seconds")
        self._rw(TOKENS, lambda d: d.__setitem__(token, {"user": user_id, "exp": exp}))
        return token

    def redeem(self, token: str) -> str | None:
        rec = self._rw(TOKENS, lambda d: d.pop(token, None))
        if not rec:
            return None
        sid = secrets.token_urlsafe(32)
        exp = (self.now() + SESSION_TTL).isoformat(timespec="seconds")
        self._rw(SESSIONS, lambda d: d.__setitem__(sid, {"user": rec["user"], "exp": exp,
                                                         "csrf": secrets.token_urlsafe(16)}))
        return sid

    def session(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        return self._rw(SESSIONS, lambda d: d.get(sid))

    def logout(self, sid: str) -> None:
        self._rw(SESSIONS, lambda d: d.pop(sid, None))


# ── 資料 ───────────────────────────────────────────────────────────────────

def _tenants(app) -> list[str]:
    store = app.store
    root = store.tenant_dir(SYS).parent
    ids = {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("sys-")} if root.exists() else set()
    return sorted(ids | set(app.registry.tenants()))


def _plan_label(st: dict) -> str:
    if st["exempt"]:
        return "管理員（不限）"
    if st["paid"]:
        return f"{st['tier'].name}方案（到 {st['paid_until']:%m/%d}）" + ("・自動扣款" if st["auto"] else "")
    if st.get("open"):
        return "免費開放期"
    return "試用中" if st["trial"] else "免費版"


def _areas(prospect: dict | None) -> str:
    p = prospect or {}
    out = []
    for county, ds in (p.get("areas") or {}).items():
        ds = [d for d in ds or [] if d != "*"]
        out.append(county + ("（" + "、".join(ds[:4]) + ("…" if len(ds) > 4 else "") + "）" if ds else "全區"))
    return "、".join(out) or "—"


def tenant_rows(app) -> list[dict]:
    store, reg = app.store, app.registry
    binds = store.read_json(SYS, "bindings.json", default={}) or {}
    bound = {v.get("tenant"): v for v in binds.values()}
    rows = []
    for t in _tenants(app):
        b = bound.get(t) or {}
        prospect = (store.read_json(t, "line", "prospect_setup.json", default={}) or {}).get("profile")
        profile = (store.read_json(t, "line", "agent_profile.json", default={}) or {}).get("profile") or {}
        run = store.read_json(t, "jobs", "daily_brief_runs.json", default={}) or {}
        try:
            n_cust = len(app.customers.list_customers(t, active_only=False))
        except Exception:
            n_cust = 0
        try:
            n_list = len(app.listings.list_all(t)) if getattr(app, "listings", None) else 0
        except Exception:
            n_list = 0
        roster = store.read_json(team.TEAM_TENANT, *team.ROSTER, default={}) or {}
        rows.append({"tenant": t, "name": roster.get(t) or "", "bound": bool(b), "joined": (b.get("at") or "")[:16].replace("T", " "),
                     "self_signup": bool(b.get("self")), "plan": _plan_label(app.plans.status(t)) if app.plans else "—",
                     "customers": n_cust, "listings": n_list, "areas": _areas(prospect),
                     "brand": profile.get("brand") or "", "signature": profile.get("signature") or "",
                     "last_run": (run.get("at") or "")[:16].replace("T", " "), "last_posts": run.get("posts"),
                     "last_listings": run.get("listings"), "last_push": run.get("push"),
                     "consent": (store.read_json(t, "line", "consent.json", default={}) or {}).get("terms", "")})
    rows.sort(key=lambda r: r["joined"] or "", reverse=True)
    return rows


def overview(app) -> dict:
    store = app.store
    now = app._now() if hasattr(app, "_now") else datetime.datetime.now()
    rows = tenant_rows(app)
    signups = store.read_json(SYS, "signups.json", default={}) or {}
    usage = store.read_json(SYS, "search", "usage.json", default={}) or {}
    budget = app.budget().summary()
    posts = store.read_jsonl(SYS, "patrol", "posts.jsonl") if store.path(SYS, "patrol", "posts.jsonl").exists() else []
    week = (now - datetime.timedelta(days=7)).isoformat()
    recent = [p for p in posts if (p.get("first_seen") or "") >= week]
    pool = store.read_json(SYS, "market", "pool.json", default={}) or {}
    today = now.date().isoformat()
    return {"tenants": len(rows), "bound": sum(r["bound"] for r in rows),
            "signups_today": signups.get("n", 0) if signups.get("day") == today else 0,
            "signup_cap": getattr(app, "signup_cap", None),
            "push_used": budget["used"], "push_cap": budget["plan_cap"],
            "search_today": usage.get("total", 0) if usage.get("day") == today else 0,
            "search_cap": getattr(app.search, "daily_cap", None), "search_off": usage.get("off"),
            "patrol_week": len(recent), "patrol_kept": sum(1 for p in recent if p.get("keep")),
            "market_today": sum(1 for v in pool.values() if v.get("first_seen") == today), "market_pool": len(pool),
            "signup_mode": ("任何人" if (getattr(app, "signup_mode", None) or _signup_mode()) == "open" else "只限受邀")}


# ── 頁面 ───────────────────────────────────────────────────────────────────

# 頁尾署名：本版改自開源 rea-kit（完整署名見 NOTICE，轉散布時 NOTICE 檔要保留）
CUSTOM_AD_HTML = "<p class='custom-ad'>" + html.escape(brand.ATTRIBUTION) + "</p>"

STYLE = """
:root{--bg:#f5f5f7;--card:#fff;--ink:#1d1d1f;--sub:#6e6e73;--line:#e5e5ea;--brand:""" + brand.GREEN + """;--tint:""" + brand.TINT + """;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--ink:#f5f5f7;--sub:#a1a1a6;--line:#2c2c2e;--tint:#0b3a20}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang TC","Microsoft JhengHei",sans-serif;line-height:1.5}
header{position:sticky;top:0;z-index:2;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(18px);
border-bottom:1px solid var(--line);padding:14px 16px 0}
h1{font-size:20px;margin:0 0 10px;font-weight:700}nav{display:flex;gap:6px;overflow-x:auto;padding-bottom:10px}
nav a{white-space:nowrap;padding:7px 14px;border-radius:999px;color:var(--ink);text-decoration:none;font-size:15px;background:var(--card);
border:1px solid var(--line)}nav a.on{background:var(--brand);color:#fff;border-color:var(--brand)}
main{max-width:1100px;margin:0 auto;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:18px}
.tile{background:var(--card);border-radius:16px;padding:14px 16px;border:1px solid var(--line)}
.tile b{display:block;font-size:26px;font-weight:700}.tile span{color:var(--sub);font-size:13px}
.card{background:var(--card);border-radius:16px;border:1px solid var(--line);margin-bottom:16px;overflow:hidden}
.card h2{font-size:16px;margin:0;padding:14px 16px;border-bottom:1px solid var(--line)}
.row{padding:12px 16px;border-bottom:1px solid var(--line)}.row:last-child{border-bottom:0}
.row .t{font-weight:600}.row .s{color:var(--sub);font-size:13px}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--tint);color:var(--brand);margin-right:4px}
.pill.no{background:#fde7e7;color:#b42318}.warn{color:var(--warn)}
a{color:var(--brand)}form{display:inline}button{font:inherit;border:0;border-radius:10px;padding:8px 14px;
background:var(--brand);color:#fff;margin:4px 6px 4px 0;cursor:pointer}button.sec{background:var(--tint);color:var(--brand)}
.code{font-size:34px;font-weight:700;letter-spacing:6px}p.hint{color:var(--sub);font-size:13px;margin:6px 16px 14px}
.custom-ad{max-width:1100px;margin:8px auto 24px;padding:0 16px;color:var(--sub);font-size:12px;text-align:center;overflow-wrap:anywhere}.custom-ad a{color:inherit}
"""

TABS = (("", "總覽"), ("agents", "仲介"), ("patrol", "海巡"), ("market", "全市場"))


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _page(title: str, body: str, tab: str = "") -> bytes:
    nav = "".join(f"<a class='{'on' if k == tab else ''}' href='/admin/{k}'>{_e(n)}</a>" for k, n in TABS)
    doc = ("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>"
           f"<title>{_e(title)}｜{_e(brand.NAME)} 管理</title><style>{STYLE}</style></head><body>"
           f"<header><h1>{_e(brand.NAME)} 管理員面板</h1><nav>{nav}</nav></header><main>{body}</main>{CUSTOM_AD_HTML}</body></html>")
    return doc.encode("utf-8")


def _form(action: str, label: str, csrf: str, extra: dict | None = None, cls: str = "") -> str:
    hidden = "".join(f"<input type='hidden' name='{_e(k)}' value='{_e(v)}'>" for k, v in (extra or {}).items())
    return (f"<form method='post' action='/admin/act'><input type='hidden' name='csrf' value='{_e(csrf)}'>"
            f"<input type='hidden' name='do' value='{_e(action)}'>{hidden}<button class='{cls}'>{_e(label)}</button></form>")


def _name_form(action: str, label: str, csrf: str, extra: dict | None = None, value: str = "") -> str:
    """帶一格「姓名」的表單（邀請新成員、改顯示名稱）。"""
    hidden = "".join(f"<input type='hidden' name='{_e(k)}' value='{_e(v)}'>" for k, v in (extra or {}).items())
    return (f"<form method='post' action='/admin/act'><input type='hidden' name='csrf' value='{_e(csrf)}'>"
            f"<input type='hidden' name='do' value='{_e(action)}'>{hidden}"
            f"<input name='name' maxlength='12' placeholder='同仁姓名（例：家儒）' value='{_e(value)}'> "
            f"<button>{_e(label)}</button></form>")


def page_overview(app, sess) -> bytes:
    o = overview(app)
    tiles = [(o["tenants"], f"仲介帳號（綁定 LINE {o['bound']}）"),
             (o["signups_today"], f"今天新加入（{o['signup_mode']}，上限 {o['signup_cap']}）"),
             (f"{o['push_used']}/{o['push_cap']}", "本月 LINE 推播／額度"),
             (f"{o['search_today']}/{o['search_cap']}", "今天搜尋次數／上限"),
             (f"{o['patrol_kept']}/{o['patrol_week']}", "海巡 7 天：留下／看過"),
             (o["market_today"], f"今天全市場新物件（池 {o['market_pool']}）")]
    warn = []
    if o["push_cap"] and o["push_used"] >= o["push_cap"] * 0.8:
        warn.append("LINE 推播快用完了：到 LINE 官方帳號後台升級方案（升級後隔天自動跟上）。")
    if o["push_cap"] <= 200 and o["bound"] >= 5:
        warn.append(f"目前是免費 200 則方案，{o['bound']} 位仲介每天一則報告就會不夠。")
    if o["search_off"]:
        warn.append("搜尋服務今天停用：" + str(o["search_off"]))
    me = sess["user"]
    mine = app.registry.tenant_of(me)
    body = ("<div class='grid'>" + "".join(f"<div class='tile'><b>{_e(v)}</b><span>{_e(k)}</span></div>" for v, k in tiles)
            + "</div>")
    if warn:
        body += "<div class='card'><h2>要注意</h2>" + "".join(f"<div class='row warn'>{_e(w)}</div>" for w in warn) + "</div>"
    body += ("<div class='card'><h2>我的 LINE</h2><div class='row'><div class='t'>"
             + (f"現在綁在：{_e(mine)}" if mine else "現在沒有綁任何帳號（傳訊息會看到新客人的歡迎卡）")
             + "</div></div><div class='row'>"
             + _form("me_fresh", "改成新客人體驗", sess["csrf"], cls="sec")
             + "</div><p class='hint'>按了之後在 LINE 傳任何訊息，就會看到新客人的歡迎卡；在「仲介」頁可以一鍵接回任何帳號。</p></div>")
    return _page("總覽", body, "")


def page_agents(app, sess) -> bytes:
    rows = tenant_rows(app)
    me = app.registry.tenant_of(sess["user"])
    items = []
    for r in rows:
        pills = (("<span class='pill'>LINE 已綁</span>" if r["bound"] else "<span class='pill no'>未綁 LINE</span>")
                 + ("<span class='pill'>自己加入</span>" if r["self_signup"] else "")
                 + (f"<span class='pill'>{_e(r['plan'])}</span>")
                 + ("<span class='pill'>我的 LINE</span>" if r["tenant"] == me else ""))
        last = (f"最後報告 {r['last_run']}：貼文 {r['last_posts']}、配對 {r['last_listings']}、推播 {r['last_push']}"
                if r["last_run"] else "還沒有報告")
        who = "・".join(x for x in (r.get("name"), r["brand"], r["signature"]) if x)
        items.append(f"<div class='row'><div class='t'>{_e(who or r['tenant'])}</div>"
                     f"<div class='s'>{_e(r['tenant'])}・加入 {_e(r['joined'] or '—')}</div><div>{pills}</div>"
                     f"<div class='s'>買方 {r['customers']}・物件 {r['listings']}・海巡：{_e(r['areas'])}</div>"
                     f"<div class='s'>{_e(last)}</div><div>"
                     + _form("bind_me", "把我的 LINE 接到這個帳號", sess["csrf"], {"tenant": r["tenant"]}, "sec")
                     + _form("bind_code", "發綁定碼", sess["csrf"], {"tenant": r["tenant"]}, "sec")
                     + _name_form("rename", "改顯示名稱", sess["csrf"], {"tenant": r["tenant"]}, r.get("name") or "")
                     + "</div></div>")
    shared = team.is_shared()
    invite = ("<div class='row'>" + _name_form("invite", "＋ 邀請新成員", sess["csrf"])
              + "<p class='hint'>產生 6 位數綁定碼給新成員；他加好友後傳這組數字就能開始用。"
              + ("姓名會顯示在配對卡上（例：「王先生（家儒的客人）」），讓同仁知道要找誰。" if shared else "")
              + "</p></div>")
    body = (f"<div class='card'><h2>仲介 {len(rows)} 位</h2>" + invite + ("".join(items) or "<div class='row'>還沒有人</div>")
            + "<p class='hint'>只顯示數量，不顯示客人的暱稱與需求內容。</p></div>")
    return _page("仲介", body, "agents")


def page_patrol(app, sess, show_all: bool) -> bytes:
    store = app.store
    path = store.path(SYS, "patrol", "posts.jsonl")
    posts = store.read_jsonl(SYS, "patrol", "posts.jsonl") if path.exists() else []
    posts = sorted(posts, key=lambda p: p.get("first_seen") or "", reverse=True)[:300]
    reasons = Counter(p.get("reason") for p in posts)
    shown = posts if show_all else [p for p in posts if p.get("keep")]
    rows = "".join(
        f"<div class='row'><div class='t'>{'✅' if p.get('keep') else '✖'} {_e(p.get('reason'))}</div>"
        f"<div class='s'>@{_e(p.get('handle'))}・{_e((p.get('posted_at') or '')[:16].replace('T', ' '))}・"
        f"{_e(p.get('area'))}・{_e({'buy': '找買方', 'rent': '找租客', 'sell': '找屋主'}.get(p.get('intent'), ''))}</div>"
        f"<div class='s'>{_e((p.get('title') or '')[:80])}</div><a href='{_e(_safe_url(p.get('url')))}' target='_blank' rel='noopener'>開原文</a></div>"
        for p in shown[:120])
    stats = "".join(f"<div class='row s'>{_e(k)}：{v}</div>" for k, v in reasons.most_common(12))
    toggle = "<a href='/admin/patrol'>只看留下的</a>" if show_all else "<a href='/admin/patrol?all=1'>看全部（含擋掉的）</a>"
    body = (f"<div class='card'><h2>海巡候選（最近 {len(posts)} 篇）・{toggle}</h2>{rows or '<div class=row>沒有</div>'}</div>"
            f"<div class='card'><h2>判斷理由統計</h2>{stats}</div>")
    return _page("海巡", body, "patrol")


def page_market(app, sess) -> bytes:
    store = app.store
    scans = store.read_json(SYS, "market", "scans.json", default={}) or {}
    pool = store.read_json(SYS, "market", "pool.json", default={}) or {}
    rows = "".join(f"<div class='row'><div class='t'>{_e(k)}</div><div class='s'>最後盤點 {_e(v.get('day'))}・搜尋 {v.get('queries')} 次・"
                   f"找到 {v.get('found')}・新的 {v.get('new')}" + (f"・<span class='warn'>{_e(v.get('errors'))}</span>" if v.get("errors") else "")
                   + "</div></div>" for k, v in sorted(scans.items()))
    latest = max((v.get("first_seen", "") for v in pool.values()), default="")
    brands = Counter((v.get("brand") or v.get("site")) for v in pool.values() if v.get("first_seen") == latest)
    br = "".join(f"<span class='pill'>{_e(b)} {n}</span>" for b, n in brands.most_common())
    body = (f"<div class='card'><h2>各地區盤點</h2>{rows or '<div class=row>還沒盤點</div>'}</div>"
            f"<div class='card'><h2>{_e(latest)} 新出現的品牌分布</h2><div class='row'>{br or '—'}</div></div>")
    return _page("全市場", body, "market")


def page_message(title: str, text: str, extra: str = "") -> bytes:
    return _page(title, f"<div class='card'><h2>{_e(title)}</h2><div class='row'>{_e(text)}{extra}</div></div>")


def _safe_url(url) -> str:
    """外部來的網址只放 http(s)（擋 javascript: 這類連結）。"""
    u = str(url or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else "#"


# ── 動作 ───────────────────────────────────────────────────────────────────

def act(app, sess, form: dict) -> bytes:
    do, tenant, me = form.get("do", ""), form.get("tenant", ""), sess["user"]
    known = set(_tenants(app))
    if do == "bind_code" and tenant in known:
        code = app.registry.issue_code(tenant)
        return page_message("綁定碼", f"帳號 {tenant} 的綁定碼（15 分鐘內有效）：",
                            f"<div class='code'>{_e(code)}</div><p class='hint'>請那支 LINE 對官方帳號直接傳這 6 個數字。</p>")
    if do == "rename" and tenant in known:
        try:
            name = team.set_name(app.store, tenant, form.get("name", ""))
        except ValueError as e:
            return page_message("沒有改成", str(e), "<p><a href='/admin/agents'>回仲介頁</a></p>")
        return page_message("已改名", f"{tenant} 現在顯示為「{name}」。", "<p><a href='/admin/agents'>回仲介頁</a></p>")
    if do == "invite":
        name = (form.get("name") or "").strip()
        if name:
            try:
                team.clean_name(name)
            except ValueError as e:
                return page_message("沒有邀請", str(e), "<p><a href='/admin/agents'>回仲介頁</a></p>")
        new = "m" + secrets.token_hex(7)                       # 新成員自己的帳號（綁定、草稿、推播額度各自一份）
        if name:
            team.set_name(app.store, new, name)
        code = app.registry.issue_code(new)
        return page_message("邀請新成員", "請新成員用手機加官方帳號好友，然後直接傳這 6 個數字（15 分鐘內有效）：",
                            f"<div class='code'>{_e(code)}</div><p class='hint'>"
                            + ("全店共用模式：大家的買方與物件互相看得到、互相配對；只有建立的人和店長能修改。"
                               if team.is_shared() else "每位成員一個獨立帳號，客人與物件互相看不到。")
                            + "過期了回來再按一次「邀請新成員」。</p><p><a href='/admin/agents'>回仲介頁</a></p>")
    if do == "bind_me" and tenant in known:
        owner = app.registry.user_of(tenant)
        if owner and owner != me:                              # 不能把正在使用的仲介踢掉（一個帳號只綁一支 LINE）
            return page_message("沒有接上", f"{tenant} 目前有仲介在使用，接過去會讓對方的 LINE 斷線，所以不做。"
                                "要幫他看資料，請他在 LINE 打「匯出我的資料」。", "<p><a href='/admin/agents'>回仲介頁</a></p>")
        ok, words = app.registry.redeem(me, app.registry.issue_code(tenant))
        return page_message("已接上" if ok else "沒有成功", f"你的 LINE 現在接到 {tenant}。" if ok else words,
                            "<p><a href='/admin/agents'>回仲介頁</a></p>")
    if do == "me_fresh":
        app.registry.unbind_user(me)
        return page_message("已改成新客人", "你的 LINE 已經跟帳號分開。現在在 LINE 傳任何訊息，就會看到新客人的歡迎卡；"
                            "按「同意並開始」會用你的 LINE 開一個新的客人帳號。試完回「仲介」頁一鍵接回。",
                            "<p><a href='/admin/agents'>回仲介頁</a></p>")
    return page_message("看不懂這個動作", "請回上一頁再按一次。")


# ── WSGI ──────────────────────────────────────────────────────────────────

def make_admin_app(app, auth: AdminAuth):
    def wsgi(environ, start_response):
        def send(body: bytes, status="200 OK", headers=()):
            start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                                    ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                                    ("Content-Length", str(len(body))), *headers])
            return [body]

        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        if path == "/admin/login" and method == "GET":
            sid = auth.redeem((qs.get("t") or [""])[0])
            if not sid:
                return send(page_message("登入連結失效", "連結 10 分鐘內有效、只能用一次。請在 LINE 再點一次「管理員面板」。"),
                            "403 Forbidden")
            cookie = f"{COOKIE}={sid}; Path=/admin; Max-Age={int(SESSION_TTL.total_seconds())}; HttpOnly; Secure; SameSite=Lax"
            return send(b"", "302 Found", [("Location", "/admin/"), ("Set-Cookie", cookie)])
        jar = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        sid = jar[COOKIE].value if COOKIE in jar else None
        sess = auth.session(sid)
        if not sess or sess["user"] not in admins():
            return send(page_message("請先登入", f"在 LINE 對「{brand.NAME}」官方帳號點「更多功能」→「管理員面板」，"
                                     "會收到一個登入連結（10 分鐘內有效）。"), "401 Unauthorized")
        if path == "/admin/logout":
            auth.logout(sid)
            return send(page_message("已登出", "這台裝置已登出。"), headers=[("Set-Cookie", f"{COOKIE}=; Path=/admin; Max-Age=0")])
        if path == "/admin/act" and method == "POST":
            try:
                n = min(int(environ.get("CONTENT_LENGTH") or 0), 10_000)
            except ValueError:
                n = 0
            form = {k: v[0] for k, v in urllib.parse.parse_qs(environ["wsgi.input"].read(n).decode("utf-8", "replace")).items()}
            if not secrets.compare_digest(form.get("csrf", ""), sess["csrf"]):
                return send(page_message("請重新整理", "表單過期了，請回上一頁重新整理再按。"), "403 Forbidden")
            return send(act(app, sess, form))
        if method != "GET":
            return send(b"", "405 Method Not Allowed", [("Allow", "GET, POST")])
        tab = path[len("/admin"):].strip("/")
        if tab == "":
            return send(page_overview(app, sess))
        if tab == "agents":
            return send(page_agents(app, sess))
        if tab == "patrol":
            return send(page_patrol(app, sess, (qs.get("all") or [""])[0] == "1"))
        if tab == "market":
            return send(page_market(app, sess))
        return send(page_message("找不到這一頁", "請從上面的分頁進去。"), "404 Not Found")
    return wsgi


def login_card(url: str) -> dict:
    """LINE 回覆：一張卡、一顆按鈕開登入連結（10 分鐘、一次）。"""
    return {"type": "flex", "altText": "管理員面板登入連結",
            "contents": {"type": "bubble", "size": "kilo",
                         "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                             {"type": "text", "text": "管理員面板", "weight": "bold", "size": "lg"},
                             {"type": "text", "text": "按下面的按鈕登入。連結 10 分鐘內有效、只能用一次；登入後這台裝置記住 30 天。",
                              "wrap": True, "size": "sm", "color": "#6e6e73"}]},
                         "footer": {"type": "box", "layout": "vertical", "contents": [
                             {"type": "button", "style": "primary", "color": brand.GREEN,
                              "action": {"type": "uri", "label": "打開管理員面板", "uri": url}}]}}}

