"""一鍵串接精靈：每個串接用合成資料從頭走一遍（LINE、Google 試算表、實價登錄、地政平台、591／樂屋搜尋）。

網路全部是假的：FakeNet 扮演 LINE、Google、內政部、搜尋服務。LINE 的「webhook 測試」會真的打進我們的服務並驗簽。
"""
import base64
import datetime
import io
import json
import os
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.parse

from adapters import integration_checks as C
from adapters.line.client import LineClient
from adapters.line.fake_api import FakeLineApi, synthetic_line_id
from adapters.line.signature import sign
from adapters.settings_store import FIELDS, SettingsStore
from adapters.storage import TenantStore
from web.app_holder import AppHolder

SECRET = "0123456789abcdef0123456789abcdef"
TOKEN = "T" * 120 + "Abc/+="
WRONG_SECRET = "f" * 32
OWNER = synthetic_line_id("U", "wizard-owner")
CUSTOMERS_CSV = ("客戶編號,稱呼,買或租,需求區域,預算(萬),需求型態,需求房數,狀態,電話\n"
                 "A001,合成甲,買,台中市西屯區,1200,電梯大樓,3,進行中,PHONE-NOT-IMPORTED\n"
                 "A002,合成乙,買,台中市南屯區,900,華廈,2,進行中,\n").encode("utf-8")
LISTINGS_CSV = ("物件編號,案名,交易,縣市,區域,總價(萬),型態,格局,坪數,屋齡,網址,上架日,狀態\n"
                "S-001,合成景觀三房,出售,台中市,西屯區,1180,電梯大樓,3房2廳2衛,32.5,8,https://example.com/l/1,2026-10-01,銷售中\n").encode("utf-8")
LVR_CSV = ("﻿鄉鎮市區,交易標的,土地位置建物門牌,交易年月日,建物型態,總價元,單價元平方公尺,建物移轉總面積平方公尺,備註\n"
           "The villages and towns urban district,transaction sign,address,date,building state,total price,unit price,area,note\n"
           "西屯區,房地(土地+建物),示範路1~30號,1150801,住宅大樓(11層含以上有電梯),12000000,150000,80,\n"
           "西屯區,房地(土地+建物),示範路31~60號,1150802,住宅大樓(11層含以上有電梯),15000000,160000,93.75,\n"
           "西屯區,房地(土地+建物),示範路61~90號,1150803,公寓(5樓含以下無電梯),3000000,50000,60,親友間交易\n"
           "南屯區,土地,示範段,1150804,其他,5000000,30000,0,\n").encode("utf-8")


class FakeResp:
    def __init__(self, status, body=b"", ctype="application/json"):
        self.status, self._body = status, body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"Content-Type": ctype}

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """假網路。line_secret＝LINE 那邊真正的 secret（拿來簽 webhook 測試）；service＝我們的 WSGI（webhook 測試打它）。"""

    def __init__(self):
        self.line_secret, self.line_token = SECRET, TOKEN
        self.service = None
        self.endpoint, self.active = "", True
        self.sheet_public = True
        self.calls = []
        self.moi_queries = 0

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        self.calls.append((method, url))
        auth = req.headers.get("Authorization", "")
        if url.startswith("https://api.line.me"):
            if auth != f"Bearer {self.line_token}":
                return FakeResp(401, {"message": "Authentication failed"})
            path = urllib.parse.urlparse(url).path
            if path == "/v2/bot/info":
                return FakeResp(200, {"displayName": "合成房仲小幫手", "basicId": "@000test", "userId": "U" + "0" * 32})
            if path == "/v2/bot/channel/webhook/endpoint" and method == "PUT":
                self.endpoint = json.loads(req.data)["endpoint"]
                return FakeResp(200, {})
            if path == "/v2/bot/channel/webhook/endpoint":
                return FakeResp(200, {"endpoint": self.endpoint, "active": self.active})
            if path == "/v2/bot/channel/webhook/test":
                body = json.dumps({"destination": "U" + "0" * 32, "events": []}).encode()
                code = self.deliver(body)
                return FakeResp(200, {"success": code == 200, "statusCode": code, "reason": "", "detail": ""})
        if url.startswith("https://docs.google.com/spreadsheets/"):
            if not self.sheet_public:
                return FakeResp(200, b"<html>sign in</html>", "text/html; charset=utf-8")
            return FakeResp(200, LISTINGS_CSV if "gid=7" in url else CUSTOMERS_CSV, "text/csv")
        if url.startswith(C.LVR_BASE):
            return FakeResp(200, LVR_CSV, "application/octet-stream")
        if url.startswith(C.MOI_BASE + "/cp/getToken"):
            good = "Basic " + base64.b64encode(b"cid-synthetic:sec-synthetic").decode()
            return FakeResp(200, {"access_token": "moi-tok"}) if auth == good else FakeResp(401, {})
        if url.startswith(C.MOI_BASE + "/cp/api/"):
            self.moi_queries += 1
            return FakeResp(200, {"STATUS": 1, "RESPONSE": [{"LANDREG": {"AREA": "100.5", "ZONE": "住宅區"}}]})
        if url.startswith("https://api.search.brave.com"):
            if req.headers.get("X-subscription-token") != "brave-synthetic":
                raise urllib.error.HTTPError(url, 401, "x", {}, io.BytesIO(b""))
            return FakeResp(200, {"web": {"results": [{"title": "合成 591 物件", "url": "https://sale.591.com.tw/home/house/detail/2/1.html",
                                                       "description": "出售"}]}})
        return FakeResp(404, {})

    def deliver(self, body: bytes) -> int:
        out = {}

        def start(status, headers):
            out["code"] = int(status.split()[0])
        env = {"PATH_INFO": "/line-shared/webhook", "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body), "HTTP_X_LINE_SIGNATURE": sign(self.line_secret, body),
               "REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "203.0.113.9"}      # 經過通道進來
        self.service(env, start)
        return out["code"]


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = unittest.mock.patch.dict(os.environ, {k: "" for k in FIELDS})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.s = SettingsStore(self.tmp.name)

    def test_secret_is_not_plaintext_on_disk_and_roundtrips(self):
        self.s.save({"REA_SHARED_LINE_SECRET": SECRET, "REA_LVR_COUNTIES": "台中市"})
        raw = self.s.path.read_text(encoding="utf-8")
        self.assertEqual(self.s.get("REA_SHARED_LINE_SECRET"), SECRET)
        self.assertIn("台中市", raw)                                     # 非金鑰照存
        if os.name == "nt":
            self.assertNotIn(SECRET, raw)                                # Windows：DPAPI 加密
        self.assertEqual(os.environ["REA_SHARED_LINE_SECRET"], SECRET)   # 存了馬上生效

    def test_env_wins_and_status_has_no_values(self):
        self.s.save({"REA_SEARCH_KEY": "file-value"}, apply=False)
        os.environ["REA_SEARCH_KEY"] = "env-value"
        self.assertEqual(self.s.apply_to_environ(), [])
        self.assertEqual(os.environ["REA_SEARCH_KEY"], "env-value")
        st = self.s.status()
        self.assertIs(st["REA_SEARCH_KEY"], True)
        self.assertNotIn("value", json.dumps(st))

    def test_unknown_key_rejected_and_empty_deletes(self):
        with self.assertRaises(ValueError):
            self.s.save({"EVIL": "x"})
        self.s.save({"REA_SEARCH_KEY": "k"})
        self.s.save({"REA_SEARCH_KEY": ""})
        self.assertEqual(self.s.get("REA_SEARCH_KEY"), "")


class CheckTests(unittest.TestCase):
    def test_line_formats_explain_swaps(self):
        self.assertTrue(C.check_line_secret_format(SECRET).ok)
        swapped = C.check_line_secret_format(TOKEN)
        self.assertFalse(swapped.ok)
        self.assertIn("貼反了", swapped.msg)
        self.assertIn("貼反了", C.check_line_token_format(SECRET).msg)
        self.assertIn("空白", C.check_line_token_format("a b" * 50).msg)

    def test_public_base(self):
        self.assertEqual(C.normalize_public_base("rea.example.com/")[0], "https://rea.example.com")
        self.assertFalse(C.normalize_public_base("http://rea.example.com")[1].ok)
        self.assertFalse(C.normalize_public_base("https://localhost")[1].ok)
        self.assertFalse(C.normalize_public_base("https://203.0.113.5")[1].ok)
        self.assertIn("路徑", C.normalize_public_base("https://rea.example.com/line-shared/webhook")[1].msg)

    def test_webhook_test_failures_are_plain_language(self):
        def fake(code, reason=""):
            return lambda req, timeout=None: FakeResp(200, {"success": False, "statusCode": code, "reason": reason})
        self.assertIn("Channel secret 填錯", C.line_test_webhook(TOKEN, "https://x.example/w", opener=fake(403)).msg)
        self.assertIn("沒有回應", C.line_test_webhook(TOKEN, "https://x.example/w", opener=fake(None, "Request timeout")).msg)
        self.assertIn("DNS", C.line_test_webhook(TOKEN, "https://x.example/w", opener=fake(None, "could not resolve host")).msg)
        self.assertIn("憑證", C.line_test_webhook(TOKEN, "https://x.example/w", opener=fake(None, "SSL handshake")).msg)

    def test_sheet_urls(self):
        url, ok = C.sheet_csv_url("https://docs.google.com/spreadsheets/d/" + "a" * 30 + "/edit#gid=7")
        self.assertTrue(ok.ok)
        self.assertTrue(url.endswith("/export?format=csv&gid=7"))
        self.assertFalse(C.sheet_csv_url("https://example.com/sheet")[1].ok)
        net = FakeNet()
        net.sheet_public = False
        r = C.check_sheet("https://docs.google.com/spreadsheets/d/" + "a" * 30 + "/edit", "customers", opener=net)
        self.assertFalse(r.ok)
        self.assertIn("知道連結的任何人", " ".join(r.fix))

    def test_lvr_and_counties(self):
        self.assertEqual(C.lvr_seasons(datetime.date(2026, 2, 1), 3), ["115S1", "114S4", "114S3"])
        self.assertIn("看不懂", C.check_lvr("火星市").msg)
        r = C.check_lvr("臺中市", opener=FakeNet(), today=datetime.date(2026, 10, 20))
        self.assertTrue(r.ok, r.msg)
        self.assertEqual(r.data["counties"], ["台中市"])

    def test_moi_and_search(self):
        net = FakeNet()
        self.assertTrue(C.check_moi("cid-synthetic", "sec-synthetic", opener=net).ok)
        self.assertFalse(C.check_moi("cid-synthetic", "wrong", opener=net).ok)
        moved = lambda req, timeout=None: FakeResp(302, b"<html>Object moved</html>", "text/html")    # 真實平台帳密錯的樣子
        self.assertIn("不認得", C.check_moi("cid", "bad", opener=moved).msg)
        self.assertEqual(net.moi_queries, 0)                              # 驗證不查地號、不扣錢
        self.assertTrue(C.check_search("brave", "brave-synthetic", opener=net).ok)
        self.assertIn("不對", C.check_search("brave", "nope", opener=net).msg)
        def brave_422(req, timeout=None):                                   # 真實 Brave 金鑰錯回 422
            raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(b"{}"))
        self.assertIn("不對", C.check_search("brave", "nope", opener=brave_422).msg)


class WizardFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = unittest.mock.patch.dict(os.environ, {**{k: "" for k in FIELDS}, "REA_DATA_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        for k in FIELDS:
            os.environ.pop(k, None)
        self.api = FakeLineApi(token=TOKEN).start()
        self.addCleanup(self.api.stop)
        self.store = TenantStore(self.tmp.name)
        self.dt = datetime.datetime(2026, 10, 20, 9)
        from web.line_app import create_app
        factory = lambda token: LineClient(token, api_base=self.api.base_url, min_interval=0)
        self.holder = AppHolder(lambda: create_app(store=self.store, client_factory=factory, now=lambda: self.dt),
                                start_scheduler=False)
        self.net = FakeNet()
        self.settings = SettingsStore(self.tmp.name)
        from tools.serve_line import build_wsgi
        self.wsgi = build_wsgi(self.holder, self.settings, setup_token="t" * 40, opener=self.net)
        self.net.service = self.wsgi
        self.cookie = ""

    def call(self, path, body=None, *, method=None, remote="127.0.0.1", headers=None, cookie=None):
        raw = json.dumps(body).encode() if body is not None else b""
        path, _, qs = path.partition("?")
        env = {"PATH_INFO": path, "QUERY_STRING": qs, "REQUEST_METHOD": method or ("POST" if body is not None else "GET"),
               "REMOTE_ADDR": remote, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
               "HTTP_COOKIE": self.cookie if cookie is None else cookie, "HTTP_X_REA_SETUP": "1", **(headers or {})}
        out = {}

        def start(status, hdrs):
            out["status"], out["headers"] = status, dict(hdrs)
        data = b"".join(self.wsgi(env, start))
        try:
            data = json.loads(data)
        except ValueError:
            data = data.decode("utf-8", "replace")
        return out["status"], out["headers"], data

    def login(self):
        st, hdrs, _ = self.call("/setup?k=" + "t" * 40)
        self.assertTrue(st.startswith("303"))
        self.cookie = hdrs["Set-Cookie"].split(";")[0]

    def line_say(self, text, eid):
        body = json.dumps({"events": [{"type": "message", "webhookEventId": eid, "replyToken": "r-" + eid,
                                       "source": {"type": "user", "userId": OWNER},
                                       "message": {"type": "text", "text": text}}]}).encode()
        self.assertEqual(self.net.deliver(body), 200)
        return self.api.replies[-1]["messages"][0]

    # ── 安全 ──
    def test_wizard_only_opens_locally_with_the_one_time_link(self):
        self.assertTrue(self.call("/setup", remote="203.0.113.9")[0].startswith("403"))
        self.assertTrue(self.call("/setup", headers={"HTTP_CF_CONNECTING_IP": "203.0.113.9"})[0].startswith("403"))
        self.assertTrue(self.call("/setup")[0].startswith("403"))                     # 沒帶進入碼
        self.assertTrue(self.call("/setup?k=wrong")[0].startswith("403"))
        self.login()
        st, hdrs, page = self.call("/setup")
        self.assertTrue(st.startswith("200"))
        self.assertIn("一鍵串接精靈", page)
        self.assertNotIn("lin.ee", page)                                               # 信義版：沒有租主機廣告
        self.assertIn("rea-kit", page)                                                 # 但保留原作者署名
        self.assertIn("HttpOnly", self.call("/setup?k=" + "t" * 40)[1]["Set-Cookie"])
        st, _, _ = self.call("/setup/api/line/keys", {"secret": SECRET, "token": TOKEN}, headers={"HTTP_X_REA_SETUP": ""})
        self.assertTrue(st.startswith("400"))                                            # 沒自訂標頭（CSRF）

    def test_service_runs_before_line_is_set_up(self):
        st, _, body = self.call("/healthz")
        self.assertEqual(body, "setup-needed")
        self.assertIsNone(self.holder.app)

    # ── LINE：金鑰 → webhook → 綁管理員 ──
    def test_line_full_flow_with_real_signature_check(self):
        self.login()
        _, _, r = self.call("/setup/api/line/keys", {"secret": TOKEN, "token": SECRET})
        self.assertFalse(r["ok"])
        self.assertIn("貼反了", r["msg"])
        self.net.line_token = "X" * 120                                                  # LINE 那邊已重發
        _, _, r = self.call("/setup/api/line/keys", {"secret": SECRET, "token": TOKEN})
        self.assertFalse(r["ok"])
        self.assertIn("失效", r["msg"])
        self.net.line_token = TOKEN
        _, _, r = self.call("/setup/api/line/keys", {"secret": WRONG_SECRET, "token": TOKEN})
        self.assertTrue(r["ok"], r)                                                      # 格式對、token 對（secret 錯要等 webhook 測試才知道）
        self.assertIsNotNone(self.holder.app)
        _, _, r = self.call("/setup/api/line/webhook", {"public_base": "rea.example.com"})
        self.assertFalse(r["ok"])
        self.assertIn("Channel secret 填錯", r["msg"])                                 # LINE 真的打進來，驗簽失敗
        _, _, r = self.call("/setup/api/line/keys", {"secret": SECRET, "token": TOKEN})
        self.assertTrue(r["ok"])
        self.net.active = False
        _, _, r = self.call("/setup/api/line/webhook", {"public_base": "rea.example.com"})
        self.assertFalse(r["ok"])
        self.assertIn("Use webhook", r["msg"])
        self.net.active = True
        _, _, r = self.call("/setup/api/line/webhook", {"public_base": "https://rea.example.com"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.net.endpoint, "https://rea.example.com/line-shared/webhook")
        _, _, st = self.call("/setup/api/status")
        dumped = json.dumps(st, ensure_ascii=False)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(TOKEN, dumped)
        self.assertTrue(st["line"]["keys"] and st["line"]["running"])
        # 綁管理員
        _, _, r = self.call("/setup/api/line/claim", {})
        code = r["data"]["code"]
        self.line_say("同意並開始", "e1")
        self.assertIn("不對", self.line_say("管理員 000000" if code != "000000" else "管理員 111111", "e2")["text"])
        self.assertIn("管理員", self.line_say(f"管理員 {code}", "e3")["text"])
        _, _, st = self.call("/setup/api/status")
        self.assertTrue(st["line"]["admin"])
        self.assertEqual(os.environ["REA_ADMIN_USERS"], OWNER)
        self.assertTrue(self.holder.admin_tenant())

    def _line_ready(self):
        self.login()
        self.assertTrue(self.call("/setup/api/line/keys", {"secret": SECRET, "token": TOKEN})[2]["ok"])
        code = self.call("/setup/api/line/claim", {})[2]["data"]["code"]
        self.line_say("同意並開始", "s1")
        self.line_say(f"管理員 {code}", "s2")

    # ── Google 試算表 ──
    def test_google_sheet_check_save_and_sync(self):
        self.login()
        url = "https://docs.google.com/spreadsheets/d/" + "b" * 30 + "/edit#gid=0"
        _, _, r = self.call("/setup/api/gsheet/check", {"kind": "customers", "url": url})
        self.assertTrue(r["ok"], r)
        self.assertIn("2／2", r["msg"])
        self.assertIn("電話", r["msg"])                                                  # 電話欄不匯入，有講
        _, _, r = self.call("/setup/api/gsheet/sync", {"kind": "customers"})
        self.assertFalse(r["ok"])                                                        # 還沒綁管理員
        self._line_ready()
        _, _, r = self.call("/setup/api/gsheet/sync", {"kind": "customers"})
        self.assertTrue(r["ok"], r)
        tenant = self.holder.admin_tenant()
        custs = self.holder.app.customers.list_customers(tenant)
        self.assertEqual({c["label"] for c in custs}, {"合成甲", "合成乙"})
        self.assertNotIn("PHONE-NOT-IMPORTED", json.dumps(custs, ensure_ascii=False))
        self.assertTrue(self.call("/setup/api/gsheet/check",
                                  {"kind": "listings", "url": url.replace("gid=0", "gid=7")})[2]["ok"])
        self.assertTrue(self.call("/setup/api/gsheet/sync", {"kind": "listings"})[2]["ok"])
        self.assertEqual(len(self.holder.app.listings.list_all(tenant)), 1)
        # 排程每天同步也走同一條
        from web.scheduler import DailyScheduler
        sched = DailyScheduler(self.holder.app, self.holder.app.search, self.holder.app.customers, now=lambda: self.dt)
        sched.opener = self.net
        self.assertEqual(sorted(sched.sync_sheets()), [("customers", True), ("listings", True)])
        self.net.sheet_public = False
        _, _, r = self.call("/setup/api/gsheet/check", {"kind": "customers", "url": url})
        self.assertFalse(r["ok"])

    # ── 實價登錄 ──
    def test_lvr_save_and_query(self):
        self.login()
        _, _, r = self.call("/setup/api/lvr/check", {"counties": "台中市、宜蘭縣"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.settings.get("REA_LVR_COUNTIES"), "台中市、宜蘭縣")
        _, _, r = self.call("/setup/api/lvr/query", {"county": "台中市"})
        self.assertEqual(r["data"]["districts"], ["南屯區", "西屯區"])
        _, _, r = self.call("/setup/api/lvr/query", {"county": "台中市", "district": "西屯區"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["count"], 2)                                          # 親友交易排除、土地排除
        self.assertEqual(r["data"]["median_unit_wan_per_ping"], 51.2)
        n = len(self.net.calls)
        self.call("/setup/api/lvr/query", {"county": "台中市", "district": "西屯區"})
        self.assertEqual(len(self.net.calls), n)                                          # 同一季不重抓

    # ── 地政平台 ──
    def test_moi_save_confirm_cache_and_daily_limit(self):
        self.login()
        _, _, r = self.call("/setup/api/moi/check", {"client_id": "cid-synthetic", "secret": "wrong", "daily_limit": "1"})
        self.assertFalse(r["ok"])
        _, _, r = self.call("/setup/api/moi/check", {"client_id": "cid-synthetic", "secret": "sec-synthetic", "daily_limit": "1"})
        self.assertTrue(r["ok"], r)
        q = {"unit": "aa", "sec": "1", "no": "123-4"}
        _, _, r = self.call("/setup/api/moi/query", q)
        self.assertFalse(r["ok"])
        self.assertTrue(r["data"]["need_confirm"])
        self.assertEqual(self.net.moi_queries, 0)
        _, _, r = self.call("/setup/api/moi/query", {**q, "confirm": True})
        self.assertTrue(r["ok"], r)
        self.assertIn("30.4 坪", r["msg"])
        _, _, r = self.call("/setup/api/moi/query", {**q, "confirm": True})
        self.assertIn("沒有再收費", r["msg"])
        _, _, r = self.call("/setup/api/moi/query", {"unit": "AA", "sec": "0001", "no": "5", "confirm": True})
        self.assertIn("每日上限", r["msg"])
        self.assertEqual(self.net.moi_queries, 1)

    # ── 591／樂屋（搜尋服務）──
    def test_search_save_and_clear(self):
        self.login()
        _, _, r = self.call("/setup/api/search/check", {"provider": "brave", "key": "nope"})
        self.assertFalse(r["ok"])
        _, _, r = self.call("/setup/api/search/check", {"provider": "brave", "key": "brave-synthetic"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(os.environ["REA_SEARCH_KEY"], "brave-synthetic")
        _, _, r = self.call("/setup/api/clear", {"group": "search"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.settings.get("REA_SEARCH_KEY"), "")
        self.assertNotIn("REA_SEARCH_KEY", os.environ)

    def test_templates_download(self):
        self.login()
        st, hdrs, body = self.call("/setup/template/customers.csv")
        self.assertTrue(st.startswith("200"))
        self.assertIn("客戶編號", body)
        self.assertNotIn("電話", body)


if __name__ == "__main__":
    unittest.main()
