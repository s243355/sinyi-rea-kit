"""開放給大家（負責人 2026-10-04：「要上線了，給大家使用」）：加好友按「同意並開始」就能用、每日名額、
回來的人用同一份資料、服務條款頁、LINE 月額度自動跟上、同樣的搜尋共用結果。合成資料、本機假 LINE、不連網。"""
import datetime
import json
import tempfile
import unittest

import test_customer_intake as support
from core import brand
from adapters.line.fake_api import synthetic_line_id
from adapters.line.shared import BindingRegistry, SYS_TENANT, TERMS_VERSION
from adapters.storage import TenantStore
from adapters.web_search import BudgetedSearch, SearchHit
from web import terms_page

NEW = synthetic_line_id("U", "public-newcomer")


class SignupTests(unittest.TestCase):
    setUp = support.CustomerIntakeTests.setUp
    tearDown = support.CustomerIntakeTests.tearDown
    send = support.CustomerIntakeTests.send

    def test_agree_then_use_everything_without_a_code(self):
        card = self.send("你好", user=NEW)
        self.assertIn("同意並開始", json.dumps(card, ensure_ascii=False))
        self.assertIsNone(self.app.registry.tenant_of(NEW))
        self.send("同意並開始", user=NEW)
        tenant = self.app.registry.tenant_of(NEW)
        self.assertTrue(tenant and tenant.startswith("a") and NEW not in tenant)     # 資料夾名稱看不出 LINE 帳號
        self.assertEqual(self.api.replies[-1]["messages"][1]["altText"], f"歡迎使用{brand.NAME}")
        consent = self.store.read_json(tenant, "line", "consent.json")
        self.assertEqual(consent["terms"], TERMS_VERSION)
        self.assertIn("客戶暱稱", self.send("新增買方", user=NEW)["text"])          # 直接能用
        self.assertNotIn(NEW, (self.store.tenant_dir(SYS_TENANT) / "audit.jsonl").read_text(encoding="utf-8"))

    def test_block_and_come_back_keeps_the_same_data(self):
        self.send("同意並開始", user=NEW)
        tenant = self.app.registry.tenant_of(NEW)
        self.app.registry.unbind_user(NEW)
        self.dt += datetime.timedelta(days=3)
        self.assertIn("歡迎回來", self.send("同意並開始", user=NEW)["text"])
        self.assertEqual(self.app.registry.tenant_of(NEW), tenant)

    def test_daily_cap_and_group_cannot_sign_up(self):
        self.app.signup_cap = 1
        self.send("同意並開始", user=NEW)
        other = synthetic_line_id("U", "public-second")
        self.assertIn("名額滿了", self.send("同意並開始", user=other)["text"])
        self.assertIsNone(self.app.registry.tenant_of(other))
        self.dt += datetime.timedelta(days=1)
        self.send("同意並開始", user=other)
        self.assertIsNotNone(self.app.registry.tenant_of(other))
        reg = BindingRegistry(self.store, now=self.now)
        self.assertEqual(reg.signup(synthetic_line_id("C", "group"))[0], None)

    def test_old_codes_still_work(self):
        code = self.app.registry.issue_code("tenant-z")
        self.send(code, user=NEW)
        self.assertEqual(self.app.registry.tenant_of(NEW), "tenant-z")


class QuotaTests(unittest.TestCase):
    setUp = support.CustomerIntakeTests.setUp
    tearDown = support.CustomerIntakeTests.tearDown
    send = support.CustomerIntakeTests.send

    def test_plan_cap_follows_line(self):
        self.assertEqual(self.app.budget().policy.plan_cap, 200)
        self.app._client = type("C", (), {"quota": lambda self: {"type": "limited", "value": 5000}})()
        self.assertEqual(self.app.sync_quota(), 5000)
        self.assertEqual(self.app.budget().policy.plan_cap, 5000)                     # 升級方案後自動跟上
        self.app._client = type("C", (), {"quota": lambda self: None})()
        self.assertIsNone(self.app.sync_quota())
        self.assertEqual(self.app.budget().policy.plan_cap, 5000)                     # 查不到就維持


class TermsPageTests(unittest.TestCase):
    def test_renders_both_documents_without_internal_notes(self):
        page = terms_page.render("合成提供者").decode("utf-8")
        self.assertIn("使用條款", page)
        self.assertIn("委託處理條款", page)
        self.assertIn("合成提供者", page)
        self.assertNotIn("{提供者姓名}", page)
        self.assertNotIn("不是法律意見", page)                  # 開頭的內部說明不對外
        self.assertIn("<table>", page)
        self.assertIn("noindex", page)

    def test_default_name_and_wsgi(self):
        self.assertIn("本服務提供者", terms_page.render("").decode("utf-8"))
        got = {}
        body = terms_page.app({"REQUEST_METHOD": "GET"}, lambda s, h: got.update(status=s))
        self.assertEqual(got["status"], "200 OK")
        self.assertTrue(body[0].startswith(b"<!doctype html>"))
        terms_page.app({"REQUEST_METHOD": "POST"}, lambda s, h: got.update(status=s))
        self.assertTrue(got["status"].startswith("405"))


class SearchCacheTests(unittest.TestCase):
    def test_same_query_shared_within_ttl_and_not_counted(self):
        class P:
            name, calls = "fake", 0

            def search(self, q, **kw):
                P.calls += 1
                return [SearchHit("t", "https://example.test/x", "s")]
        with tempfile.TemporaryDirectory() as d:
            store, now = TenantStore(d), [datetime.datetime(2026, 10, 4, 9)]
            s = BudgetedSearch(P(), store, now=lambda: now[0], cache_ttl=datetime.timedelta(hours=2))
            s.search("tenant-a", "site:threads.com 宜蘭 買房")
            self.assertEqual(s.search("tenant-b", "site:threads.com 宜蘭 買房")[0].url, "https://example.test/x")
            self.assertEqual(P.calls, 1)
            self.assertEqual(store.read_json(SYS_TENANT, "search", "usage.json")["total"], 1)
            s.search("tenant-b", "site:threads.com 宜蘭 買房", recency="w")              # 不同的查詢照樣搜
            self.assertEqual(P.calls, 2)
            now[0] += datetime.timedelta(hours=3)
            s.search("tenant-c", "site:threads.com 宜蘭 買房")                          # 過期重搜
            self.assertEqual(P.calls, 3)
            plain = BudgetedSearch(P(), store, now=lambda: now[0])                       # 沒開快取的照舊
            plain.search("tenant-a", "site:threads.com 宜蘭 買房")
            self.assertEqual(P.calls, 4)


if __name__ == "__main__":
    unittest.main()
