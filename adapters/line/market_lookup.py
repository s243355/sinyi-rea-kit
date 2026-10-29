# -*- coding: utf-8 -*-
"""LINE 指令「行情 ＿＿」：社區、建案、行政區的實價登錄行情。

先查 ToEstate（社區層級、已扣車位）；連不上、被限流或額度用完，行政區改用內政部開放資料（只有行政區概況）。
只回概況，不估價。對話不留紀錄（只有快取與用量計數）。
"""
from __future__ import annotations

import os
from typing import Callable

from adapters.line.customer_intake import message
from adapters.toestate import ToEstateClient, ToEstateError, area_text, community_text
from core import market_query as Q

HELP = ("查行情：打「行情 社區名」或「行情 行政區」\n例：行情 語山簷｜行情 南屯區｜行情 台中市西屯區\n"
        "資料是實價登錄（約晚 1～3 個月），只供參考，不是估價。")
DEFAULT_CITY_ENV = "REA_DEFAULT_CITY"


def moi_area_text(county: str, town: str, *, fetch=None, parse=None, summary=None) -> str:
    """內政部開放資料的行政區概況（ToEstate 不能用時的備援）。"""
    from adapters import lvr_opendata as L
    fetch, parse, summary = fetch or L.fetch, parse or L.parse, summary or L.summary
    season, body = fetch(county)
    s = summary(parse(body), town, recent=5)
    if not s["count"]:
        return f"{county}{town}：內政部 {season} 季沒有可用的成交資料。"
    lines = [f"📊 {county}{town}（內政部 {season} 季資料）",
             f"{s['count']} 筆，單價中位 {s['median_unit_wan_per_ping']} 萬/坪（未扣車位）", "最近成交："]
    for r in s["recent"]:
        lines.append(f"・{r['date']} {r['type'][:6]} {r['ping']}坪 {r['total_wan']:,}萬 {r['unit_wan_per_ping']}萬/坪")
    lines.append("資料：內政部實價登錄開放資料（政府資料開放授權條款）")
    return "\n".join(lines)


def lookup(query: str, *, client: ToEstateClient | None, default_county: str | None,
           moi: Callable[[str, str], str] | None = moi_area_text) -> tuple[str, tuple[str, ...]]:
    """回 (文字, 快速按鈕)。"""
    kind, a, b = Q.parse(query, default_county)
    why = ""
    if client is not None and client.available:
        try:
            if kind == "area":
                if not b:
                    return (f"要查哪一區？例：行情 {a}{_example(a)}", ())
                return area_text(client.area_stats(a, b), county=a, town=b), ()
            return _by_name(client, a)
        except ToEstateError as e:
            why = str(e)
    else:
        why = "ToEstate 沒有接上。" if client is None else "ToEstate 今天暫停查詢（額度或限流）。"
    if kind == "area" and b and moi:
        try:
            return (f"（{why} 改用內政部資料）\n" if why else "") + moi(a, b), ()
        except Exception:
            return f"{why} 內政部資料也暫時抓不到，請稍後再試。", ()
    if kind == "area":
        return f"要查哪一區？例：行情 {a}{_example(a)}", ()
    return (f"{why} 社區行情要用 ToEstate；現在可以先查行政區，例：行情 {default_county or '台中市'}南屯區。"
            if why else "查不到。"), ()


def _example(county: str) -> str:
    from core.tw_districts import DISTRICTS
    return (DISTRICTS.get(county) or ("",))[0]


def _by_name(client: ToEstateClient, query: str) -> tuple[str, tuple[str, ...]]:
    items = client.search(query, limit=5)
    if not items:
        return (f"ToEstate 找不到「{query}」。可以試試社區正式名稱、路名＋門牌，或打「行情 行政區」。", ())
    top = items[0]
    others = tuple(f"行情 {x['title']}" for x in items[1:4] if x.get("kind") in ("community", "project")
                   and x.get("title") and len(x["title"]) <= 15)
    k = top.get("kind")
    if k == "community" and top.get("community_id"):
        return community_text(client.community(top["community_id"])), others
    if k == "area" and top.get("county") and top.get("town"):
        return area_text(client.area_stats(top["county"], top["town"]), county=top["county"], town=top["town"]), others
    if k == "nodata":
        return (f"「{query}」這個門牌在實價登錄沒有成交紀錄；附近的地址不是同一棟，請不要直接拿來比。"
                "可以改查社區名或行政區。", others)
    sub = top.get("sub") or ""
    url = top.get("url") or ""
    return (f"🔎 {top.get('title') or query}\n{sub}\n{url}\n（建案、生活圈的細節請開連結看）", others)


def make_market_handler(*, client_factory: Callable[[], ToEstateClient | None], env=os.environ,
                        moi: Callable[[str, str], str] | None = moi_area_text):
    def handler(ctx):
        if not ctx.is_owner_dm or not ctx.text:
            return
        query = Q.split_command(ctx.text)
        if query is None:
            return
        if not query:
            ctx.reply([message(HELP)])
            return
        default = (env.get(DEFAULT_CITY_ENV) or "台中市").replace("臺", "台")
        text, quick = lookup(query, client=client_factory(), default_county=default, moi=moi)
        ctx.reply([message(text[:4800], quick)])
    return handler
