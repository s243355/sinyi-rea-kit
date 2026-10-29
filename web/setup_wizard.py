"""一鍵串接精靈（/setup）：點選＋貼上就把 LINE、Google 試算表、實價登錄、地政平台、591／樂屋搜尋串好。

安全：
  - 只收「架設這台電腦」自己的連線（127.0.0.1／::1），而且帶任何轉送標頭（通道、反向代理）一律拒絕
    ——就算服務開到外網，外面的人也打不開精靈。
  - 第一次要用一次性網址 /setup?k=<碼> 進來（啟動器會自動開），之後靠 HttpOnly Cookie；API 另要自訂標頭擋 CSRF。
  - 金鑰只寫進本機設定檔（adapters/settings_store），任何 API 都不回傳金鑰內容。
"""
from __future__ import annotations

import ipaddress
import json
import pathlib
import secrets
import urllib.parse
from http.cookies import SimpleCookie

from adapters import integration_checks as C
from adapters import integration_status
from adapters.settings_store import SettingsStore
from web import setup_claim

HERE = pathlib.Path(__file__).resolve().parent
COOKIE = "rea_setup"
FORWARD_HEADERS = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP", "HTTP_FORWARDED",
                   "HTTP_CF_CONNECTING_IP", "HTTP_CF_RAY", "HTTP_TRUE_CLIENT_IP", "HTTP_X_ORIGINAL_FORWARDED_FOR")
RENT_URL = ""                   # 信義版不提供租主機（原版的租主機連結已拿掉）
MAX_BODY = 20_000
# Google 試算表範本（第二列是假資料；電話、地址、身分證不要放，放了也不會匯入）
TEMPLATES = {
    "customers": "客戶編號,稱呼,買或租,需求區域,預算(萬),需求型態,需求房數,狀態\r\n"
                 "A001,範例客戶甲,買,台中市西屯區、台中市南屯區,1200,電梯大樓,3,進行中\r\n",
    # 店內物件表：「負責同仁」填管理員面板登記的名字，配到買方時會通知那位同仁
    "listings": "物件編號,案名,交易,縣市,區域,總價(萬),型態,格局,坪數,屋齡,網址,上架日,狀態,負責同仁\r\n"
                "S-001,範例景觀三房,出售,台中市,西屯區,1180,電梯大樓,3房2廳2衛,32.5,8,https://example.com/l/S-001,2026-10-01,銷售中,範例同仁\r\n",
}
GROUPS = {
    "line": ("REA_SHARED_LINE_SECRET", "REA_SHARED_LINE_TOKEN", "REA_PUBLIC_BASE", "REA_ADMIN_USERS", "REA_SIGNUP_MODE"),
    "gsheet": ("REA_GSHEET_CUSTOMERS_URL", "REA_GSHEET_LISTINGS_URL"),
    "lvr": ("REA_LVR_COUNTIES",),
    "toestate": ("REA_TOESTATE_URL", "REA_TOESTATE_KEY", "REA_TOESTATE_DAILY_CAP"),
    "moi": ("REA_MOI_CLIENT_ID", "REA_MOI_SECRET", "REA_MOI_DAILY_LIMIT"),
    "search": ("REA_SEARCH_PROVIDER", "REA_SEARCH_KEY"),
}


def token_path(settings: SettingsStore) -> pathlib.Path:
    return settings.path.with_name("setup_token")


def ensure_token(settings: SettingsStore) -> str:
    """啟動器用：拿（沒有就產生）一次性進入碼。檔案在本機資料夾，不進 git。"""
    p = token_path(settings)
    try:
        tok = p.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok, encoding="utf-8")
    return tok


def is_local(environ) -> bool:
    if any(environ.get(h) for h in FORWARD_HEADERS):
        return False
    host = (environ.get("HTTP_HOST") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host and host not in ("127.0.0.1", "localhost", "::1"):     # 擋 DNS rebinding 與不帶標頭的通道
        return False
    try:
        return ipaddress.ip_address(environ.get("REMOTE_ADDR", "")).is_loopback
    except ValueError:
        return False


def make_setup_app(holder, settings: SettingsStore, *, token: str | None = None, opener=None, now=None):
    token = token or ensure_token(settings)
    page = (HERE / "setup.html").read_bytes()
    kw = {"opener": opener} if opener else {}

    def send(start_response, status, body: bytes, ctype="application/json; charset=utf-8", extra=()):
        start_response(status, [("Content-Type", ctype), ("Content-Length", str(len(body))),
                                ("Cache-Control", "no-store"), ("X-Frame-Options", "DENY"),
                                ("Referrer-Policy", "no-referrer"),
                                ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                            "script-src 'self' 'unsafe-inline'; img-src 'self' data:"),
                                *extra])
        return [body]

    def js(start_response, data, status="200 OK"):
        return send(start_response, status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def status():
        st = settings.status()
        app = holder.app
        bot = {}
        if app is not None:
            bot = getattr(holder, "bot_info", {}) or {}
        return {
            "fields": st,
            "line": {"keys": st["REA_SHARED_LINE_SECRET"] and st["REA_SHARED_LINE_TOKEN"], "running": app is not None,
                     "public_base": settings.get("REA_PUBLIC_BASE") or "", "bot_name": bot.get("name", ""),
                     "webhook_ok": bool(getattr(holder, "webhook_ok", False)),
                     "admin": setup_claim.claimed(settings), "admin_tenant": bool(holder.admin_tenant()),
                     "error": holder.error},
            "gsheet": {"customers": st["REA_GSHEET_CUSTOMERS_URL"], "listings": st["REA_GSHEET_LISTINGS_URL"]},
            "lvr": {"counties": settings.get("REA_LVR_COUNTIES")},
            "toestate": {"url": settings.get("REA_TOESTATE_URL") or "", "key": st["REA_TOESTATE_KEY"],
                         "off": (settings.get("REA_TOESTATE_URL") or "").lower() == "off",
                         "default_city": settings.get("REA_DEFAULT_CITY") or "台中市"},
            "moi": {"keys": st["REA_MOI_CLIENT_ID"] and st["REA_MOI_SECRET"],
                    "daily_limit": settings.get("REA_MOI_DAILY_LIMIT") or "20"},
            "search": {"provider": settings.get("REA_SEARCH_PROVIDER"), "key": st["REA_SEARCH_KEY"]},
            "signup_mode": "open" if settings.get("REA_SIGNUP_MODE") == "open" else "invite",
            "team_mode": "solo" if settings.get("REA_TEAM_MODE") == "solo" else "shared",
            "health": integration_status.load(settings),
            "rent_url": RENT_URL,
        }

    # ── 各步驟 ──
    def line_keys(b):
        fs, ft = C.check_line_secret_format(b.get("secret", "")), C.check_line_token_format(b.get("token", ""))
        if not fs.ok:
            return fs
        if not ft.ok:
            return ft
        info = C.line_bot_info(b["token"], **kw)
        if not info.ok:
            return info
        integration_status.record("line", True, "", settings=settings)
        settings.save({"REA_SHARED_LINE_SECRET": b["secret"].strip(), "REA_SHARED_LINE_TOKEN": b["token"].strip()})
        holder.bot_info = info.data
        holder.webhook_ok = False
        holder.reload()
        info.msg = info.msg.rstrip("。") + "。金鑰已存在這台電腦（畫面不會再顯示）。"
        info.data = {"name": info.data.get("name", ""), "basic_id": info.data.get("basic_id", "")}
        return info

    def line_webhook(b):
        base, fmt = C.normalize_public_base(b.get("public_base", ""))
        if not fmt.ok:
            return fmt
        tok = settings.get("REA_SHARED_LINE_TOKEN")
        if not tok:
            return C.Check(False, "請先完成上一步（貼上 Channel secret 和 access token）。")
        if holder.app is None:
            holder.reload()
        endpoint = base + "/line-shared/webhook"
        # 先請 LINE 對「新網址」打一次測試，過了才真的改 LINE 後台：原本能用的設定不會被一個打錯的網址弄壞
        test = C.line_test_webhook(tok, endpoint, **kw)
        if not test.ok:
            return test
        r = C.line_set_webhook(tok, endpoint, **kw)
        if not r.ok:
            return r
        settings.save({"REA_PUBLIC_BASE": base})
        holder.reload()                                       # 服務條款、管理員面板的連結用新網址
        active = C.line_webhook_active(tok, **kw)
        holder.webhook_ok = active.ok
        if not active.ok:
            return active
        test.data = {"endpoint": endpoint}
        return test

    def gsheet_check(b):
        kind = b.get("kind")
        if kind not in ("customers", "listings"):
            return C.Check(False, "不認得的試算表種類。")
        r = C.check_sheet(b.get("url", ""), kind, **kw)
        if r.ok:
            settings.save({"REA_GSHEET_CUSTOMERS_URL" if kind == "customers" else "REA_GSHEET_LISTINGS_URL":
                           b["url"].strip()})
            r.msg = r.msg.rstrip("。") + "。網址已存好；之後每天自動同步一次，也可以按「立即同步」。"
        return r

    def gsheet_sync(b):
        from adapters.gsheet_sync import sync
        tenant = holder.admin_tenant()
        if not tenant:
            return C.Check(False, "要先完成 LINE 第 5 步（綁定管理員），資料才知道要放進誰的資料夾。")
        kind = b.get("kind", "")
        if kind not in ("customers", "listings"):
            return C.Check(False, "不認得的試算表種類。")
        res = sync(kind, tenant, customers=holder.app.customers, listings=holder.app.listings, **kw)
        integration_status.record("gsheet_" + kind, res.ok, "" if res.ok else res.msg, settings=settings)
        if res.ok and kind == "listings":
            from web.jobs.team_notify import after_sheet_sync
            told = after_sheet_sync(holder.app, res.data.get("new_ids"))
            if told:
                res.msg = res.msg.rstrip("。") + f"。新物件配到 {len(told)} 位同仁的買方，已用 LINE 通知他們。"
        return res

    def lvr_check(b):
        r = C.check_lvr(b.get("counties", ""), **kw)
        if r.ok:
            settings.save({"REA_LVR_COUNTIES": "、".join(r.data["counties"])})
        return r

    def toestate_check(b):
        url = (b.get("url") or "").strip()
        city = (b.get("default_city") or "").replace("臺", "台").strip()
        if city:
            from core.tw_districts import DISTRICTS
            if city not in DISTRICTS:
                return C.Check(False, "預設縣市看不懂，請從選單選。")
        if url.lower() == "off":
            settings.save({"REA_TOESTATE_URL": "off", **({"REA_DEFAULT_CITY": city} if city else {})})
            holder.reload()
            return C.Check(True, "已關閉 ToEstate，行情查詢只用內政部資料（只有行政區概況）。")
        key = (b.get("key") or "").strip() or settings.get("REA_TOESTATE_KEY") or ""
        r = C.check_toestate(url, key, **kw)
        if r.ok:
            updates = {"REA_TOESTATE_URL": url}
            if (b.get("key") or "").strip():
                updates["REA_TOESTATE_KEY"] = key
            if city:
                updates["REA_DEFAULT_CITY"] = city
            settings.save(updates)
            holder.reload()
            r.msg = r.msg.rstrip("。") + "。已存好；同仁在 LINE 打「行情 社區名」就會用 ToEstate 查。"
        return r

    def lvr_query(b):
        from adapters import lvr_opendata as L
        county = (b.get("county") or "").replace("臺", "台").strip()
        try:
            season, body = L.fetch(county, root=settings.path.parent.parent, **kw)
        except (ValueError, RuntimeError) as e:
            return C.Check(False, str(e))
        rows = L.parse(body)
        district = (b.get("district") or "").strip()
        if not district:
            return C.Check(True, f"{county} {season}：請選行政區", data={"districts": L.districts(rows), "season": season})
        s = L.summary(rows, district)
        if not s["count"]:
            return C.Check(False, f"{county}{district} {season} 沒有一般建物買賣資料。")
        return C.Check(True, f"{county}{district} {season}：{s['count']} 筆，單價中位數約 {s['median_unit_wan_per_ping']} 萬/坪",
                       data={**s, "season": season})

    def moi_check(b):
        r = C.check_moi(b.get("client_id", ""), b.get("secret", ""), **kw)
        limit = str(b.get("daily_limit") or "20").strip()
        if not limit.isdigit() or not 0 < int(limit) <= 500:
            return C.Check(False, "每日上限請填 1～500 的數字。")
        if r.ok:
            integration_status.record("moi", True, "", settings=settings)
            settings.save({"REA_MOI_CLIENT_ID": b["client_id"].strip(), "REA_MOI_SECRET": b["secret"].strip(),
                           "REA_MOI_DAILY_LIMIT": limit})
            r.msg = r.msg.rstrip("。") + f"。已存好，每天最多查 {limit} 筆。"
        return r

    def moi_query(b):
        from adapters.moi_cop import MoiClient, MoiError
        client = MoiClient(root=settings.path.parent.parent, **kw)
        try:
            out = client.query(b.get("unit", ""), b.get("sec", ""), b.get("no", ""), confirm=bool(b.get("confirm")))
        except MoiError as e:
            if "驗證沒過" in str(e):
                integration_status.record("moi", False, "地政平台金鑰驗證沒過（可能過期、沒餘額或被停用），請到串接精靈重新驗證",
                                          settings=settings)
            return C.Check(False, str(e), data={"need_confirm": "確認查詢" in str(e)})
        tag = "（之前查過，沒有再收費）" if out["cached"] else "（已向平台查詢一筆）"
        area = f"面積 {out['area_sqm']} ㎡（約 {out['area_ping']} 坪）" if out.get("area_sqm") else "查到了"
        return C.Check(True, area + tag, data={"fields": out.get("fields", {})})

    def search_check(b):
        r = C.check_search(b.get("provider", ""), b.get("key", ""), **kw)
        if r.ok:
            integration_status.record("search", True, "", settings=settings)
            from adapters.web_search import SYS as SEARCH_SYS, USAGE
            from adapters.storage import TenantStore
            store = TenantStore(settings.path.parent.parent)          # 今天因金鑰錯被停用的，換新金鑰後馬上恢復
            with store.lock(SEARCH_SYS, "search-usage"):
                book = store.read_json(SEARCH_SYS, *USAGE, default={}) or {}
                if book.pop("off", None) is not None:
                    store.write_json(SEARCH_SYS, *USAGE, data=book)
            settings.save({"REA_SEARCH_PROVIDER": b["provider"].strip().lower(), "REA_SEARCH_KEY": b["key"].strip()})
            holder.reload()
            r.msg = r.msg.rstrip("。") + "。已存好，每天的「今日報告」會自動找各平台新物件和想買房的貼文。"
        return r

    def line_signup(b):
        mode = b.get("mode")
        if mode not in ("invite", "open"):
            return C.Check(False, "請選一個選項。")
        settings.save({"REA_SIGNUP_MODE": mode})
        return C.Check(True, "已改成：" + ("只有我邀請的人能用（在管理員面板按「邀請新成員」發綁定碼）。" if mode == "invite"
                                         else "任何加好友的人按「同意並開始」就能用（每天新加入有上限）。"))

    def team_mode(b):
        mode = b.get("mode")
        if mode not in ("shared", "solo"):
            return C.Check(False, "請選一個選項。")
        settings.save({"REA_TEAM_MODE": mode})
        holder.reload()
        return C.Check(True, "已改成：" + ("全店共用：大家的買方與物件互相看得到，新物件會配到全店的買方；只有建立的人和店長能修改。"
                                         if mode == "shared" else "各自獨立：每個人的客人和物件互相看不到。")
                       + "（切換前已經建的資料不會自動搬家，建議開始用之前就決定好。）")

    def line_claim(b):
        if holder.app is None:
            return C.Check(False, "要先完成 LINE 第 3 步（金鑰）和第 4 步（Webhook）。")
        return C.Check(True, "請在 10 分鐘內用手機傳給官方帳號", data={"code": setup_claim.issue_code(settings)})

    def clear(b):
        keys = GROUPS.get(b.get("group", ""))
        if not keys:
            return C.Check(False, "不認得的串接。")
        settings.save({k: "" for k in keys})
        holder.reload()
        return C.Check(True, "已取消這個串接，金鑰已從這台電腦刪除。")

    ROUTES = {"line/keys": line_keys, "line/webhook": line_webhook, "line/claim": line_claim, "line/signup": line_signup,
              "team/mode": team_mode,
              "gsheet/check": gsheet_check, "gsheet/sync": gsheet_sync, "lvr/check": lvr_check, "toestate/check": toestate_check, "lvr/query": lvr_query,
              "moi/check": moi_check, "moi/query": moi_query, "search/check": search_check, "clear": clear}

    def wsgi(environ, start_response):
        if not is_local(environ):
            return send(start_response, "403 Forbidden",
                        "串接精靈只能在架設服務的那台電腦上開（http://127.0.0.1:8080/setup）。".encode("utf-8"),
                        "text/plain; charset=utf-8")
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        jar = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        has_cookie = COOKIE in jar and secrets.compare_digest(jar[COOKIE].value, token)
        if path in ("/setup", "/setup/") and method == "GET":
            k = (qs.get("k") or [""])[0]
            if k and secrets.compare_digest(k, token):
                cookie = f"{COOKIE}={token}; Path=/setup; HttpOnly; SameSite=Strict; Max-Age=2592000"
                start_response("303 See Other", [("Location", "/setup"), ("Set-Cookie", cookie),
                                                 ("Content-Length", "0"), ("Cache-Control", "no-store")])
                return [b""]
            if not has_cookie:
                return send(start_response, "403 Forbidden",
                            ("請用啟動器打開精靈（雙擊「開始設定.vbs」，或執行 python tools/start.py --setup）。"
                             "它會自動帶一次性的進入碼。").encode("utf-8"), "text/plain; charset=utf-8")
            return send(start_response, "200 OK", page, "text/html; charset=utf-8")
        if not has_cookie:
            return js(start_response, {"ok": False, "msg": "請重新用啟動器打開精靈"}, "403 Forbidden")
        if path.startswith("/setup/template/") and method == "GET":
            kind = path[len("/setup/template/"):].removesuffix(".csv")
            if kind in TEMPLATES:
                return send(start_response, "200 OK", ("\ufeff" + TEMPLATES[kind]).encode("utf-8"), "text/csv; charset=utf-8",
                            [("Content-Disposition", f'attachment; filename="{kind}_template.csv"')])
        if path == "/setup/api/status" and method == "GET":
            return js(start_response, status())
        if path.startswith("/setup/api/") and method == "POST":
            if environ.get("HTTP_X_REA_SETUP") != "1":
                return js(start_response, {"ok": False, "msg": "bad request"}, "400 Bad Request")
            fn = ROUTES.get(path[len("/setup/api/"):])
            if fn is None:
                return js(start_response, {"ok": False, "msg": "not found"}, "404 Not Found")
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            if n > MAX_BODY:
                return js(start_response, {"ok": False, "msg": "too large"}, "413 Payload Too Large")
            try:
                body = json.loads(environ["wsgi.input"].read(n).decode("utf-8") or "{}") if n else {}
            except (UnicodeDecodeError, ValueError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            body = {k: (v if isinstance(v, (str, bool, int)) else "") for k, v in body.items()}
            try:
                res = fn(body)
            except Exception as e:                       # 不讓精靈白畫面；也不把例外內容（可能含網址）整段吐出
                import traceback
                traceback.print_exc()                    # 只進本機 server.log，不回給畫面
                res = C.Check(False, f"發生沒預料到的錯誤（{type(e).__name__}），請截圖這個畫面給店長或負責架設的人。")
            return js(start_response, res.as_dict())
        return js(start_response, {"ok": False, "msg": "not found"}, "404 Not Found")

    wsgi.token = token
    return wsgi
