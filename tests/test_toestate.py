"""ToEstate（MCP，Streamable HTTP）串接＋「行情」指令。全部用假網路與合成數字，不連真實網站。"""
import datetime
import json
import tempfile
import unittest

from adapters.line import market_lookup as M
from adapters.storage import TenantStore
from adapters.toestate import ToEstateClient, ToEstateError, area_text, community_text
from core import market_query as Q

AREA = {"county": "臺中市", "town": "南屯區", "n_1y": 120, "median_ping_1y": 362000, "median_ping_1y_c": 362000,
        "median_ping_prev_1y": 380000, "p25_ping_1y": 290000, "p75_ping_1y": 470000, "median_total_1y": 15100000,
        "rent_n_1y": 30, "rent_median_ping_1y": 846.3}
COMMUNITY = {"community_id": 9, "name": "合成社區", "town": "南屯區", "building_type": "住宅大樓",
             "completion_date": "2024-07-07", "n_1y": 6, "median_ping_1y": 738000, "n_3y": 8, "median_ping_3y": 716000,
             "remark_summary": {"n_flags": 1},
             "summary": {"recent_sales": [{"date": "2026-06-26", "floor": 8, "floor_total": 15, "area_ping": 70.8,
                                           "rooms": 4, "total_price": 54780000, "unit_price_ping": 732612,
                                           "has_parking": True}]}}


class FakeResp:
    def __init__(self, status, body=b"", ctype="application/json", session=""):
        self.status, self._body = status, body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"Content-Type": ctype, **({"Mcp-Session-Id": session} if session else {})}

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeMcp:
    """假的 ToEstate MCP 伺服器。sse=True 用 text/event-stream 回；wrap=True 把結果包成 {"result": "<json>"}。"""

    def __init__(self, *, sse=False, wrap=True, status=None):
        self.sse, self.wrap, self.status = sse, wrap, status
        self.tool_calls, self.headers = [], []

    def __call__(self, req, timeout=None):
        self.headers.append(dict(req.headers))
        if self.status:
            return FakeResp(self.status, b"")
        msg = json.loads(req.data)
        if msg.get("method") == "initialize":
            return self._send({"protocolVersion": "2025-06-18", "capabilities": {}}, msg["id"], session="sess-1")
        if msg.get("method") == "notifications/initialized":
            return FakeResp(202, b"")
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        self.tool_calls.append((name, args))
        data = {"search": {"items": [{"kind": "community", "title": "合成社區", "community_id": 9},
                                     {"kind": "community", "title": "合成二社區", "community_id": 10}]},
                "get_community": COMMUNITY,
                "get_area_statistics": {"items": [AREA]}}[name]
        text = json.dumps({"result": json.dumps(data)} if self.wrap else data)
        return self._send({"content": [{"type": "text", "text": text}]}, msg["id"])

    def _send(self, result, rid, session=""):
        body = {"jsonrpc": "2.0", "id": rid, "result": result}
        if self.sse:
            return FakeResp(200, ("event: message\ndata: " + json.dumps(body) + "\n\n").encode(),
                            "text/event-stream", session)
        return FakeResp(200, body, session=session)


class ToEstateClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TenantStore(self.tmp.name)
        self.now = datetime.datetime(2026, 10, 29, 9)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, net, **kw):
        return ToEstateClient(self.store, opener=net, now=lambda: self.now, env={}, **kw)

    def test_handshake_session_and_double_wrapped_result(self):
        net = FakeMcp()
        c = self.client(net, key="synthetic-key")
        self.assertEqual(c.area_stats("台中市", "南屯區")["n_1y"], 120)
        self.assertEqual(net.headers[-1].get("Mcp-session-id"), "sess-1")         # 握手拿到的 session 有帶回去
        self.assertEqual(net.headers[-1].get("Authorization"), "Bearer synthetic-key")

    def test_sse_and_plain_json_both_work_and_no_key_sends_no_auth(self):
        for sse, wrap in ((True, True), (False, False)):
            net = FakeMcp(sse=sse, wrap=wrap)
            c = self.client(net)
            self.assertEqual(c.community(9)["name"], "合成社區")
            self.assertNotIn("Authorization", net.headers[-1])
            self.store.delete_tenant("sys-market")                       # 清快取，下一輪重新打

    def test_cache_and_daily_cap(self):
        net = FakeMcp()
        c = self.client(net, daily_cap=2)
        c.area_stats("台中市", "南屯區")
        c.area_stats("台中市", "南屯區")                                       # 快取，不算次數
        self.assertEqual(len(net.tool_calls), 1)
        c.community(9)
        with self.assertRaises(ToEstateError) as e:
            c.search("別的")
        self.assertIn("用完", str(e.exception))
        self.assertFalse(c.available)
        self.now += datetime.timedelta(days=1)
        self.assertTrue(c.available)                                          # 隔天自動恢復

    def test_429_stops_for_the_day_and_401_says_check_key(self):
        c = self.client(FakeMcp(status=429))
        with self.assertRaises(ToEstateError) as e:
            c.search("合成")
        self.assertIn("限流", str(e.exception))
        self.assertFalse(c.available)
        with self.assertRaises(ToEstateError) as e:
            ToEstateClient(TenantStore(self.tmp.name + "/b"), opener=FakeMcp(status=401), env={}).search("合成")
        self.assertIn("金鑰", str(e.exception))


class TextTests(unittest.TestCase):
    def test_area_text_converts_to_wan_and_shows_change(self):
        t = area_text(AREA, county="台中市", town="南屯區")
        self.assertIn("單價中位 36.2 萬/坪", t)
        self.assertIn("-4.7%", t)
        self.assertIn("1,510 萬", t)
        self.assertIn("已扣車位", t)

    def test_community_text_lists_recent_sales_and_flags(self):
        t = community_text(COMMUNITY)
        self.assertIn("合成社區", t)
        self.assertIn("2026-06 8F/15F 4房 70.8坪 5,478萬 73.3萬/坪 含車位", t)
        self.assertIn("瑕疵", t)


class QueryParseTests(unittest.TestCase):
    def test_parse(self):
        cases = {"台中市南屯區": ("area", "台中市", "南屯區"), "臺中南屯區": ("area", "台中市", "南屯區"),
                 "台中 南屯": ("area", "台中市", "南屯區"), "南屯": ("area", "台中市", "南屯區"),
                 "彰化縣員林市": ("area", "彰化縣", "員林市"), "台中": ("area", "台中市", None),
                 "語山簷": ("name", "語山簷", None), "員林": ("name", "員林", None)}
        for q, want in cases.items():
            with self.subTest(q=q):
                self.assertEqual(Q.parse(q, "台中市"), want)

    def test_split_command(self):
        self.assertEqual(Q.split_command("行情 南屯區"), "南屯區")
        self.assertEqual(Q.split_command("實價：語山簷"), "語山簷")
        self.assertEqual(Q.split_command("行情"), "")
        self.assertIsNone(Q.split_command("行情很好"))
        self.assertIsNone(Q.split_command("新增買方"))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TenantStore(self.tmp.name)
        self.moi_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def moi(self, county, town):
        self.moi_calls.append((county, town))
        return f"內政部概況 {county}{town}"

    def test_name_goes_to_community_with_other_matches_as_buttons(self):
        c = ToEstateClient(self.store, opener=FakeMcp(), env={})
        text, quick = M.lookup("合成社區", client=c, default_county="台中市", moi=self.moi)
        self.assertIn("合成社區", text)
        self.assertEqual(quick, ("行情 合成二社區",))

    def test_falls_back_to_moi_for_districts_when_toestate_fails(self):
        c = ToEstateClient(self.store, opener=FakeMcp(status=503), env={})
        text, _ = M.lookup("南屯", client=c, default_county="台中市", moi=self.moi)
        self.assertIn("改用內政部資料", text)
        self.assertEqual(self.moi_calls, [("台中市", "南屯區")])
        text, _ = M.lookup("合成社區", client=None, default_county="台中市", moi=self.moi)
        self.assertIn("沒有接上", text)

    def test_moi_area_text_from_synthetic_rows(self):
        rows = [{"date": "1150801", "type": "住宅大樓(11層含以上有電梯)", "ping": 35.0, "total_wan": 1500,
                 "unit_wan_per_ping": 42.9}]
        text = M.moi_area_text("台中市", "南屯區", fetch=lambda c: ("115S2", b""), parse=lambda b: [],
                               summary=lambda r, d, recent: {"count": 1, "median_unit_wan_per_ping": 42.9,
                                                             "recent": rows})
        self.assertIn("115S2", text)
        self.assertIn("42.9", text)



class WizardCheckTests(unittest.TestCase):
    def test_check_toestate_ok_bad_url_and_rate_limit(self):
        from adapters.integration_checks import check_toestate
        r = check_toestate("", "", opener=FakeMcp())
        self.assertTrue(r.ok, r.msg)
        self.assertIn("120 筆", r.msg)
        self.assertFalse(check_toestate("http://toestate.test/mcp", opener=FakeMcp()).ok)
        bad = check_toestate("", "", opener=FakeMcp(status=403))
        self.assertFalse(bad.ok)
        self.assertIn("金鑰", bad.msg)
        self.assertTrue(bad.fix)


if __name__ == "__main__":
    unittest.main()
