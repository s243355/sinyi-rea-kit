# -*- coding: utf-8 -*-
"""全店共用模式：同一個官方帳號底下的經紀人，買方與物件放在同一個店資料夾，互相看得到、互相配對。

開關：環境變數 REA_TEAM_MODE（串接精靈可設）。
- shared（本分支預設）：全店共用。每筆客戶／物件記下「誰建的」（owner＝建立者的租戶代號），
  大家都看得到、新物件會配到全店的買方；但「改」只有建立者本人或管理員（店長）能改。
- solo：跟原版一樣，每人各自獨立。

為什麼不直接讓大家共用一個租戶：LINE 綁定、草稿、推播額度、隱私匯出／刪除都還是要「一人一份」，
所以只有客戶與物件這兩本帳搬到店資料夾（sys-team），其他照舊。

兩種看法（view）：
- 全店（team）：給 LINE 上的新增／查詢／配對用。
- 只看自己的（own）：給每日排程、隱私匯出／刪除用。排程如果用全店，同一位客人會被每位同仁的報告各跑一次。
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping

from adapters.csv.customer_source import CsvCustomerSource
from adapters.csv.listing_source import CsvListingSource
from adapters.storage import TenantStore

TEAM_TENANT = "sys-team"                # 保留前綴 sys-：不會發給任何人當租戶，管理員面板也不會列成一位仲介
ROSTER = ("team", "roster.json")        # {租戶代號: 顯示名稱}，存在店資料夾
MODE_ENV = "REA_TEAM_MODE"
DEFAULT_MODE = "shared"


class NotOwner(ValueError):
    """不是建立者也不是管理員，不能改別人的客戶或物件。訊息直接回給仲介看。"""


def team_mode(env=os.environ) -> str:
    v = (env.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    return "solo" if v == "solo" else "shared"


def is_shared(env=os.environ) -> bool:
    return team_mode(env) == "shared"


# ── 名冊：卡片上「這是誰的客人」要顯示人名，不是租戶代號 ──

_NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,12}$")


def clean_name(value: str) -> str:
    name = (value or "").strip()
    if not _NAME_RE.match(name) or re.search(r"\d{6,}|@", name):
        raise ValueError("名稱 1～12 個字，不要放電話或 email。")
    return name


def set_name(store: TenantStore, tenant_id: str, name: str) -> str:
    name = clean_name(name)
    with store.lock(TEAM_TENANT, "roster"):
        roster = store.read_json(TEAM_TENANT, *ROSTER, default={}) or {}
        roster[tenant_id] = name
        store.write_json(TEAM_TENANT, *ROSTER, data=roster)
    return name


def drop_name(store: TenantStore, tenant_id: str) -> None:
    with store.lock(TEAM_TENANT, "roster"):
        roster = store.read_json(TEAM_TENANT, *ROSTER, default={}) or {}
        if roster.pop(tenant_id, None) is not None:
            store.write_json(TEAM_TENANT, *ROSTER, data=roster)


def name_of(store: TenantStore, tenant_id: str | None) -> str:
    """名冊有登記用名冊；沒有就用分享署名的第一段（「林小姐｜XX房屋」→ 林小姐）；都沒有叫「同事」。"""
    if not tenant_id:
        return "店內"
    roster = store.read_json(TEAM_TENANT, *ROSTER, default={}) or {}
    if roster.get(tenant_id):
        return roster[tenant_id]
    try:
        prof = (store.read_json(tenant_id, "line", "agent_profile.json", default={}) or {}).get("profile") or {}
    except ValueError:
        prof = {}
    sig = (prof.get("signature") or "").strip()
    if sig:
        first = re.split(r"[｜|/／,，\s]", sig)[0].strip()
        if first and not re.search(r"\d{6,}|@", first):
            return first[:12]
    return "同事"


def _decorate(store: TenantStore, viewer: str, rows: list[dict]) -> list[dict]:
    """別人建的資料加上 owner_name（卡片顯示「家儒的客人」）；自己的不加。直接改傳進來的 dict（都是剛讀出來的副本）。"""
    names: dict[str, str] = {}
    for r in rows:
        owner = r.get("owner")
        if owner and owner != viewer:
            if owner not in names:
                names[owner] = name_of(store, owner)
            r["owner_name"] = names[owner]
    return rows


def _mine_first(store: TenantStore, viewer: str, rows: list[dict]) -> list[dict]:
    _decorate(store, viewer, rows)
    return sorted(rows, key=lambda r: (bool(r.get("owner_name")), r.get("customer_id") or r.get("id") or ""))


# ── 權限 ──

def _always_admin(_tenant: str) -> bool:
    return False


class _Team:
    """客戶／物件兩種來源共用的「全店」行為。is_admin(租戶) 由組裝的地方給（看綁定的 LINE 是不是管理員）。"""

    def _space(self, tenant_id: str) -> str:
        return TEAM_TENANT if self.shared else tenant_id

    def _may_edit(self, tenant_id: str, record: Mapping[str, Any] | None) -> None:
        if not self.shared or record is None:
            return
        owner = record.get("owner")
        if owner in (None, tenant_id) or self.is_admin(tenant_id):
            return
        raise NotOwner(f"這是{name_of(self.store, owner)}的資料，只有他本人或店長可以修改。")


class TeamCustomerSource(_Team, CsvCustomerSource):
    def __init__(self, store: TenantStore, now=None, *, shared: bool | None = None,
                 is_admin: Callable[[str], bool] = _always_admin):
        super().__init__(store, **({"now": now} if now else {}))
        self.shared = is_shared() if shared is None else shared
        self.is_admin = is_admin

    # 寫入：記下建立者；改別人的要擋
    def import_customers(self, tenant_id: str, data, **kw):
        space = self._space(tenant_id)
        rep = super().import_customers(space, data, **kw)
        if self.shared:
            self._stamp(space, tenant_id, rep.new_ids)
        return rep

    def save_need(self, tenant_id: str, *, label: str, need: Mapping[str, Any], customer_id: str | None = None,
                  expected_version: int | None = None) -> dict:
        space = self._space(tenant_id)
        if customer_id:
            self._may_edit(tenant_id, super().get(space, customer_id))
        rec = super().save_need(space, label=label, need=need, customer_id=customer_id,
                                expected_version=expected_version)
        if self.shared and not customer_id:
            rec = self._stamp(space, tenant_id, [rec["customer_id"]])[0]
        return rec

    def update_identity(self, tenant_id: str, customer_id: str, **kw) -> dict:
        space = self._space(tenant_id)
        self._may_edit(tenant_id, super().get(space, customer_id))
        return super().update_identity(space, customer_id, **kw)

    def bind_line_target(self, tenant_id: str, customer_id: str, line_target: str, members: int | None = None) -> None:
        space = self._space(tenant_id)
        self._may_edit(tenant_id, super().get(space, customer_id))
        super().bind_line_target(space, customer_id, line_target, members)

    # 讀取：全店
    def list_customers(self, tenant_id: str, *, active_only: bool = True) -> list[dict]:
        rows = super().list_customers(self._space(tenant_id), active_only=active_only)
        if not self.shared or tenant_id == TEAM_TENANT:
            return rows
        return _mine_first(self.store, tenant_id, rows)

    def get(self, tenant_id: str, customer_id: str) -> dict | None:
        rec = super().get(self._space(tenant_id), customer_id)
        if rec is not None and self.shared and tenant_id != TEAM_TENANT:
            _mine_first(self.store, tenant_id, [rec])
        return rec

    def import_groups(self, tenant_id: str, line_events: Iterable[dict]) -> list[str]:
        return super().import_groups(tenant_id, line_events)            # 群組名冊仍是每人一份

    # 匯出／刪除：只有自己建的（個資：受託處理的範圍是「這位仲介的客人」）
    def export(self, tenant_id: str) -> str:
        if not self.shared:
            return super().export(tenant_id)
        return OwnCustomers(self, tenant_id).export_csv()

    def delete_all(self, tenant_id: str) -> None:
        if not self.shared:
            return super().delete_all(tenant_id)
        with self.store.lock(TEAM_TENANT, "customers"):
            book = self._book(TEAM_TENANT)
            keep = {k: v for k, v in book.items() if v.get("owner") != tenant_id}
            self.store.write_json(TEAM_TENANT, *self.CUSTOMERS, data=keep)

    def own(self, tenant_id: str) -> list[dict]:
        rows = self.list_customers(tenant_id, active_only=False)
        return [c for c in rows if not self.shared or c.get("owner") in (None, tenant_id)]

    def own_view(self) -> "OwnCustomerView":
        return OwnCustomerView(self)

    def _stamp(self, space: str, tenant_id: str, ids: Iterable[str]) -> list[dict]:
        out = []
        with self.store.lock(space, "customers"):
            book = self._book(space)
            for cid in ids:
                if cid in book and not book[cid].get("owner"):
                    book[cid]["owner"] = tenant_id
                if cid in book:
                    out.append(deepcopy(book[cid]))
            self.store.write_json(space, *self.CUSTOMERS, data=book)
        return out


class OwnCustomers:
    """把「我自己的客人」輸出成 CSV（跟原版欄位一樣）。"""

    def __init__(self, src: TeamCustomerSource, tenant_id: str):
        self.src, self.tenant_id = src, tenant_id

    def export_csv(self) -> str:
        import csv
        import io
        from adapters.csv.customer_source import EXPORT_FIELDS
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, lineterminator="\n")
        w.writeheader()
        for c in self.src.own(self.tenant_id):
            need = c.get("need_card") or {}
            blank = lambda k: need.get(k) if need.get(k) is not None else ""
            w.writerow({"customer_id": c["customer_id"], "label": c.get("label") or "", "status": c.get("status"),
                        "transaction": need.get("transaction"),
                        "preferred_areas": "、".join(need.get("preferred_areas") or []),
                        "budget_min": blank("budget_min"), "budget_max": blank("budget_max"),
                        "property_type": need.get("property_type") or "", "rooms_min": blank("rooms_min"),
                        "line_target": c.get("line_target") or "", "members": c.get("members") or ""})
        return buf.getvalue()


class OwnCustomerView:
    """排程與隱私功能用：list_customers 只回自己建的，其他方法照全店來源。"""

    def __init__(self, src: TeamCustomerSource):
        self._src = src

    def list_customers(self, tenant_id: str, *, active_only: bool = True) -> list[dict]:
        rows = self._src.own(tenant_id)
        return [c for c in rows if c.get("status") == "active"] if active_only else rows

    def __getattr__(self, name):
        return getattr(self._src, name)


class TeamListingSource(_Team, CsvListingSource):
    def __init__(self, store: TenantStore, now=None, *, shared: bool | None = None,
                 is_admin: Callable[[str], bool] = _always_admin):
        super().__init__(store, **({"now": now} if now else {}))
        self.shared = is_shared() if shared is None else shared
        self.is_admin = is_admin

    def import_csv(self, tenant_id: str, data, **kw):
        space = self._space(tenant_id)
        rep = super().import_csv(space, data, **kw)
        if self.shared:
            self._stamp(space, tenant_id, rep.new_ids)
            self._owners_from_roster(space, kw.get("source", "csv"))
        return rep

    def _owners_from_roster(self, space: str, source: str) -> None:
        """店內物件表有「負責同仁」欄：名字對得到名冊的，這間就算那位同仁的（他可以改、配對會通知他）。"""
        roster = self.store.read_json(TEAM_TENANT, *ROSTER, default={}) or {}
        by_name = {}
        for tenant, name in roster.items():
            by_name.setdefault(name, []).append(tenant)
        with self.store.lock(space, "listings"):
            snap = self._snapshot(space)
            changed = False
            for x in snap.values():
                hit = by_name.get(x.get("agent_name") or "")
                if x.get("source") == source and hit and len(hit) == 1 and x.get("owner") != hit[0]:
                    x["owner"] = hit[0]                     # 同名兩位以上就不猜，留給原本的建立者
                    changed = True
            if changed:
                self.store.write_json(space, *self.SNAPSHOT, data=snap)

    def save_one(self, tenant_id: str, item: Mapping[str, Any]) -> dict:
        space = self._space(tenant_id)
        rec = super().save_one(space, item)
        if self.shared:
            rec = self._stamp(space, tenant_id, [rec["id"]])[0]
        return rec

    def mark_online(self, tenant_id: str, listing_id: str, online: bool) -> bool:
        space = self._space(tenant_id)
        self._may_edit(tenant_id, super().get(space, listing_id))
        return super().mark_online(space, listing_id, online)

    def list_all(self, tenant_id: str) -> list[dict]:
        rows = super().list_all(self._space(tenant_id))
        return _decorate(self.store, tenant_id, rows) if self.shared and tenant_id != TEAM_TENANT else rows

    def get(self, tenant_id: str, listing_id: str) -> dict | None:
        rec = super().get(self._space(tenant_id), listing_id)
        if rec is not None and self.shared and tenant_id != TEAM_TENANT:
            _decorate(self.store, tenant_id, [rec])
        return rec

    def changes_since(self, tenant_id: str, since: str | None = None) -> list[dict]:
        return super().changes_since(self._space(tenant_id), since)

    def own(self, tenant_id: str) -> list[dict]:
        rows = self.list_all(tenant_id)
        return [x for x in rows if not self.shared or x.get("owner") in (None, tenant_id)]

    def delete_all(self, tenant_id: str) -> None:
        if not self.shared:
            return super().delete_all(tenant_id)
        with self.store.lock(TEAM_TENANT, "listings"):
            snap = self._snapshot(TEAM_TENANT)
            keep = {k: v for k, v in snap.items() if v.get("owner") != tenant_id}
            self.store.write_json(TEAM_TENANT, *self.SNAPSHOT, data=keep)

    def own_view(self) -> "OwnListingView":
        return OwnListingView(self)

    def _stamp(self, space: str, tenant_id: str, ids: Iterable[str]) -> list[dict]:
        out = []
        with self.store.lock(space, "listings"):
            snap = self._snapshot(space)
            for lid in ids:
                if lid in snap and not snap[lid].get("owner"):
                    snap[lid]["owner"] = tenant_id
                if lid in snap:
                    out.append(dict(snap[lid]))
            self.store.write_json(space, *self.SNAPSHOT, data=snap)
        return out


class OwnListingView:
    def __init__(self, src: TeamListingSource):
        self._src = src

    def list_all(self, tenant_id: str) -> list[dict]:
        return self._src.own(tenant_id)

    def list_active(self, tenant_id: str) -> list[dict]:
        return [x for x in self._src.own(tenant_id) if x.get("online")]

    def __getattr__(self, name):
        return getattr(self._src, name)
