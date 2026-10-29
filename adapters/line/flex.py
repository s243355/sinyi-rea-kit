# -*- coding: utf-8 -*-
"""LINE Flex 卡片：客戶面物件卡、審核卡。全部回純 dict，可離線 json.dumps 驗證。

客戶面物件卡的規矩：
  - 第一眼是房子（有圖放封面）；沒圖也要能推，換成一塊有設計感的色底，不讓卡片看起來壞掉
  - 價格大字；類型／房數／坪數／屋齡排一列
  - 整張卡可點＋底部一顆按鈕，客戶怎麼點都對
  - 絕不出現內部物件編號、屋主、地址門牌——客戶看不懂、也不該看到

LINE 的硬性限制（違反就整包 400 打回）：
  - 封面圖網址必須 https；按鈕／連結只收 http(s)
  - carousel 最多 12 張，而且每張 bubble 的 size 必須一致
  - altText 最多 400 字；postback data 最多 300 字
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Mapping
from core import brand

INK1 = "#191C22"
INK2 = "#8A93A3"
LISTING = {"bar": brand.GREEN, "tint": brand.TINT, "ink": brand.INK}
REVIEW = {"bar": "#534AB7", "tint": "#EEEDFE", "ink": "#26215C"}

MAX_ALT = 400
MAX_LABEL = 20
MAX_URI = 1000                 # LINE 連結按鈕上限；超過整則回覆會被拒（仲介什麼都收不到）
MAX_CAROUSEL = 12


def _txt(text: Any, size: str = "sm", color: str = INK1, weight: str | None = None,
         align: str | None = None, wrap: bool = True, flex: int | None = None) -> dict:
    d: dict[str, Any] = {"type": "text", "text": str(text) or " ", "size": size, "color": color, "wrap": wrap}
    if weight:
        d["weight"] = weight
    if align:
        d["align"] = align
    if flex is not None:
        d["flex"] = flex
    return d


def _is_web(uri: Any) -> bool:
    u = urllib.parse.urlparse(str(uri or ""))
    return u.scheme in ("http", "https") and bool(u.netloc)


def _is_https(uri: Any) -> bool:
    u = urllib.parse.urlparse(str(uri or ""))
    return u.scheme == "https" and bool(u.netloc)


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fmt(n: float) -> str:
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.1f}"


def format_price(listing: Mapping[str, Any]) -> str:
    """售：萬元；租：元／月（轉接器正規化時就定好單位，這裡只負責顯示）。"""
    p = _num(listing.get("price"))
    if p is None:
        return "價格洽詢"
    if listing.get("transaction") == "rent":
        return f"{_fmt(p)} 元/月"
    return f"{_fmt(p)} 萬"


def listing_specs(listing: Mapping[str, Any]) -> list[str]:
    out = []
    if listing.get("property_type"):
        out.append(str(listing["property_type"]))
    rooms = _num(listing.get("rooms"))
    if rooms is not None and rooms > 0:
        out.append(f"{rooms:g} 房")
    size = _num(listing.get("size_ping"))
    if size is not None:
        out.append(f"{_fmt(size)} 坪")
    age = _num(listing.get("age_years"))
    if age is not None:
        out.append("新成屋" if age < 1 else f"屋齡 {age:g} 年")
    return out


def unit_price(listing: Mapping[str, Any]) -> str | None:
    """售：每坪約 X 萬（總價 ÷ 坪數）；租或缺坪數就不顯示。"""
    price, size = _num(listing.get("price")), _num(listing.get("size_ping"))
    if listing.get("transaction") == "rent" or not price or not size or size <= 0:
        return None
    return f"每坪約 {price / size:,.1f} 萬"


def place_text(listing: Mapping[str, Any]) -> str:
    """區域・路段（只到路段，不出現門牌）。區域已含縣市就不重複。"""
    city, area = str(listing.get("city") or ""), str(listing.get("area") or "")
    place = area if city and area.startswith(city) else "".join(x for x in (city, area) if x)
    return "・".join(x for x in (place, str(listing.get("road") or "")) if x)


def share_uri(listing: Mapping[str, Any], *, signature: str | None = None) -> str:
    """LINE 的分享網址：仲介點下去 → LINE 跳出「傳送給…」→ 他自己挑客戶送出。
    訊息是仲介本人發的（客戶信任、也不用審核），內容只有物件事實＋連結＋仲介自己的署名，不含內部編號。"""
    place, title = place_text(listing) or str(listing.get("area") or ""), str(listing.get("title") or "")
    price = " ".join(x for x in (format_price(listing), unit_price(listing) and f"（{unit_price(listing)}）") if x)
    specs = " ｜ ".join(listing_specs(listing))
    url, sig = str(listing.get("url") or ""), str(signature or "")
    # LINE 的連結上限 1000 字（中文編碼後一字約 9 字元）：太長就依序拿掉規格、標題，再縮署名，網址和價格一定留
    for head, spec, s in ((("・".join(x for x in (place, title) if x)), specs, sig), (place or title, specs, sig),
                          (place or title, "", sig), (place, "", sig[:12]), (place[:10], "", "")):
        uri = "https://line.me/R/msg/text/?" + urllib.parse.quote(
            "\n".join(x for x in (head or "物件資訊", price, spec, url, s) if x))
        if len(uri) <= MAX_URI:
            return uri
    return "https://line.me/R/msg/text/?" + urllib.parse.quote("\n".join(x for x in ("物件資訊", price) if x))


def match_bubble(listing: Mapping[str, Any], *, customer: str, need_line: str, note: str | None = None,
                 signature: str | None = None) -> dict:
    """給仲介本人的配對卡：一張＝一位客人 × 一間物件（參考設計 docs/協作/…審核推送卡參考設計）。
    上面標「推給誰」與他的需求摘要；按鈕：看物件（有連結才有）＋📤 分享給客戶（仲介自己的 LINE 送出）。
    客戶收到的分享文字不含「推給」、需求、內部編號。"""
    c = LISTING
    uri = listing.get("url") if _is_web(listing.get("url")) else None
    tag = {"type": "box", "layout": "vertical", "backgroundColor": c["tint"], "cornerRadius": "6px", "paddingAll": "8px",
           "contents": [_txt(f"推給：{customer}", size="sm", color=c["ink"], weight="bold"),
                        _txt(f"需求：{need_line}", size="xxs", color=c["ink"])]}
    body: list[dict] = [tag]
    if note:
        body.append(_txt(f"⚠ {note}", size="xxs", color="#A3480B"))
    place = place_text(listing)
    if place:
        body.append(_txt(place, size="xs", color=c["ink"], weight="bold"))
    body.append(_txt(listing.get("title") or listing.get("property_type") or "物件", weight="bold"))
    price_row = [_txt(format_price(listing), size="xl", color=c["bar"], weight="bold", wrap=False, flex=0)]
    if unit_price(listing):
        price_row.append(_txt(unit_price(listing), size="xs", color=INK2, align="end"))
    body.append({"type": "box", "layout": "baseline", "spacing": "md", "contents": price_row})
    specs = listing_specs(listing)
    if specs:
        body.append(_txt(" ｜ ".join(specs), size="xs", color=INK2))
    buttons = []
    if uri:
        buttons.append({"type": "button", "style": "primary", "height": "sm", "color": c["bar"],
                        "action": {"type": "uri", "label": "看物件・約帶看", "uri": uri}})
    buttons.append({"type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "uri", "label": "📤 分享給客戶", "uri": share_uri(listing, signature=signature)}})
    bub: dict[str, Any] = {
        "type": "bubble", "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "12px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "10px", "contents": buttons},
    }
    if uri:
        bub["action"] = {"type": "uri", "label": "看物件", "uri": uri}
    pic = listing.get("photo_url") if listing.get("own") else None      # 別家刊登的物件不帶照片（著作權）
    if _is_https(pic):
        bub["hero"] = {"type": "image", "url": pic, "size": "full", "aspectRatio": "20:13", "aspectMode": "cover"}
    return bub


def match_messages(items: list[tuple[Mapping[str, Any], str, str, str | None]], *, intro: str,
                   signature: str | None = None) -> list[dict]:
    """[(物件, 客人稱呼, 需求摘要, 提醒或 None)] → 每 12 張一則 carousel，最多 5 則。"""
    out = []
    for start in range(0, min(len(items), MAX_CAROUSEL * 5), MAX_CAROUSEL):
        chunk = items[start:start + MAX_CAROUSEL]
        bubbles = [match_bubble(x, customer=who, need_line=line, note=note, signature=signature)
                   for x, who, line, note in chunk]
        alt = f"{intro}：{len(items)} 組配對" + (f"（續 {start // MAX_CAROUSEL + 1}）" if start else "")
        out.append(flex_message(alt, carousel(bubbles)))
    return out


def listing_bubble(listing: Mapping[str, Any], *, cta: str = "我要看這間",
                   for_customers: list[str] | None = None, share: bool = False) -> dict:
    """一間物件一張卡。listing＝轉接器正規化後的物件 dict；必須有可點的 url。
    給客戶看的卡：share=False、不帶 for_customers。
    給仲介本人看的卡（共用通知帳號）：for_customers 列出適合的客戶稱呼，share=True 多一顆「分享給客戶」。"""
    uri = listing.get("url")
    if not _is_web(uri):
        raise ValueError("物件沒有可點的連結，不能做成客戶卡（請先補 url）")
    c = LISTING
    city, area = str(listing.get("city") or ""), str(listing.get("area") or "")
    place = area if city and area.startswith(city) else " ".join(x for x in (city, area) if x)   # 區域已含縣市就不重複
    body: list[dict] = []
    if place:
        body.append(_txt(place, size="xs", color=c["ink"], weight="bold"))
    body.append(_txt(listing.get("title") or listing.get("property_type") or "物件", weight="bold"))
    body.append({"type": "box", "layout": "baseline", "contents": [
        _txt(format_price(listing), size="xl", color=c["bar"], weight="bold", wrap=False)]})
    specs = listing_specs(listing)
    if specs:
        body.append(_txt(" ｜ ".join(specs), size="xs", color=INK2))
    if for_customers:
        names = "、".join(str(x) for x in for_customers[:5]) + (f" 等 {len(for_customers)} 位" if len(for_customers) > 5 else "")
        body.append({"type": "box", "layout": "vertical", "backgroundColor": c["tint"], "cornerRadius": "6px",
                     "paddingAll": "6px", "margin": "sm",
                     "contents": [_txt("適合：" + names, size="xs", color=c["ink"])]})
    buttons = [{"type": "button", "style": "primary", "height": "sm", "color": c["bar"],
                "action": {"type": "uri", "label": cta[:MAX_LABEL], "uri": uri}}]
    if share:
        buttons.append({"type": "button", "style": "secondary", "height": "sm",
                        "action": {"type": "uri", "label": "📤 分享給客戶", "uri": share_uri(listing)}})
    bub: dict[str, Any] = {
        "type": "bubble", "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "12px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "10px", "contents": buttons},
        "action": {"type": "uri", "label": "看物件", "uri": uri},
    }
    pic = listing.get("photo_url")
    if _is_https(pic):
        bub["hero"] = {"type": "image", "url": pic, "size": "full", "aspectRatio": "20:13",
                       "aspectMode": "cover", "action": {"type": "uri", "label": "看物件", "uri": uri}}
    else:
        bub["hero"] = {
            "type": "box", "layout": "vertical", "backgroundColor": c["tint"], "paddingAll": "18px",
            "spacing": "xs", "justifyContent": "center", "height": "110px",
            "action": {"type": "uri", "label": "看物件", "uri": uri},
            "contents": [_txt(f"🏠 {listing.get('property_type') or '物件'}", size="lg", color=c["ink"],
                              weight="bold", align="center"),
                         _txt("這間還沒放照片　點我看詳細", size="xxs", color=c["ink"], align="center")]}
    return bub


def carousel(bubbles: list[dict]) -> dict | None:
    bs = [b for b in bubbles if b][:MAX_CAROUSEL]
    if not bs:
        return None
    if len(bs) == 1:
        return bs[0]
    size = bs[0].get("size", "mega")
    return {"type": "carousel", "contents": [{**b, "size": size} for b in bs]}


def flex_message(alt_text: str, contents: dict) -> dict:
    alt = (alt_text or "通知").strip()
    if len(alt) > MAX_ALT:
        alt = alt[:MAX_ALT - 1] + "…"
    return {"type": "flex", "altText": alt, "contents": contents}


def listing_message(listings: list[Mapping[str, Any]], *, intro: str = "幫您留意到符合需求的物件") -> dict:
    """物件推播訊息：1 間＝單張卡，多間＝可左右滑的 carousel（最多 12 間）。"""
    bubbles = [listing_bubble(x) for x in listings[:MAX_CAROUSEL]]
    contents = carousel(bubbles)
    if contents is None:
        raise ValueError("沒有可推的物件")
    first = listings[0]
    alt = f"{intro}：{first.get('area') or ''} {first.get('title') or ''} {format_price(first)}"
    if len(listings) > 1:
        alt += f" 等 {len(listings)} 間"
    return flex_message(alt, contents)


def agent_digest_messages(items: list[tuple[Mapping[str, Any], list[str]]], *,
                          intro: str = "今天的新案配對") -> list[dict]:
    """給仲介本人的新案摘要：每間物件一張卡（標出適合哪些客戶＋分享鈕），12 張一則，最多 5 則。
    items＝[(物件, [客戶稱呼…])]，呼叫端已排好先後。"""
    if not items:
        return []
    out = []
    for start in range(0, min(len(items), MAX_CAROUSEL * 5), MAX_CAROUSEL):
        chunk = items[start:start + MAX_CAROUSEL]
        bubbles = [listing_bubble(x, cta="看物件", for_customers=names, share=True) for x, names in chunk]
        n_cust = len({n for _, names in items for n in names})
        alt = f"{intro}：{len(items)} 間新物件適合 {n_cust} 位客戶"
        if start:
            alt += f"（續 {start // MAX_CAROUSEL + 1}）"
        out.append(flex_message(alt, carousel(bubbles)))
    return out


def links_message(title: str, notes: list[str], links: list[tuple[str, str]]) -> dict:
    """一張卡、一排連結按鈕（找房連結用）。按鈕只收 http(s)，文字 20 字內。"""
    c = LISTING
    buttons = [{"type": "button", "style": "primary" if i == 0 else "secondary", "height": "sm",
                **({"color": c["bar"]} if i == 0 else {}),
                "action": {"type": "uri", "label": label[:MAX_LABEL], "uri": uri}}
               for i, (label, uri) in enumerate(links) if _is_web(uri)]
    bubble = {"type": "bubble", "size": "kilo",
              "header": {"type": "box", "layout": "vertical", "backgroundColor": c["bar"], "paddingAll": "12px",
                         "contents": [_txt(title, size="md", color="#FFFFFF", weight="bold")]},
              "body": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "12px",
                       "contents": [_txt(n, size="xs", color=c["ink"]) for n in notes if n] or [_txt(" ")]},
              "footer": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "10px",
                         "contents": buttons}}
    return flex_message(title, bubble)


def postback_data(action: str, item_id: str) -> str:
    data = urllib.parse.urlencode({"gate": action, "id": item_id})
    if len(data) > 300:
        raise ValueError("postback data 超過 300 字")
    return data


def parse_postback(data: str) -> dict[str, str]:
    q = urllib.parse.parse_qs(str(data or ""), keep_blank_values=False)
    return {k: v[0] for k, v in q.items() if v}


def review_bubble(*, item_id: str, recipient_label: str, summary: str, level_desc: str,
                  allow_always: bool = False) -> dict:
    """審核卡（送給租戶本人）：送出／不送，固定範本才多一顆「以後這類都可以」。
    按鈕是 postback，webhook 收到後只認租戶本人（或指定審核人）按的。"""
    c = REVIEW
    buttons = [
        {"type": "button", "style": "primary", "height": "sm", "color": c["bar"],
         "action": {"type": "postback", "label": "送出", "data": postback_data("approve", item_id),
                    "displayText": "送出"}},
        {"type": "button", "style": "secondary", "height": "sm",
         "action": {"type": "postback", "label": "不送", "data": postback_data("reject", item_id),
                    "displayText": "不送"}},
    ]
    if allow_always:
        buttons.insert(1, {"type": "button", "style": "secondary", "height": "sm",
                           "action": {"type": "postback", "label": "以後這類都可以",
                                      "data": postback_data("always", item_id),
                                      "displayText": "以後這類都可以"}})
    return {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": c["bar"], "paddingAll": "12px",
                   "contents": [_txt("📝 要送嗎？", size="md", color="#FFFFFF", weight="bold"),
                                _txt(level_desc, size="xxs", color="#FFFFFF")]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                _txt("收件", size="sm", color=INK2, flex=0),
                _txt(recipient_label or "（未命名）", size="sm", align="end")]},
            {"type": "box", "layout": "vertical", "backgroundColor": c["tint"], "cornerRadius": "8px",
             "paddingAll": "9px", "contents": [_txt(summary[:300], size="sm", color=c["ink"])]},
            _txt(f"編號 {item_id}", size="xxs", color=INK2)]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "10px",
                   "contents": buttons},
    }


def validate_message(msg: Mapping[str, Any]) -> list[str]:
    """送出前自檢，避開最常見的 400。回空 list＝沒問題。"""
    errs: list[str] = []
    import json as _json
    for uri in __import__("re").findall(r'"uri":\s*"([^"]*)"', _json.dumps(msg, ensure_ascii=False)):
        if len(uri) > MAX_URI:
            errs.append(f"連結超過 {MAX_URI} 字")
    t = msg.get("type")
    if t == "text":
        if not str(msg.get("text") or "").strip():
            errs.append("文字訊息是空的")
        elif len(str(msg["text"])) > 5000:
            errs.append("文字訊息超過 5000 字")
        return errs
    if t != "flex":
        return errs
    if not msg.get("altText") or len(msg["altText"]) > MAX_ALT:
        errs.append("altText 空白或超過 400 字")
    contents = msg.get("contents") or {}
    bubbles = contents.get("contents", []) if contents.get("type") == "carousel" else [contents]
    if len(bubbles) > MAX_CAROUSEL:
        errs.append("carousel 超過 12 張")
    if len({b.get("size", "mega") for b in bubbles}) > 1:
        errs.append("carousel 裡 bubble 尺寸不一致")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            act = node.get("action")
            if isinstance(act, dict):
                if act.get("type") == "uri" and not _is_web(act.get("uri")):
                    errs.append(f"連結不是 http(s)：{str(act.get('uri'))[:40]}")
                if len(str(act.get("label") or "")) > MAX_LABEL:
                    errs.append("按鈕文字超過 20 字")
            if node.get("type") == "image" and not _is_https(node.get("url")):
                errs.append("圖片網址必須 https")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(contents)
    return errs


def action_card(title: str, lines: list[str], buttons: list, *, color: Mapping[str, str] = LISTING,
                size: str = "kilo") -> dict:
    """一張卡：標題、幾行說明、最多 6 個按鈕。按鈕給 (標籤, 點了等於打的那句話) 或一個現成的 action dict
    （例如月曆選日期）。圖卡點選化用：仲介用點的，不用記指令、不用打編號。"""
    c = color
    acts = []
    for b in buttons[:6]:
        if isinstance(b, dict):
            acts.append(b)
        else:
            label, text = b
            acts.append({"type": "message", "label": str(label)[:MAX_LABEL], "text": str(text)[:300]})
    return {"type": "bubble", "size": size,
            "header": {"type": "box", "layout": "vertical", "backgroundColor": c["bar"], "paddingAll": "12px",
                       "contents": [_txt(title, size="md", color="#FFFFFF", weight="bold")]},
            "body": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "12px",
                     "contents": [_txt(x, size="xs", color=c["ink"]) for x in lines if x] or [_txt(" ")]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "10px",
                       "contents": [{"type": "button", "style": "primary" if i == 0 else "secondary", "height": "sm",
                                     **({"color": c["bar"]} if i == 0 else {}), "action": a}
                                    for i, a in enumerate(acts)]}}
