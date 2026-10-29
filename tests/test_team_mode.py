"""全店共用模式（REA_TEAM_MODE=shared）：買方與物件全店看得到、互相配對；只有建立者或店長能改。合成資料、本機假 LINE。"""
import datetime
import os
import tempfile
import unittest
from unittest import mock

import test_customer_intake as support
import test_listing_match_flow as flow

from adapters import team
from adapters.storage import TenantStore
from adapters.team import NotOwner, TEAM_TENANT, TeamCustomerSource, TeamListingSource
from adapters.line.fake_api import synthetic_line_id

LISTINGS_CSV = ("物件編號,案名,交易,縣市,區域,總價(萬),型態,格局,坪數,屋齡,網址,上架日,狀態\r\n"
                "S-001,合成之一,售,台中市,西屯區,1200,公寓,3房,35,12,https://example.test/s1,2026-10-01,上架\r\n")


class TeamSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TenantStore(self.tmp.name)
        self.admin = {"tenant-boss"}
        is_admin = lambda t: t in self.admin
        self.customers = TeamCustomerSource(self.store, shared=True, is_admin=is_admin)
        self.listings = TeamListingSource(self.store, shared=True, is_admin=is_admin)
        team.set_name(self.store, "tenant-a", "合成甲")
        team.set_name(self.store, "tenant-b", "合成乙")

    def tearDown(self):
        self.tmp.cleanup()

    def test_everyone_sees_the_store_pool_with_owner_names(self):
        a = self.customers.save_need("tenant-a", label="甲的客人", need=flow.BUYER)
        b = self.customers.save_need("tenant-b", label="乙的客人", need=flow.BUYER)
        self.assertEqual((a["owner"], b["owner"]), ("tenant-a", "tenant-b"))
        seen = self.customers.list_customers("tenant-a")
        self.assertEqual([c["label"] for c in seen], ["甲的客人", "乙的客人"])        # 自己的排前面
        self.assertNotIn("owner_name", seen[0])
        self.assertEqual(seen[1]["owner_name"], "合成乙")
        # 資料只放在店資料夾，不在個人資料夾
        self.assertFalse(self.store.path("tenant-a", "customers", "customers.json").exists())
        self.assertTrue(self.store.path(TEAM_TENANT, "customers", "customers.json").exists())
        # 加上的顯示名稱不會被寫回檔案
        raw = self.store.read_json(TEAM_TENANT, "customers", "customers.json")
        self.assertTrue(all("owner_name" not in c for c in raw.values()))

    def test_only_owner_or_admin_can_edit(self):
        a = self.customers.save_need("tenant-a", label="甲的客人", need=flow.BUYER)
        cid = a["customer_id"]
        with self.assertRaises(NotOwner) as e:
            self.customers.update_identity("tenant-b", cid, status="paused")
        self.assertIn("合成甲", str(e.exception))
        with self.assertRaises(NotOwner):
            self.customers.save_need("tenant-b", label="改掉", need=flow.BUYER, customer_id=cid, expected_version=1)
        self.assertEqual(self.customers.update_identity("tenant-a", cid, status="paused")["status"], "paused")
        self.assertEqual(self.customers.update_identity("tenant-boss", cid, status="active")["status"], "active")
        rec = self.listings.save_one("tenant-a", {"transaction": "buy", "area": "台中市西屯區", "price": 1200,
                                                  "price_unit": "萬", "own": True, "source": "line"})
        with self.assertRaises(NotOwner):
            self.listings.mark_online("tenant-b", rec["id"], False)
        self.assertTrue(self.listings.mark_online("tenant-boss", rec["id"], False))

    def test_own_view_export_and_delete_cover_only_my_records(self):
        self.customers.save_need("tenant-a", label="甲的客人", need=flow.BUYER)
        self.customers.save_need("tenant-b", label="乙的客人", need=flow.BUYER)
        own = self.customers.own_view()
        self.assertEqual([c["label"] for c in own.list_customers("tenant-a")], ["甲的客人"])
        csv_text = self.customers.export("tenant-a")
        self.assertIn("甲的客人", csv_text)
        self.assertNotIn("乙的客人", csv_text)
        self.customers.delete_all("tenant-a")
        self.assertEqual([c["label"] for c in self.customers.list_customers("tenant-b")], ["乙的客人"])

    def test_sheet_sync_does_not_take_down_listings_added_on_line(self):
        line_rec = self.listings.save_one("tenant-a", {"transaction": "buy", "area": "台中市西屯區", "price": 980,
                                                       "price_unit": "萬", "own": True, "source": "line"})
        rep = self.listings.import_csv("tenant-boss", LISTINGS_CSV, source="gsheet", replace=True)
        self.assertEqual(rep.new_ids, ["S-001"])
        self.assertTrue(self.listings.get("tenant-b", line_rec["id"])["online"])
        rep = self.listings.import_csv("tenant-boss", LISTINGS_CSV.split("\r\n")[0] + "\r\n", source="gsheet",
                                       replace=True)
        self.assertEqual(rep.removed_ids, ["S-001"])                      # 表上拿掉的才下架
        self.assertTrue(self.listings.get("tenant-b", line_rec["id"])["online"])

    def test_store_sheet_without_transaction_column_and_agent_owner(self):
        sheet = ("委託編號,案名,區域,售價,型態,格局,權狀坪數,屋齡,負責同仁,屋主電話\r\n"
                 "T-9,合成表單案,台中市南屯區,1350,公寓,3房2廳,36,10,合成乙,0000\r\n")
        rep = self.listings.import_csv("tenant-boss", sheet, source="gsheet", default_transaction="buy")
        self.assertEqual(rep.new_ids, ["T-9"])
        self.assertIn("屋主電話", rep.ignored_sensitive_columns)            # 電話欄整欄不收
        rec = self.listings.get("tenant-a", "T-9")
        self.assertEqual((rec["transaction"], rec["owner"], rec["agent_name"]), ("buy", "tenant-b", "合成乙"))
        self.assertEqual(rec["owner_name"], "合成乙")
        self.assertTrue(self.listings.mark_online("tenant-b", "T-9", False))   # 負責同仁自己能下架
        with self.assertRaises(NotOwner):
            self.listings.mark_online("tenant-a", "T-9", True)

    def test_sheet_new_listings_notify_every_matching_buyer_owner(self):
        from web.jobs.team_notify import notify_new_listings
        self.customers.save_need("tenant-a", label="甲的買方", need=flow.BUYER)
        self.customers.save_need("tenant-b", label="乙的買方", need=flow.BUYER)
        self.customers.save_need("tenant-b", label="乙的南部買方", need={**flow.BUYER, "preferred_areas": ["高雄市三民區"]})
        self.listings.import_csv("tenant-boss", LISTINGS_CSV, source="gsheet")
        told = []
        sent = notify_new_listings(self.listings.list_all(TEAM_TENANT), self.customers.list_customers(TEAM_TENANT),
                                   lambda t, text: told.append((t, text)))
        self.assertEqual(sent, {"tenant-a": 1, "tenant-b": 1})
        self.assertEqual([t for t, _ in told], ["tenant-a", "tenant-b"])
        self.assertIn("S-001", told[1][1])
        self.assertIn("乙的買方", told[1][1])
        self.assertNotIn("南部", told[1][1])
        self.assertNotIn("甲的買方", told[1][1])                          # 只告訴他自己的買方

    def test_names_fall_back_to_signature_then_colleague(self):
        self.store.write_json("tenant-c", "line", "agent_profile.json",
                              data={"profile": {"signature": "合成丙｜示範店 分機123"}})
        self.assertEqual(team.name_of(self.store, "tenant-c"), "合成丙")
        self.assertEqual(team.name_of(self.store, "tenant-d"), "同事")
        with self.assertRaises(ValueError):
            team.set_name(self.store, "tenant-a", "合成" + "1" * 8)

    def test_solo_mode_keeps_original_isolation(self):
        solo = TeamCustomerSource(self.store, shared=False)
        solo.save_need("tenant-a", label="甲的客人", need=flow.BUYER)
        self.assertEqual(solo.list_customers("tenant-b"), [])
        self.assertTrue(self.store.path("tenant-a", "customers", "customers.json").exists())

    def test_mode_switch(self):
        self.assertEqual(team.team_mode({}), "shared")
        self.assertEqual(team.team_mode({"REA_TEAM_MODE": "solo"}), "solo")
        self.assertEqual(team.team_mode({"REA_TEAM_MODE": "亂打"}), "shared")


class TeamLineFlowTests(unittest.TestCase):
    """兩位同仁接同一個官方帳號：乙的買方 → 甲新增物件 → 甲看到乙的買方、乙收到通知。"""

    tap = flow.ListingMatchFlowTests.tap
    last = flow.ListingMatchFlowTests.last
    add_listing = flow.ListingMatchFlowTests.add_listing
    cards = flow.ListingMatchFlowTests.cards
    send = support.CustomerIntakeTests.send

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"REA_TEAM_MODE": "shared"})
        self.env.start()
        support.CustomerIntakeTests.setUp(self)                    # 甲＝tenant-a
        self.customers = self.app.team_customers
        self.uid_b = synthetic_line_id("U", "team-colleague")
        self.send("綁定 " + self.app.registry.issue_code("tenant-b"), user=self.uid_b)
        team.set_name(self.store, "tenant-a", "合成甲")
        team.set_name(self.store, "tenant-b", "合成乙")

    def tearDown(self):
        support.CustomerIntakeTests.tearDown(self)
        self.env.stop()

    def test_new_listing_matches_colleagues_buyer_and_notifies_them(self):
        self.customers.save_need("tenant-b", label="乙的買方", need=flow.BUYER)
        pushes_before = len(self.api.pushes)
        reply = self.add_listing()
        self.assertIn("買方裡有 1 位", reply[0]["text"])
        self.assertIn("已通知 1 位同事", reply[0]["text"])
        titles = " ".join(str(b) for b in self.cards(reply))
        self.assertIn("乙的買方（合成乙的客人）", titles)
        new = self.api.pushes[pushes_before:]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["to"], self.uid_b)
        text = new[0]["messages"][0]["text"]
        self.assertIn("合成甲新增了", text)
        self.assertIn("乙的買方", text)

    def test_my_listings_shows_only_mine_and_colleague_cannot_toggle(self):
        self.add_listing()
        lid = self.listings_of("tenant-a")[0]["id"]
        self.assertIn("你還沒有物件", self.send("我的物件", user=self.uid_b)["text"])
        self.assertIn(lid, self.send("我的物件")["text"])
        self.assertTrue(self.app.team_listings.get("tenant-b", lid)["online"])   # 看得到

    def test_own_buyer_still_matches_without_notifying_anyone(self):
        self.customers.save_need("tenant-a", label="甲自己的買方", need=flow.BUYER)
        pushes_before = len(self.api.pushes)
        reply = self.add_listing()
        self.assertNotIn("已通知", reply[0]["text"])
        self.assertEqual(self.api.pushes[pushes_before:], [])

    def test_delete_my_data_removes_only_my_store_records(self):
        self.customers.save_need("tenant-a", label="甲的買方", need=flow.BUYER)
        self.customers.save_need("tenant-b", label="乙的買方", need=flow.BUYER)
        self.send("刪除我的資料")
        self.send("確定刪除我的資料")
        left = [c["label"] for c in self.app.team_customers.list_customers(team.TEAM_TENANT)]
        self.assertEqual(left, ["乙的買方"])

    def test_daily_sheet_sync_pushes_to_buyer_owner(self):
        from web.jobs.team_notify import after_sheet_sync
        self.customers.save_need("tenant-b", label="乙的買方", need=flow.BUYER)
        rep = self.app.listings.import_csv("tenant-a", LISTINGS_CSV, source="gsheet", replace=True)
        before = len(self.api.pushes)
        self.assertEqual(after_sheet_sync(self.app, rep.new_ids), {"tenant-b": 1})
        new = self.api.pushes[before:]
        self.assertEqual([p["to"] for p in new], [self.uid_b])
        self.assertIn("店內物件表", new[0]["messages"][0]["text"])
        self.assertEqual(after_sheet_sync(self.app, []), {})

    def listings_of(self, tenant):
        return [x for x in self.app.team_listings.list_all(tenant) if not x.get("owner_name")]


if __name__ == "__main__":
    unittest.main()
