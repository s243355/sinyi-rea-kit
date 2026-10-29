# -*- coding: utf-8 -*-
"""ListingSource 的第一個實作：租戶自己上傳的物件 CSV（自家 Excel、刊登平台後台匯出都行）。

不是爬蟲：只吃租戶主動提供的檔。之後的公開頁轉接器輸出同一個正規化格式，配對引擎不用改。

正規化後的物件（core/listing_matching 與 Flex 卡共用的契約，欄位說明見 docs/mcp_tools.md）：
  id, transaction(buy|rent), city, area, title, price(售：萬；租：元/月), property_type,
  rooms, size_ping, age_years, floor, url, photo_url, listed_at, online, source, updated_at

資料最小化：地址門牌、屋主、電話、身分證、備註這類欄位一律不匯入（匯入報告會列出被略過的欄名）。
客戶看得到的只有物件本身與公開連結。

整批匯入（replace=True，預設）＝「這份就是我現在全部的物件」：上次有、這次沒有的，標成下架（不刪，留紀錄）。
"""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from adapters.csv import parsing as P
from adapters.storage import TenantStore
from core.listing_diff import MATERIAL_FIELDS, listing_fingerprint

LISTING_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "listing_id", "物件編號", "物件代號", "案件編號", "案號", "編號", "委託編號", "案件代號"),
    "title": ("title", "案名", "物件名稱", "標題", "名稱", "社區名稱"),
    "transaction": ("transaction", "交易", "交易類別", "租售", "出售出租", "買賣", "類別"),
    "city": ("city", "縣市"),
    "area": ("area", "district", "區域", "行政區", "鄉鎮市區", "地區"),
    "price": ("price", "總價", "售價", "開價", "價格", "租金", "月租", "委託價", "售價(萬)", "總價(萬)"),
    "property_type": ("property_type", "型態", "物件類型", "建物型態", "類型", "用途"),
    "rooms": ("rooms", "房數", "格局", "房"),
    "size_ping": ("size_ping", "坪數", "建坪", "權狀坪數", "登記坪數", "建物面積", "面積"),
    "age_years": ("age_years", "屋齡"),
    "floor": ("floor", "樓層"),
    "url": ("url", "link", "連結", "網址", "物件網址", "分享連結"),
    "photo_url": ("photo_url", "封面", "封面圖", "照片", "圖片", "主圖"),
    "listed_at": ("listed_at", "上架日", "上架日期", "刊登日", "刊登日期"),
    "online": ("online", "上架", "是否上架", "刊登狀態", "銷售狀態", "狀態"),
    "source": ("source", "來源"),
    # 全店共用：這間是哪位同仁的（對到名冊上的名字，配對通知就會找他）。只收名字，不收電話。
    "agent_name": ("agent_name", "負責同仁", "經紀人", "承辦人", "承辦經紀人", "開發同仁", "負責人"),
}
SENSITIVE_WORDS = ("地址", "門牌", "屋主", "所有權人", "業主", "電話", "手機", "身分證", "email", "e-mail",
                   "備註", "聯絡")
MATERIAL = MATERIAL_FIELDS


@dataclass
class ImportReport:
    rows_total: int = 0
    rows_ok: int = 0
    errors: list[dict] = field(default_factory=list)       # [{"line": CSV 列號, "reason": ...}]，不含原始值
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    ignored_sensitive_columns: list[str] = field(default_factory=list)
    new_ids: list[str] = field(default_factory=list)
    changed_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    unchanged_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def fingerprint(listing: Mapping[str, Any]) -> str:
    return listing_fingerprint(listing)


def normalize_row(row: Mapping[str, str], mapping: Mapping[str, str], *, default_transaction: str | None = None,
                  default_city: str | None = None, source: str = "csv") -> dict[str, Any]:
    """一列 → 正規化物件。缺 id／交易／區域／價格 → ValueError（該列不匯入，其他列照常）。"""
    get = lambda f: row.get(mapping[f], "") if f in mapping else ""
    lid = get("id").strip()
    if not lid:
        raise ValueError("缺物件編號")
    if len(lid) > 64:
        raise ValueError("物件編號超過 64 字")
    tx = P.parse_transaction(get("transaction")) or default_transaction
    if tx not in ("buy", "rent"):
        raise ValueError("交易別看不懂（要寫 出售／出租）")
    city, area = P.normalize_area(get("area"), city=get("city"), default_city=default_city)
    if not area:
        raise ValueError("缺區域")
    price = P.parse_price(get("price"), transaction=tx, header=mapping.get("price", ""))
    if price is None or price <= 0:
        raise ValueError("缺價格或價格不合理")
    url = get("url").strip()
    photo = get("photo_url").strip()
    online = P.parse_online(get("online"))
    return {
        "id": lid, "transaction": tx, "city": city, "area": area,
        "title": get("title").strip()[:80] or None,
        "price": price, "price_unit": "元/月" if tx == "rent" else "萬",
        "property_type": P.parse_property_type(get("property_type")),
        "rooms": P.parse_rooms(get("rooms")),
        "size_ping": P.parse_size_ping(get("size_ping"), mapping.get("size_ping", "")),
        "age_years": P.parse_age(get("age_years")),
        "floor": get("floor").strip()[:20] or None,
        "url": url if url.startswith(("https://", "http://")) else None,
        "photo_url": photo if photo.startswith("https://") else None,
        "listed_at": P.parse_date(get("listed_at")),
        "online": True if online is None else online,
        "source": get("source").strip()[:40] or source,
        "agent_name": _agent(get("agent_name")),
    }


def _agent(value: str) -> str | None:
    """負責同仁欄：只留名字（12 字內），像電話、email 的整格丟掉。"""
    import re
    v = (value or "").strip()[:12]
    return None if not v or re.search(r"\d{6,}|@", v) else v


class CsvListingSource:
    SNAPSHOT = ("listings", "snapshot.json")
    CHANGES = ("listings", "changes.jsonl")

    def __init__(self, store: TenantStore, now: Callable[[], datetime.datetime] = datetime.datetime.now):
        self.store = store
        self._now = now

    # ── 匯入 ──
    def import_csv(self, tenant_id: str, data: bytes | str, *, mapping: Mapping[str, str] | None = None,
                   default_transaction: str | None = None, default_city: str | None = None,
                   source: str = "csv", replace: bool = True) -> ImportReport:
        headers, rows = P.read_table(P.decode_bytes(data))
        sensitive = P.sensitive_columns(headers, SENSITIVE_WORDS)
        usable = [h for h in headers if h not in sensitive]
        if mapping and any(h in sensitive for h in mapping.values()):
            raise ValueError("不能把地址／屋主／電話這類欄位對映進物件資料")
        mp, unmapped = P.resolve_mapping(usable, LISTING_ALIASES, mapping)
        rep = ImportReport(rows_total=len(rows), mapping=mp, unmapped_columns=unmapped,
                           ignored_sensitive_columns=sensitive)
        missing = [f for f in ("id", "area", "price") if f not in mp]
        if "transaction" not in mp and not default_transaction:
            missing.append("transaction")
        if missing:
            raise ValueError("檔案缺必要欄位：" + "、".join(missing) + "（可用欄位對映指定）")

        parsed: dict[str, dict] = {}
        for line, row in rows:
            try:
                item = normalize_row(row, mp, default_transaction=default_transaction,
                                     default_city=default_city, source=source)
            except ValueError as e:
                rep.errors.append({"line": line, "reason": str(e)})
                continue
            if item["id"] in parsed:
                rep.errors.append({"line": line, "reason": "物件編號重複（同一檔出現兩次，只收第一筆）"})
                continue
            parsed[item["id"]] = item
        rep.rows_ok = len(parsed)

        ts = self._now().isoformat(timespec="seconds")
        with self.store.lock(tenant_id, "listings"):
            snap = self._snapshot(tenant_id)
            for lid, item in parsed.items():
                old = snap.get(lid)
                if old is None:
                    snap[lid] = {**item, "first_seen": ts, "updated_at": ts}
                    rep.new_ids.append(lid)
                    self._change(tenant_id, ts, lid, "new")
                elif fingerprint(old) != fingerprint(item):
                    fields = [k for k in MATERIAL if old.get(k) != item.get(k)]
                    snap[lid] = {**old, **item, "updated_at": ts}
                    snap[lid].pop("offline_reason", None)
                    rep.changed_ids.append(lid)
                    self._change(tenant_id, ts, lid, "changed", fields)
                else:
                    rep.unchanged_count += 1
            if replace:
                for lid, old in snap.items():
                    # 只下架「同一個來源」沒出現的：店內物件表同步不能把同仁在 LINE 上新增的物件當成下架
                    if lid not in parsed and old.get("online") and old.get("source", source) == source:
                        snap[lid] = {**old, "online": False, "offline_reason": "not_in_latest_import",
                                     "updated_at": ts}
                        rep.removed_ids.append(lid)
                        self._change(tenant_id, ts, lid, "removed")
            self.store.write_json(tenant_id, *self.SNAPSHOT, data=snap)
        return rep

    def save_one(self, tenant_id: str, item: Mapping[str, Any]) -> dict:
        """LINE「新增物件」一筆一筆加（不像 CSV 整批取代既有物件）。編號自動給：M001、M002…"""
        ts = self._now().isoformat(timespec="seconds")
        with self.store.lock(tenant_id, "listings"):
            snap = self._snapshot(tenant_id)
            used = [int(k[1:]) for k in snap if k.startswith("M") and k[1:].isdigit()]
            lid = f"M{max(used, default=0) + 1:03d}"
            rec = {**dict(item), "id": lid, "online": True, "first_seen": ts, "updated_at": ts}
            snap[lid] = rec
            self.store.write_json(tenant_id, *self.SNAPSHOT, data=snap)
            self._change(tenant_id, ts, lid, "new")
        return dict(rec)

    # ── ListingSource 介面 ──
    def list_all(self, tenant_id: str) -> list[dict]:
        return sorted(self._snapshot(tenant_id).values(), key=lambda x: x["id"])

    def list_active(self, tenant_id: str) -> list[dict]:
        return [x for x in self.list_all(tenant_id) if x.get("online")]

    def get(self, tenant_id: str, listing_id: str) -> dict | None:
        return self._snapshot(tenant_id).get(listing_id)

    def public_url(self, tenant_id: str, listing_id: str) -> str | None:
        """只給上架中的物件連結：下架的物件不能再推給客戶。"""
        x = self.get(tenant_id, listing_id)
        return x.get("url") if x and x.get("online") else None

    def changes_since(self, tenant_id: str, since: str | None = None) -> list[dict]:
        rows = self.store.read_jsonl(tenant_id, *self.CHANGES)
        return [r for r in rows if not since or str(r.get("ts")) > since]

    def mark_online(self, tenant_id: str, listing_id: str, online: bool) -> bool:
        ts = self._now().isoformat(timespec="seconds")
        with self.store.lock(tenant_id, "listings"):
            snap = self._snapshot(tenant_id)
            x = snap.get(listing_id)
            if x is None:
                return False
            if bool(x.get("online")) == bool(online):
                return True
            x.update({"online": bool(online), "updated_at": ts})
            if online:
                x.pop("offline_reason", None)
            else:
                x["offline_reason"] = "marked_by_tenant"
            self.store.write_json(tenant_id, *self.SNAPSHOT, data=snap)
            self._change(tenant_id, ts, listing_id, "online" if online else "offline")
        return True

    def delete_all(self, tenant_id: str) -> None:
        with self.store.lock(tenant_id, "listings"):
            for parts in (self.SNAPSHOT, self.CHANGES):
                p = self.store.path(tenant_id, *parts)
                if p.exists():
                    p.unlink()

    # ── 內部 ──
    def _snapshot(self, tenant_id: str) -> dict[str, dict]:
        return self.store.read_json(tenant_id, *self.SNAPSHOT, default={}) or {}

    def _change(self, tenant_id: str, ts: str, lid: str, kind: str, fields: list[str] | None = None) -> None:
        rec = {"ts": ts, "id": lid, "kind": kind}
        if fields:
            rec["fields"] = fields
        self.store.append_jsonl(tenant_id, *self.CHANGES, record=rec)
