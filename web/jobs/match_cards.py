# -*- coding: utf-8 -*-
"""配對卡：物件 × 這位仲介自己的買方 → 「這間適合哪位客人」，一組一張卡（給仲介本人看）。

只用同一個租戶的物件與客人（呼叫端從同一個租戶資料夾拿），不同仲介之間互不可見。
「可優先」的直接列；缺資料的（例如物件沒填車位、客人要車位）也列，但標「要確認：車位」，不要漏掉。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from adapters.csv.customer_source import missing_fields
from web.jobs.new_listing_push import load_matcher, need_of

MISSING_ZH = {"parking": "車位", "elevator": "電梯", "rooms": "房數", "size_ping": "坪數", "age_years": "屋齡",
              "property_type": "型態", "price": "價格", "area": "區域", "transaction": "買賣／出租"}


def _n(v: Any) -> str:
    return f"{float(v):g}"


def need_line(need: Mapping[str, Any]) -> str:
    """客需卡 → 一行摘要：買｜台中市西屯區、台中市南屯區｜800–1500萬｜公寓｜2房以上。"""
    buy = need.get("transaction") == "buy"
    parts = ["買" if buy else "租"]
    areas = list(need.get("preferred_areas") or [])
    parts.append("、".join(areas[:3]) + (f" 等{len(areas)}區" if len(areas) > 3 else ""))
    unit = "萬" if buy else "元/月"
    if need.get("budget_max") is not None:
        lo = need.get("budget_min")
        parts.append((f"{_n(lo)}–" if lo else "") + f"{_n(need['budget_max'])}{unit}")
    if need.get("property_type"):
        parts.append(str(need["property_type"]))
    if need.get("rooms_min"):
        parts.append(f"{_n(need['rooms_min'])}房以上")
    for key, zh in (("parking", "車位"), ("elevator", "電梯")):
        if need.get(key) == "required":
            parts.append(f"要{zh}")
    return "｜".join(p for p in parts if p)


def pairs(listings: Iterable[Mapping[str, Any]], customers: Iterable[Mapping[str, Any]], *,
          matcher: Callable | None = None) -> list[tuple[dict, str, str, str | None]]:
    """回 [(物件, 客人稱呼, 需求摘要, 提醒或 None)]：可優先的在前（分數高的先），要確認的在後。

    物件沒有網址不算缺資料（仲介自己的物件常常還沒刊登）；分享時就只送物件事實。"""
    match = matcher or load_matcher()
    pool = [dict(x) for x in listings if x.get("online", True)]
    if not pool:
        return []
    by_id = {str(x["id"]): x for x in pool}
    ranked: list[tuple[int, int, str, tuple]] = []
    for cust in customers:
        need = need_of(cust)
        if missing_fields(need) or cust.get("status", "active") != "active":
            continue
        try:
            result = match(need, pool)
        except ValueError:
            continue
        label = cust.get("label") or cust["customer_id"]
        if cust.get("owner_name"):                      # 全店共用：同事的客人標出是誰的，配到要找他
            label += f"（{cust['owner_name']}的客人）"
        line = need_line(need)
        for bucket in ("priority", "review"):
            for r in result.get(bucket, []):
                x = by_id.get(str(r.get("listing_id", "")))
                if x is None:
                    continue
                gaps = [MISSING_ZH.get(m, m) for m in r.get("missing", []) if m != "url"]
                over = _over_by(x, need)          # 直接算（審查：物件沒網址時理由欄沒有這一條，卡片就不標）
                notes = ([f"略超預算 {over:g}{'萬' if need.get('transaction') == 'buy' else '元'}"] if over else []) + \
                        (["要確認：" + "、".join(gaps)] if gaps else [])
                note = "；".join(notes) or None
                ranked.append((1 if gaps else 0, -int(r.get("score", 0)), label, (x, label, line, note)))
    ranked.sort(key=lambda t: t[:3])
    return [t[3] for t in ranked]


def _over_by(listing: dict, need: dict) -> float:
    try:
        price, cap = float(listing.get("price")), float(need.get("budget_max"))
    except (TypeError, ValueError):
        return 0
    return price - cap if price > cap else 0
