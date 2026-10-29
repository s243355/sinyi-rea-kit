# -*- coding: utf-8 -*-
"""MCP 工具的實作本體（契約見 docs/mcp_tools.md；兩邊要一起改）。

這裡只是「純函式＋租戶情境」：MCP server（W2）把每個函式包成一個 tool，
租戶由連線身分決定、放進 ToolContext——工具參數裡沒有 tenant_id，Skill 指定不了別的租戶。

所有函式回傳 JSON 可序列化的 dict；出錯一律回 {"error": {"code", "message", "retryable"}}，不丟例外給 Skill。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Callable

from adapters.csv.customer_source import CsvCustomerSource, missing_fields
from adapters.csv.listing_source import CsvListingSource
from adapters.line import flex
from adapters.line.push_gate import PushGate
from adapters.storage import TenantStore

CONTRACT_VERSION = "0.2"
LISTING_FIELDS = ("id", "transaction", "city", "area", "title", "price", "price_unit", "property_type", "rooms",
                  "size_ping", "age_years", "floor", "url", "photo_url", "listed_at", "online", "source", "updated_at")
NEED_KEYS = ("transaction", "preferred_areas", "budget_min", "budget_max", "budget_tolerance_pct",
             "property_type", "rooms_min")


@dataclass
class ToolContext:
    tenant_id: str
    store: TenantStore
    listings: CsvListingSource
    customers: CsvCustomerSource
    gate_factory: Callable[[], PushGate] | None = None      # 沒設 LINE 的租戶＝None
    matcher: Callable | None = None
    bind_registry: Any = None                               # 共用通知帳號的 BindingRegistry；沒開共用帳號＝None
    now: Callable[[], datetime.datetime] = datetime.datetime.now


def _err(code: str, message: str, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def _as_of(ctx: ToolContext) -> str:
    return ctx.now().isoformat(timespec="seconds")


def _public_listing(x: dict) -> dict:
    return {k: x.get(k) for k in LISTING_FIELDS}


def _matcher(ctx: ToolContext) -> Callable:
    if ctx.matcher:
        return ctx.matcher
    from web.jobs.new_listing_push import load_matcher
    return load_matcher()


# ── list_listings ──
def list_listings(ctx: ToolContext, *, transaction: str | None = None, areas: list[str] | None = None,
                  property_type: str | None = None, price_min: float | None = None, price_max: float | None = None,
                  online_only: bool = True, changed_since: str | None = None, ids: list[str] | None = None,
                  limit: int = 50, cursor: str | None = None) -> dict:
    if transaction not in (None, "buy", "rent"):
        return _err("invalid_argument", "transaction 只能是 buy 或 rent")
    limit = max(1, min(int(limit or 50), 200))
    rows = ctx.listings.list_active(ctx.tenant_id) if online_only else ctx.listings.list_all(ctx.tenant_id)
    if changed_since:
        touched = {c["id"] for c in ctx.listings.changes_since(ctx.tenant_id, changed_since)}
        rows = [x for x in rows if x["id"] in touched]
    if ids:
        want = set(ids)
        rows = [x for x in rows if x["id"] in want]
    if transaction:
        rows = [x for x in rows if x.get("transaction") == transaction]
    if areas:
        want_a = {a.replace("臺", "台") for a in areas}
        rows = [x for x in rows if x.get("area") in want_a or x.get("city") in want_a]
    if property_type:
        rows = [x for x in rows if x.get("property_type") == property_type]
    if price_min is not None:
        rows = [x for x in rows if x.get("price") is not None and x["price"] >= price_min]
    if price_max is not None:
        rows = [x for x in rows if x.get("price") is not None and x["price"] <= price_max]
    start = int(cursor or 0)
    page = rows[start:start + limit]
    nxt = str(start + limit) if start + limit < len(rows) else None
    return {"listings": [_public_listing(x) for x in page], "total": len(rows), "next_cursor": nxt,
            "as_of": _as_of(ctx), "source": "csv", "contract": CONTRACT_VERSION}


# ── get_need_card ──
def get_need_card(ctx: ToolContext, *, customer_id: str) -> dict:
    card = ctx.customers.get_need_card(ctx.tenant_id, customer_id)
    if card is None:
        return _err("not_found", f"找不到客戶 {customer_id}")
    need = card["need_card"]
    return {"customer_id": card["customer_id"], "label": card["label"], "status": card["status"],
            "need_card": {k: need.get(k) for k in NEED_KEYS}, "missing": card["missing"],
            "can_push": card["can_push"], "version": need.get("version"), "updated_at": need.get("updated_at"),
            "as_of": _as_of(ctx), "contract": CONTRACT_VERSION}


# ── match ──
def match(ctx: ToolContext, *, customer_id: str | None = None, need_card: dict | None = None,
          listing_ids: list[str] | None = None, limit: int = 10) -> dict:
    if bool(customer_id) == bool(need_card):
        return _err("invalid_argument", "customer_id 與 need_card 要給剛好一個")
    version = None
    if customer_id:
        card = ctx.customers.get_need_card(ctx.tenant_id, customer_id)
        if card is None:
            return _err("not_found", f"找不到客戶 {customer_id}")
        need, version = card["need_card"], card["need_card"].get("version")
    else:
        need = dict(need_card or {})
    # 沒填的欄位整個拿掉（交給配對引擎的預設），不要傳 None 進去
    need = {k: need[k] for k in NEED_KEYS if need.get(k) is not None}
    lack = missing_fields(need)
    if lack:
        return _err("invalid_argument", "客需卡缺：" + "、".join(lack))
    rows = ctx.listings.list_active(ctx.tenant_id)
    if listing_ids:
        want = set(listing_ids)
        rows = [x for x in rows if x["id"] in want]
    by_key = {x["id"].casefold(): x for x in rows}
    try:
        result = _matcher(ctx)(need, rows)
    except RuntimeError as e:
        return _err("not_ready", str(e), retryable=True)
    except ValueError as e:
        return _err("invalid_argument", str(e))
    limit = max(1, min(int(limit or 10), 50))
    out: dict[str, Any] = {}
    for bucket in ("priority", "review", "rejected"):
        items = []
        for r in result.get(bucket, [])[:limit]:
            x = by_key.get(str(r.get("listing_id", "")).casefold())
            items.append({**r, "listing_id": x["id"] if x else r.get("listing_id"),
                          "listing": _public_listing(x) if x else None})
        out[bucket] = items
    return {**out, "need_card_version": version, "as_of": _as_of(ctx), "contract": CONTRACT_VERSION}


# ── queue_for_review ──
def queue_for_review(ctx: ToolContext, *, customer_id: str, listing_ids: list[str], note: str = "") -> dict:
    if not ctx.gate_factory:
        return _err("not_ready", "這個帳號還沒接上 LINE 官方帳號，請先完成後台的 LINE 設定精靈")
    if not listing_ids or len(listing_ids) > 3:
        return _err("invalid_argument", "listing_ids 要 1–3 間（一則訊息最多放 3 間，避免洗版）")
    cust = ctx.customers.get(ctx.tenant_id, customer_id)
    if cust is None:
        return _err("not_found", f"找不到客戶 {customer_id}")
    if cust.get("status") != "active":
        return _err("invalid_argument", f"客戶狀態是 {cust.get('status')}，不推")
    if not cust.get("line_target"):
        return _err("not_ready", "這位客戶還沒綁 LINE 群組或好友")
    picks = []
    for lid in listing_ids:
        x = ctx.listings.get(ctx.tenant_id, lid)
        if not x or not x.get("online"):
            return _err("invalid_argument", f"物件 {lid} 不存在或已下架")
        if not x.get("url"):
            return _err("invalid_argument", f"物件 {lid} 沒有可點的連結，不能推給客戶")
        picks.append(x)
    gate = ctx.gate_factory()
    label = cust.get("label") or customer_id
    summary = f"{label}：{len(picks)} 間（{picks[0].get('area') or ''} {flex.format_price(picks[0])}）"
    if note:
        summary += f"｜AI 說明：{str(note)[:200]}"
    res = gate.submit(to=cust["line_target"], messages=[flex.listing_message(picks)], level="match",
                      kind="skill_queue", recipient_label=label, summary=summary, members=cust.get("members"),
                      dedup_key=customer_id + "|" + ",".join(x["id"] for x in picks))
    q = gate.budget.summary()
    return {"state": res.state, "item_id": res.item_id, "reason": res.reason,
            "quota": {"used": q["used"], "plan_cap": q["plan_cap"], "remaining": q["remaining"]},
            "as_of": _as_of(ctx), "contract": CONTRACT_VERSION}


# ── patrol_posts ──
def patrol_posts(ctx: ToolContext, *, platforms: list[str] | None = None, keywords: list[str] | None = None,
                 areas: list[str] | None = None, since: str | None = None, limit: int = 50,
                 cursor: str | None = None) -> dict:
    """24h 海巡。W1 只有契約：採集要等「本機小幫手」（租戶自己的瀏覽器、只看公開頁），之後才回真資料。"""
    return _err("not_ready", "海巡採集尚未開放（需要本機小幫手）；契約已定，Skill 可先用合成貼文開發", retryable=True)


# ── get_push_quota（附加）──
def get_push_quota(ctx: ToolContext) -> dict:
    if not ctx.gate_factory:
        return _err("not_ready", "這個帳號還沒接上 LINE 官方帳號")
    return {**ctx.gate_factory().budget.summary(), "as_of": _as_of(ctx), "contract": CONTRACT_VERSION}


# ── get_line_bind_code（v0.2）──
def get_line_bind_code(ctx: ToolContext) -> dict:
    """給仲介一組 6 碼綁定碼：加店內官方帳號好友後傳「綁定 123456」，之後新案通知就送到他的 LINE。"""
    if ctx.bind_registry is None:
        return _err("not_ready", "共用通知帳號還沒開通")
    code = ctx.bind_registry.issue_code(ctx.tenant_id)
    bound = ctx.bind_registry.user_of(ctx.tenant_id) is not None
    return {"code": code, "expires_in_minutes": 15, "already_bound": bound,
            "how_to": "加官方帳號好友後，在一對一聊天傳：綁定 " + code,
            "as_of": _as_of(ctx), "contract": CONTRACT_VERSION}


TOOLS = {f.__name__: f for f in (list_listings, get_need_card, match, queue_for_review, patrol_posts,
                                  get_push_quota, get_line_bind_code)}
