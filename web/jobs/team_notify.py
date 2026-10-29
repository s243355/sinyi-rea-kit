# -*- coding: utf-8 -*-
"""全店共用：店內物件表同步進來的新物件 → 配全店買方 → 每位買方的負責同仁收一則通知。

跟 LINE 上「新增物件」不同：表是店長（管理員）同步的，所以連「物件負責人自己的買方」也要通知
（他沒有在 LINE 上按確認儲存，不會看到配對卡）。

一位同仁一輪只收一則（多間物件合在一起），走推播閘、吃推播額度。訊息只有物件編號、區域、型態、價格與買方暱稱。
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping

from adapters.line import flex
from web.jobs.match_cards import pairs

MAX_LINES = 8                       # 一則通知最多列幾組配對，其餘用「還有 N 組」帶過


def notify_new_listings(listings: Iterable[Mapping], buyers: Iterable[Mapping],
                        notify: Callable[[str, str], object]) -> dict[str, int]:
    """回 {租戶: 通知了幾組}。notify(租戶, 文字)。"""
    recs = [dict(x) for x in listings if x.get("online", True)]
    if not recs:
        return {}
    by_owner: dict[str, list[dict]] = {}
    for c in buyers:
        if c.get("owner"):
            by_owner.setdefault(c["owner"], []).append({k: v for k, v in c.items() if k != "owner_name"})
    sent: dict[str, int] = {}
    for owner, group in sorted(by_owner.items()):
        found = pairs(recs, group)
        if not found:
            continue
        lines = [f"・{x['id']}｜{x.get('area') or ''}｜{x.get('property_type') or ''}｜{flex.format_price(x)} → {label}"
                 for x, label, _, _ in found[:MAX_LINES]]
        more = f"\n…還有 {len(found) - MAX_LINES} 組" if len(found) > MAX_LINES else ""
        text = ("🔔 店內物件表今天新增的物件，配到你的買方：\n" + "\n".join(lines) + more
                + "\n打物件編號看詳細；別人負責的物件，記得先跟負責同仁確認再約看。")
        try:
            notify(owner, text)
            sent[owner] = len(found)
        except Exception:
            pass
    return sent


def after_sheet_sync(app, new_ids: Iterable[str]) -> dict[str, int]:
    """排程或精靈同步完物件表之後呼叫。不是全店共用、或沒有新物件就什麼都不做。"""
    team_listings = getattr(app, "team_listings", None)
    team_customers = getattr(app, "team_customers", None)
    ids = set(new_ids or ())
    if not ids or team_listings is None or team_customers is None or not team_listings.shared:
        return {}
    from adapters.team import TEAM_TENANT
    recs = [x for x in team_listings.list_all(TEAM_TENANT) if x["id"] in ids]
    buyers = team_customers.list_customers(TEAM_TENANT)

    def push(tenant: str, text: str):
        return app.notify(tenant, [{"type": "text", "text": text[:4800]}], kind="sheet_match", keep_payload=False)

    return notify_new_listings(recs, buyers, push)
