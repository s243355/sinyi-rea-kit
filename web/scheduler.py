"""每天自動跑「今日報告」（海巡＋各平台新物件），到仲介設定的時間就推給他本人。

服務裡的一條背景執行緒，每分鐘看一次；一位仲介一天一則報告（跑過就記下，重開服務也不會重跑），
報告之後 13:00、19:00 各再巡一次貼文，有新的才推「海巡加報」。
沒接搜尋服務就整個不啟動。出錯只記錄，不影響 LINE 回覆。
"""
from __future__ import annotations

import datetime
import threading

from web.jobs import daily_brief


class DailyScheduler:
    def __init__(self, app, search, customers, *, now=datetime.datetime.now, interval: float = 60.0):
        self.app, self.search, self.customers, self.now, self.interval = app, search, customers, now, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._quota_day = None
        self._sheet_day = None
        self.opener = None                         # 測試換成假網路

    def sync_sheets(self) -> list[tuple[str, bool]]:
        """串接精靈設定的 Google 試算表 → 管理員的資料夾。回 [(種類, 成功?)]。"""
        import os
        from adapters.gsheet_sync import ENV, sync
        admins = [u.strip() for u in os.environ.get("REA_ADMIN_USERS", "").split(",") if u.strip()]
        tenant = next((t for t in (self.app.registry.tenant_of(u) for u in admins) if t), None)
        out = []
        for kind, key in ENV.items():
            if not tenant or not os.environ.get(key):
                continue
            try:
                res = sync(kind, tenant, customers=self.customers, listings=getattr(self.app, "listings", None),
                           opener=self.opener)
                out.append((kind, res.ok))
                self._health("gsheet_" + kind, res.ok, "" if res.ok else res.msg)
                if res.ok and kind == "listings":       # 全店共用：新物件配到誰的買方就通知誰
                    from web.jobs.team_notify import after_sheet_sync
                    after_sheet_sync(self.app, res.data.get("new_ids"))
                if not res.ok:
                    self.app._audit("gsheet_sync_failed", kind=kind)
            except Exception as e:
                self.app._audit("gsheet_sync_error", kind=kind, err=type(e).__name__)
                out.append((kind, False))
        return out

    def tick(self) -> list[tuple[str, str]]:
        """跑一輪：回 [(租戶, 結果)]。測試直接呼叫這個。"""
        done = []
        now = self.now()
        if self._quota_day != now.date() and hasattr(self.app, "sync_quota"):
            self._quota_day = now.date()           # 每天向 LINE 查一次官方帳號月額度（升級方案後推播上限自動跟上）
            try:
                self.app.sync_quota()
            except Exception as e:
                self.app._audit("quota_sync_error", err=f"{type(e).__name__}: {str(e)[:120]}")
            self.check_line_auth()
        if self._sheet_day != now.date() and now.hour >= 6:
            self._sheet_day = now.date()           # 每天早上同步一次串接的 Google 試算表（沒串就什麼都不做）
            self.sync_sheets()
        if not self.search.available:
            return done
        tenants = self.app.registry.tenants()
        if tenants:                                # 搜尋額度全站共用：每天換人排第一，不會永遠是同幾位拿不到
            k = now.toordinal() % len(tenants)
            tenants = tenants[k:] + tenants[:k]
        for tenant in tenants:
            slot = None
            try:
                main_due = daily_brief.due(self.app.store, tenant, now)
                if not main_due:
                    # 報告跑過了 → 看有沒有到加報時段（13:00、19:00 再巡一次貼文，有新的才推）
                    slot = daily_brief.due_extra(self.app.store, tenant, now)
                    if not slot:
                        continue
                msgs, result, commit = daily_brief.run_for_tenant(tenant_id=tenant, store=self.app.store,
                                                                  search=self.search, customers_src=self.customers,
                                                                  now=self.now, patrol_only=bool(slot), slot=slot)
                # 推播紀錄不留內容（含別人的貼文摘要）；真的送到才把網址記成看過
                kind = "patrol_extra" if slot else "daily_brief"
                state = self.app.notify(tenant, msgs, kind=kind, keep_payload=False).state if msgs else "nothing"
                if state == "sent":
                    commit()
                if slot:
                    daily_brief.mark_extra(self.app.store, tenant, now, slot, result, state)
                else:
                    daily_brief.mark_run(self.app.store, tenant, now, result, state)
                done.append((tenant, state))
            except Exception as e:                       # 一位仲介出錯不拖垮其他人；記成這次跑過，下個時段再跑（不每分鐘重試燒額度）
                self.app._audit("daily_brief_error", tenant=tenant, err=f"{type(e).__name__}: {str(e)[:120]}")
                if slot:
                    daily_brief.mark_extra(self.app.store, tenant, now, slot, {"errors": [type(e).__name__]}, "error")
                else:
                    daily_brief.mark_run(self.app.store, tenant, now, {"errors": [type(e).__name__]}, "error")
                done.append((tenant, "error"))
        return done

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:                       # 任何意外都不能讓排程執行緒死掉
                try:
                    self.app._audit("scheduler_error", err=f"{type(e).__name__}: {str(e)[:120]}")
                except Exception:
                    pass

    def start(self) -> "DailyScheduler":
        if self._thread is None:                    # 額度同步、方案通知、今日報告（沒接搜尋服務就只做前兩樣）
            self._thread = threading.Thread(target=self._loop, name="daily-brief", daemon=True)
            self._thread.start()
        return self

    def stop(self, wait: float = 30.0):
        """停掉。等舊的這一輪跑完再回來（串接精靈換金鑰時新舊排程不會同時跑、重複推今日報告）。"""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(wait)

    def _health(self, name: str, ok: bool, msg: str) -> None:
        try:
            from adapters import integration_status
            from adapters.settings_store import SettingsStore
            integration_status.record(name, ok, msg, settings=SettingsStore(self.app.store.root), now=self.now)
        except Exception:
            pass

    def check_line_auth(self) -> bool | None:
        """每天問 LINE 一次「我是誰」：token 被重發或刪掉時，服務會整個收不到也回不了，要在精靈上看得到。"""
        fn = getattr(getattr(self.app, "client", None), "bot_info_status", None)
        if fn is None:
            return None
        status = fn()
        if status == 200:
            self._health("line", True, "")
            return True
        if status in (401, 403):
            self._health("line", False, "LINE access token 失效（可能被重新發行或刪除），請到串接精靈第 3 步重貼")
            return False
        return None                                # 連不上之類的暫時問題不算壞
