# -*- coding: utf-8 -*-
"""一鍵串接精靈的「檢查格式＋實際測試連線」。

每個函式都回 Check：ok（過了沒）、msg（白話一句）、fix（照著做就能修的步驟）、data（成功時給畫面的資訊，絕不含金鑰）。
網路一律走 opener（測試時換成假的），不跟轉址（避免金鑰被帶到別的主機），逾時 15 秒。
"""
from __future__ import annotations

import base64
import datetime
import json
import re
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

LINE_API = "https://api.line.me"
USER_AGENT = "rea-kit/1.0 (+setup wizard)"
MAX_RESPONSE = 30 * 1024 * 1024


@dataclass
class Check:
    ok: bool
    msg: str
    fix: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "msg": self.msg, "fix": self.fix, "data": self.data}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


SAFE_OPEN = urllib.request.build_opener(_NoRedirect).open


@dataclass
class Resp:
    status: int
    body: bytes
    content_type: str = ""
    session_id: str = ""                # MCP 伺服器給的 Mcp-Session-Id（ToEstate 用；其他串接用不到）

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return {}


class NetError(Exception):
    """連不上（DNS、逾時、拒絕連線）。"""


def http(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
         opener=None, timeout: float = 15.0) -> Resp:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with (opener or SAFE_OPEN)(req, timeout=timeout) as r:
            body = r.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise NetError("回應太大")
            return Resp(getattr(r, "status", 200), body, r.headers.get("Content-Type", "") if r.headers else "",
                        (r.headers.get("Mcp-Session-Id", "") or "") if r.headers else "")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        finally:
            e.close()
        return Resp(e.code, raw, (e.headers.get("Content-Type", "") if e.headers else ""))
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise NetError(str(getattr(e, "reason", e))) from None


NO_NET = ["確認這台電腦連得上網路（開瀏覽器隨便開個網站試試）", "公司網路有防火牆的話，請網管放行這個網址"]


# ═════════════════ LINE 官方帳號 ═════════════════
SECRET_RE = re.compile(r"^[0-9a-f]{32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=._-]{100,400}$")


def check_line_secret_format(secret: str) -> Check:
    s = (secret or "").strip()
    if not s:
        return Check(False, "還沒貼上 Channel secret。", ["到 LINE Developers → 你的 Channel →「Basic settings」分頁，找到 Channel secret，按複製"])
    if SECRET_RE.match(s):
        return Check(True, "Channel secret 格式正確。")
    if TOKEN_RE.match(s):
        return Check(False, "這看起來是 Channel access token，不是 Channel secret（兩個貼反了）。",
                     ["Channel secret 是 32 個英數字，在「Basic settings」分頁", "很長那串是 access token，在「Messaging API」分頁"])
    return Check(False, f"Channel secret 應該是 32 個英數字（0-9、a-f），你貼的有 {len(s)} 個字。",
                 ["確認整串都有複製到，前後沒有多空白", "不要貼到 Channel ID（那是一串數字）"])


def check_line_token_format(token: str) -> Check:
    t = (token or "").strip()
    if not t:
        return Check(False, "還沒貼上 Channel access token。",
                     ["到 LINE Developers → 你的 Channel →「Messaging API」分頁最下面，按「Issue」產生，再按複製"])
    if SECRET_RE.match(t):
        return Check(False, "這看起來是 Channel secret，不是 access token（兩個貼反了）。",
                     ["access token 很長（一百多個字），在「Messaging API」分頁最下面"])
    if " " in t or "\n" in t:
        return Check(False, "access token 中間有空白或換行，可能複製時斷行了。", ["回 LINE Developers 再按一次複製鈕，直接貼上"])
    if not TOKEN_RE.match(t):
        return Check(False, f"access token 長度看起來不對（{len(t)} 個字），一般是一百多個字。",
                     ["確認是按「Messaging API」分頁裡 Channel access token（long-lived）的複製鈕"])
    return Check(True, "access token 格式正確。")


def _line_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"}


def line_bot_info(token: str, *, opener=None, api: str = LINE_API) -> Check:
    """用 token 問 LINE「我是哪個官方帳號」：能回名稱＝token 正確。"""
    fmt = check_line_token_format(token)
    if not fmt.ok:
        return fmt
    try:
        r = http("GET", f"{api}/v2/bot/info", headers=_line_headers(token), opener=opener)
    except NetError:
        return Check(False, "連不上 LINE 的伺服器。", NO_NET)
    if r.status == 200:
        d = r.json()
        return Check(True, f"連上了：{d.get('displayName') or '你的官方帳號'}（{d.get('basicId') or ''}）",
                     data={"name": d.get("displayName", ""), "basic_id": d.get("basicId", ""),
                           "bot_user_id": d.get("userId", "")})
    if r.status == 401:
        return Check(False, "LINE 說這把 access token 不對或已經失效。",
                     ["如果你按過「Reissue」重新發行，舊的那把就失效了，要貼新的",
                      "確認是同一個 Channel 的 token（有好幾個官方帳號時容易拿錯）"])
    if r.status == 429:
        return Check(False, "LINE 說查太多次了，請等一分鐘再試。")
    return Check(False, f"LINE 回了看不懂的狀態（{r.status}），請稍後再試。")


def normalize_public_base(url: str) -> tuple[str, Check]:
    u = (url or "").strip().rstrip("/")
    if not u:
        return "", Check(False, "還沒填對外網址。", ["填你的網域或通道網址，例：https://rea.你的網域.com"])
    if u.startswith("http://"):
        return "", Check(False, "LINE 只接受 https:// 開頭的網址。",
                         ["用 Cloudflare Tunnel、ngrok 或主機商的 HTTPS 網址", "或改「跟我們租主機」，網址我們幫你弄好"])
    if not u.startswith("https://"):
        u = "https://" + u
    p = urllib.parse.urlparse(u)
    host = p.hostname or ""
    if not host or "." not in host or host in ("localhost",) or re.fullmatch(r"[\d.]+", host):
        return "", Check(False, "網址看起來不是公開網域（不能用 localhost 或 IP）。",
                         ["LINE 要從網路上連進來，必須是公開的 https 網域"])
    if p.path not in ("", "/") or p.query:
        return "", Check(False, "只要填網域就好，不用加後面的路徑。", [f"例：https://{host}"])
    return f"https://{host}" + (f":{p.port}" if p.port else ""), Check(True, "網址格式正確。")


def line_set_webhook(token: str, endpoint: str, *, opener=None, api: str = LINE_API) -> Check:
    try:
        r = http("PUT", f"{api}/v2/bot/channel/webhook/endpoint", headers=_line_headers(token),
                 body=json.dumps({"endpoint": endpoint}).encode(), opener=opener)
    except NetError:
        return Check(False, "連不上 LINE 的伺服器。", NO_NET)
    if r.status == 200:
        return Check(True, "已經幫你把 Webhook 網址填進 LINE。")
    if r.status == 401:
        return Check(False, "access token 不對或失效，沒辦法幫你填 Webhook。", ["回上一步重新貼 access token"])
    msg = (r.json() or {}).get("message", "")
    return Check(False, f"LINE 不接受這個 Webhook 網址（{r.status} {msg}）。",
                 ["網址必須是 https，而且不能超過 500 字", "確認網域拼字正確"])


def line_webhook_active(token: str, *, opener=None, api: str = LINE_API) -> Check:
    try:
        r = http("GET", f"{api}/v2/bot/channel/webhook/endpoint", headers=_line_headers(token), opener=opener)
    except NetError:
        return Check(False, "連不上 LINE 的伺服器。", NO_NET)
    if r.status != 200:
        return Check(False, f"查不到 Webhook 設定（{r.status}）。")
    d = r.json()
    if d.get("active"):
        return Check(True, "LINE 後台的「Use webhook」已經打開。", data={"endpoint": d.get("endpoint", "")})
    return Check(False, "網址填好了，但 LINE 後台的「Use webhook」還沒打開。",
                 ["到 LINE Developers →「Messaging API」分頁 → Webhook settings", "把「Use webhook」切到開（綠色）",
                  "回來再按一次「測試連線」"], data={"endpoint": d.get("endpoint", "")})


def line_test_webhook(token: str, endpoint: str, *, opener=None, api: str = LINE_API) -> Check:
    """請 LINE 真的打一次我們的 webhook（LINE 官方的 webhook test API）。過了＝網址通、secret 也對。"""
    try:
        r = http("POST", f"{api}/v2/bot/channel/webhook/test", headers=_line_headers(token),
                 body=json.dumps({"endpoint": endpoint}).encode(), opener=opener, timeout=30)
    except NetError:
        return Check(False, "連不上 LINE 的伺服器。", NO_NET)
    if r.status == 401:
        return Check(False, "access token 不對或失效。", ["回第 3 步重新貼 access token"])
    if r.status != 200:
        return Check(False, f"LINE 的測試功能回了 {r.status}，請稍後再試。")
    d = r.json()
    if d.get("success"):
        return Check(True, "LINE 已經成功連到你的服務，簽章也驗過了 🎉")
    code, reason = d.get("statusCode"), str(d.get("reason") or "") + " " + str(d.get("detail") or "")
    low = reason.lower()
    if code == 403:
        return Check(False, "LINE 連到你的服務了，但簽章對不上：Channel secret 填錯了。",
                     ["回第 3 步，從「Basic settings」重新複製 Channel secret", "如果按過 secret 的「Issue」，要貼新的那個"])
    if code == 404:
        return Check(False, "LINE 連到了主機，但找不到收訊息的位置（404）。",
                     ["確認對外網址指到的是這個服務（通道的目標要是 http://127.0.0.1:8080）"])
    if code in (502, 503, 504) or "timeout" in low or "time out" in low:
        return Check(False, "LINE 連過去，但你的服務沒有回應（可能沒開，或通道斷了）。",
                     ["確認服務在跑（狀態頁開得起來）", "確認通道（Cloudflare Tunnel／ngrok）也在跑",
                      "家用網路常常斷線：不想顧主機的話可以跟我們租"])
    if "resolve" in low or "unknownhost" in low or "dns" in low:
        return Check(False, "LINE 找不到這個網域（DNS 查不到）。", ["檢查網址有沒有拼錯", "剛設定的網域可能要等幾分鐘才生效"])
    if "ssl" in low or "certificate" in low or "tls" in low:
        return Check(False, "你的網址 HTTPS 憑證有問題，LINE 拒絕連線。", ["要用正式憑證（Cloudflare、Let's Encrypt 都可以），不能用自簽憑證"])
    if "refused" in low:
        return Check(False, "LINE 連過去被拒絕：主機有開，但服務沒在聽。", ["確認服務有啟動、通道指到正確的埠"])
    return Check(False, f"LINE 連不到你的服務（狀態 {code or '無'}）。",
                 ["確認服務和通道都在跑，再按一次測試", "還是不行，請截圖這個畫面給店長或負責架設的人"],
                 data={"status": code})


# ═════════════════ Google 試算表 ═════════════════
SHEET_RE = re.compile(r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})")
PUB_RE = re.compile(r"^https://docs\.google\.com/spreadsheets/d/e/([A-Za-z0-9_-]{20,})/pub")


def sheet_csv_url(url: str) -> tuple[str, Check]:
    u = (url or "").strip()
    if not u:
        return "", Check(False, "還沒貼上試算表網址。", ["打開你的 Google 試算表，複製瀏覽器網址列整串"])
    m = PUB_RE.match(u)
    if m:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        gid = (q.get("gid") or ["0"])[0]
        return (f"https://docs.google.com/spreadsheets/d/e/{m.group(1)}/pub?output=csv&gid={gid}",
                Check(True, "網址格式正確（已發布到網路的試算表）。"))
    m = SHEET_RE.match(u)
    if not m:
        return "", Check(False, "這不是 Google 試算表的網址。",
                         ["網址要長得像 https://docs.google.com/spreadsheets/d/一長串/edit", "Excel 檔請先上傳到 Google 雲端硬碟，再用 Google 試算表開啟"])
    gid = "0"
    g = re.search(r"[#&?]gid=(\d+)", u)
    if g:
        gid = g.group(1)
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid}",
            Check(True, "網址格式正確。"))


def fetch_sheet_csv(url: str, *, opener=None) -> tuple[bytes, Check]:
    csv_url, fmt = sheet_csv_url(url)
    if not fmt.ok:
        return b"", fmt
    try:
        r = http("GET", csv_url, opener=opener, timeout=20)
    except NetError:
        return b"", Check(False, "連不上 Google。", NO_NET)
    share_fix = ["在試算表右上角按「共用」", "「一般存取權」改成「知道連結的任何人」、角色「檢視者」",
                 "（建議：表裡只放需求條件和代號，不要放客人電話、身分證）"]
    if r.status in (301, 302, 303, 307, 401, 403) or "text/html" in r.content_type.lower():
        return b"", Check(False, "Google 不讓我們讀這份試算表（還沒開放連結檢視）。", share_fix)
    if r.status == 404:
        return b"", Check(False, "找不到這份試算表或這個分頁。", ["確認網址完整，分頁沒有被刪掉"])
    if r.status != 200:
        return b"", Check(False, f"Google 回了 {r.status}，請稍後再試。")
    if not r.body.strip():
        return b"", Check(False, "這個分頁是空的。", ["確認第一列是欄位名稱，下面有資料"])
    return r.body, Check(True, "讀到試算表了。")


def check_sheet(url: str, kind: str, *, opener=None) -> Check:
    """kind＝customers／listings。真的讀一次，用暫存資料夾跑一次匯入（不寫進正式資料），回報幾筆能用。"""
    body, got = fetch_sheet_csv(url, opener=opener)
    if not got.ok:
        return got
    from adapters.storage import TenantStore
    with tempfile.TemporaryDirectory() as tmp:
        store = TenantStore(tmp)
        try:
            if kind == "customers":
                from adapters.csv.customer_source import CsvCustomerSource
                rep = CsvCustomerSource(store).import_customers("dry-run", body)
            else:
                from adapters.csv.listing_source import CsvListingSource
                rep = CsvListingSource(store).import_csv("dry-run", body, default_transaction="buy")
        except ValueError as e:
            return Check(False, f"試算表讀到了，但欄位對不上：{e}",
                         ["第一列要是欄位名稱；可以直接下載本專案的範本照抄（精靈裡有連結）"])
    data = {"rows_total": rep.rows_total, "rows_ok": rep.rows_ok, "errors": rep.errors[:5],
            "ignored_sensitive_columns": list(rep.ignored_sensitive_columns)}
    if rep.rows_ok == 0:
        return Check(False, "試算表讀到了，但沒有一列能用。",
                     [f"第 {e['line']} 列：{e['reason']}" for e in rep.errors[:3]] or ["確認第二列開始有資料"], data=data)
    note = ""
    if rep.ignored_sensitive_columns:
        note = f"（電話／地址這類欄位不會匯入：{'、'.join(rep.ignored_sensitive_columns)}）"
    return Check(True, f"可以匯入 {rep.rows_ok}／{rep.rows_total} 列{note}", data=data)


# ═════════════════ 內政部實價登錄（開放資料，免金鑰）═════════════════
LVR_BASE = "https://plvr.land.moi.gov.tw/DownloadSeason"
COUNTY_LETTER = {"台北市": "A", "台中市": "B", "基隆市": "C", "台南市": "D", "高雄市": "E", "新北市": "F",
                 "宜蘭縣": "G", "桃園市": "H", "嘉義市": "I", "新竹縣": "J", "苗栗縣": "K", "南投縣": "M",
                 "彰化縣": "N", "新竹市": "O", "雲林縣": "P", "嘉義縣": "Q", "屏東縣": "T", "花蓮縣": "U",
                 "台東縣": "V", "金門縣": "W", "澎湖縣": "X", "連江縣": "Z"}


def lvr_seasons(today: datetime.date | None = None, n: int = 6) -> list[str]:
    """由近到遠的季別（民國年＋S＋季），例：115S3、115S2…"""
    t = today or datetime.date.today()
    y, q = t.year - 1911, (t.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append(f"{y}S{q}")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def lvr_url(county: str, season: str, kind: str = "A") -> str:
    """kind：A＝買賣、B＝預售屋、C＝租賃。"""
    return f"{LVR_BASE}?season={season}&fileName={COUNTY_LETTER[county]}_lvr_land_{kind}.csv"


def parse_counties(text: str) -> tuple[list[str], list[str]]:
    names = [x.strip().replace("臺", "台") for x in re.split(r"[,，、\s]+", text or "") if x.strip()]
    good = [n for n in names if n in COUNTY_LETTER]
    bad = [n for n in names if n not in COUNTY_LETTER]
    return list(dict.fromkeys(good)), bad


def check_lvr(counties: str, *, opener=None, today: datetime.date | None = None) -> Check:
    good, bad = parse_counties(counties)
    if bad:
        return Check(False, "看不懂這些縣市：" + "、".join(bad), ["請寫完整縣市名，例：台中市、宜蘭縣"])
    if not good:
        return Check(False, "請至少選一個縣市。")
    first = good[0]
    for season in lvr_seasons(today):
        try:
            r = http("GET", lvr_url(first, season), opener=opener, timeout=30)
        except NetError:
            return Check(False, "連不上內政部實價登錄網站。", NO_NET + ["政府網站偶爾維護，晚點再試"])
        if r.status == 200 and "鄉鎮市區".encode("utf-8") in r.body[:200]:
            rows = max(r.body.count(b"\n") - 2, 0)       # 前兩列是中英文欄名
            return Check(True, f"連上了：{first} {season} 有 {rows} 筆買賣資料（免金鑰、免費）。",
                         data={"counties": good, "season": season, "rows": rows})
    return Check(False, "內政部網站有回應，但最近幾季都抓不到資料。", ["政府網站可能在維護，晚點再試"])


# ═════════════════ 內政部地政整合資訊服務共享協作平台（付費 API）═════════════════
MOI_BASE = "https://copapi.moi.gov.tw"


def check_toestate(url: str, key: str = "", *, opener=None, store=None) -> Check:
    """實際握手並查一次「台中市南屯區」的行政區統計（用掉 1 次查詢）。"""
    import tempfile
    from adapters.storage import TenantStore
    from adapters.toestate import DEFAULT_URL, ToEstateClient, ToEstateError
    url = (url or DEFAULT_URL).strip()
    if not url.startswith("https://"):
        return Check(False, "網址要用 https:// 開頭。", [f"沒有特別的網址就留空，會用預設的 {DEFAULT_URL}"])
    with tempfile.TemporaryDirectory() as tmp:          # 測試不留快取，正式查詢才快取
        client = ToEstateClient(store or TenantStore(tmp), url=url, key=key or "", opener=opener, env={})
        try:
            stats = client.area_stats("台中市", "南屯區")
        except ToEstateError as e:
            return Check(False, str(e), ["確認網址正確（預設 " + DEFAULT_URL + "）",
                                         "有填金鑰的話，確認金鑰沒有多複製空白", "公司網路有防火牆的話，請網管放行 toestate.tw"])
    if not stats or not stats.get("n_1y"):
        return Check(False, "連上了，但查不到測試資料（台中市南屯區）。請稍後再試，或聯絡 ToEstate 確認服務狀態。")
    return Check(True, f"連線成功：台中市南屯區近一年 {stats['n_1y']} 筆成交。", data={"n_1y": stats["n_1y"]})


def check_moi(client_id: str, secret: str, *, opener=None, base: str = MOI_BASE) -> Check:
    """只換 access token（不查任何地號，不會扣錢）。換得到＝帳號、安全碼、服務開通都 OK。"""
    cid, sec = (client_id or "").strip(), (secret or "").strip()
    if not cid or not sec:
        return Check(False, "Client ID 和安全碼都要填。", ["在地政協作平台登入後，到「應用程式／API 金鑰」頁面複製"])
    if " " in cid or " " in sec:
        return Check(False, "Client ID 或安全碼中間有空白，可能複製到多餘的字。")
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    try:
        r = http("GET", f"{base}/cp/getToken", headers={"Authorization": "Basic " + auth}, opener=opener, timeout=25)
    except NetError:
        return Check(False, "連不上內政部地政平台。", NO_NET + ["政府平台偶爾維護，晚點再試"])
    if r.status == 200 and r.json().get("access_token"):
        return Check(True, "地政平台驗證通過（只換了通行證，沒有查任何地號、沒有扣錢）。")
    if r.status in (200, 302, 400, 401, 403):            # 帳密錯時平台會轉址到錯誤頁（302）
        return Check(False, "地政平台不認得這組 Client ID／安全碼。",
                     ["確認是從平台的金鑰管理頁複製，兩個沒有對調", "申請的服務要「已核准」才會生效",
                      "帳戶沒有餘額時也可能被拒絕"])
    if r.status == 429:
        return Check(False, "平台說換通行證太頻繁，等一分鐘再試。")
    return Check(False, f"地政平台回了 {r.status}，可能在維護，晚點再試。")


# ═════════════════ 591／樂屋（透過搜尋服務 API，不直接爬平台）═════════════════
def check_search(provider: str, key: str, *, opener=None) -> Check:
    from adapters.web_search import PROVIDERS, SearchError
    kind, k = (provider or "").strip().lower(), (key or "").strip()
    if kind not in PROVIDERS:
        return Check(False, "請選一個搜尋服務（Serper 或 Brave）。")
    if not k:
        return Check(False, "還沒貼上搜尋服務的 API key。")
    if " " in k:
        return Check(False, "API key 中間有空白，可能複製到多餘的字。")
    try:
        hits = PROVIDERS[kind](k, opener=opener).search("site:591.com.tw 出售 電梯大樓", recency="w", count=5)
    except SearchError as e:
        if e.kind == "auth":
            return Check(False, "搜尋服務說這把 API key 不對或已停用。", ["回搜尋服務的後台重新複製 API key"])
        if e.kind == "quota":
            return Check(False, "搜尋服務的免費額度用完了，或打太快被限速。", ["到搜尋服務後台看剩餘額度", "Brave 每月有免費額度，月初會重置"])
        return Check(False, f"搜尋服務暫時連不上（{e}）。", NO_NET)
    return Check(True, f"搜尋服務可以用：試查 591 最近一週，找到 {len(hits)} 筆。",
                 data={"sample": [h.title[:40] for h in hits[:3]]})
