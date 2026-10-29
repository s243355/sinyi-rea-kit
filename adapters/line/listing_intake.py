"""新增物件（仲介自己受託的物件）→ 馬上比對他自己的買方 → 配對卡：推給誰＋分享鈕。

這是免費功能「新物件配對＋審核推送卡」在封測的物件來源：仲介自己點選輸入，不用任何網站採集。
物件只存事實欄位（區域、路段、價格、型態、房數、坪數、屋齡、車位、電梯、刊登網址）；
不收門牌、屋主、電話。物件與配對只在這位仲介自己的租戶資料夾裡。

指令：新增物件｜我的物件｜物件 M001｜配對 M001｜下架 M001｜上架 M001｜新案（全部物件×全部買方）
"""
import datetime
import re

from adapters.csv.customer_source import CsvCustomerSource
from adapters.csv.listing_source import CsvListingSource
from adapters.line import agent_profile, area_picker as AP, drafts, flex
from adapters.line.customer_intake import message
from core.property_schema import PROPERTY_TYPES, canonical_type
from core.tw_districts import COUNTIES, DISTRICTS
from web.jobs.match_cards import pairs

STATE = ("line", "listing_intake.json")
PURPOSE = "listing"
STEPS = ("transaction", "area", "road", "title", "price", "type", "rooms", "size", "age", "parking", "elevator",
         "url", "confirm")
SKIP = "略過"
START_WORDS = {"新增物件", "加物件"}
LIST_WORDS = {"我的物件", "物件清單"}
DIGEST_WORDS = {"新案", "今天新案", "新物件", "配對"}
ID_COMMAND = re.compile(r"(物件|配對|下架|上架)\s*(M\d{3,6})", re.I)
PHONE_OR_MAIL = re.compile(r"0\d[\d\-\s]{7,}\d|\d{7,}|@")
YES_NO = {"parking": ("有車位", "沒有車位"), "elevator": ("有電梯", "沒有電梯")}
MAX_CARD_MESSAGES = 4            # 一次回覆最多 5 則：1 則文字＋最多 4 則卡片


def _yes_no(value):
    return "有" if value is True else ("沒有" if value is False else "未填")


def summary(x: dict) -> str:
    lines = [("出售" if x.get("transaction") == "buy" else "出租") + "｜" + (flex.place_text(x) or "（未選區域）")]
    if x.get("title"):
        lines.append(x["title"])
    if x.get("price"):
        unit = flex.unit_price(x)
        lines.append(flex.format_price(x) + (f"（{unit}）" if unit else ""))
    specs = flex.listing_specs(x)
    if specs:
        lines.append(" ｜ ".join(specs))
    lines.append(f"車位：{_yes_no(x.get('parking'))}｜電梯：{_yes_no(x.get('elevator'))}")
    lines.append("刊登網址：" + ("有" if x.get("url") else "沒有（分享時只送文字）"))
    return "\n".join(lines)


def question(draft: dict) -> dict:
    step, x = draft["step"], draft["listing"]
    if step == "area":
        return AP.card(draft["pick"], PURPOSE)
    rent = x.get("transaction") == "rent"
    asks = {
        "transaction": ("新增物件：這間是出售還是出租？", ("出售", "出租", "取消")),
        "road": ("在哪條路？只寫到路段，例：中山路二段（不要寫門牌）。", (SKIP, "取消")),
        "title": ("幫它取個標題，例：近公園三房平車。", (SKIP, "取消")),
        "price": ("月租多少？（元，例：18000）" if rent else "總價多少？（萬元，例：1280）", ("取消",)),
        "type": ("什麼型態？", (*PROPERTY_TYPES, "取消")),
        "rooms": ("幾房？（店面、土地、車位選 0）", ("0", "1", "2", "3", "4", "5", SKIP, "取消")),
        "size": ("權狀坪數？（例：35.5）", (SKIP, "取消")),
        "age": ("屋齡幾年？（新成屋、預售點 0）", ("0", "3", "5", "10", "15", "20", "25", "30", "40", SKIP, "取消")),
        "parking": ("有車位嗎？", (*YES_NO["parking"], SKIP, "取消")),
        "elevator": ("有電梯嗎？", (*YES_NO["elevator"], SKIP, "取消")),
        "url": ("刊登網址？（591、公司官網等，客人點這個看照片）\n還沒刊登就按略過，分享時只送文字。", (SKIP, "取消")),
        "confirm": (summary(x) + "\n\n確認這是你自己受託、可以分享給客人的物件，再按確認儲存。", ("確認儲存", "取消")),
    }
    return message(*asks[step])


def _free_text(text: str, limit: int) -> str:
    t = text.strip()
    if not t or len(t) > limit or any(ord(c) < 32 for c in t):
        raise ValueError(f"最多 {limit} 個字，請重打。")
    if PHONE_OR_MAIL.search(t):
        raise ValueError("不要放電話或 email。")
    if drafts.looks_like_command(t):
        raise ValueError("這看起來是指令或客戶編號，不是物件資料（會跟著分享給客人）。請重打，或按取消。")
    return t


def _number(text: str, lo: float, hi: float, what: str) -> float:
    t = text.replace(",", "").replace("，", "").replace("萬", "").replace("元", "").replace("坪", "") \
            .replace("年", "").replace("房", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", t) or not lo <= float(t) <= hi:
        raise ValueError(f"{what}請打數字。")
    return float(t)


def _typed_area(text: str):
    """打字也可以：台中市西屯區、臺中市西屯區。"""
    t = text.replace("臺", "台").replace(" ", "")
    for county in COUNTIES:
        if t.startswith(county) and t[len(county):] in DISTRICTS[county]:
            return county, t[len(county):]
    return None


def advance(draft: dict, text: str) -> None:
    step, x = draft["step"], draft["listing"]
    skip = text == SKIP
    if step == "transaction":
        if text not in ("出售", "出租"):
            raise ValueError("請選出售或出租。")
        x["transaction"] = "buy" if text == "出售" else "rent"
        draft["pick"] = AP.new_pick(single=True)
    elif step == "area":
        hit = _typed_area(text)
        if not hit:
            raise ValueError("請點卡片選縣市和行政區，或打「台中市西屯區」這樣。")
        x["city"], x["area"] = hit[0], hit[0] + hit[1]
        draft.pop("pick", None)
    elif step == "road":
        if not skip:
            road = _free_text(text, 20)
            if re.search(r"號|樓|之\s*\d", road):
                raise ValueError("只寫到路段就好，不要寫門牌號碼。")
            x["road"] = road
    elif step == "title":
        if not skip:
            x["title"] = _free_text(text, 40)
    elif step == "price":
        rent = x["transaction"] == "rent"
        x["price"] = _number(text, 100, 10_000_000, "月租") if rent else _number(text, 1, 1_000_000, "總價（萬）")
        x["price_unit"] = "元/月" if rent else "萬"
    elif step == "type":
        kind = canonical_type(text)
        if kind not in PROPERTY_TYPES:
            raise ValueError("請點下面的型態。")
        x["property_type"] = kind
    elif step == "rooms":
        if not skip:
            x["rooms"] = int(_number(text, 0, 50, "房數"))
    elif step == "size":
        if not skip:
            x["size_ping"] = _number(text, 0.1, 100_000, "坪數")
    elif step == "age":
        if not skip:
            x["age_years"] = _number(text, 0, 150, "屋齡")
    elif step in YES_NO:
        if not skip:
            t = text.strip()
            yes = t in (YES_NO[step][0], "有", "是", "有的") or (t.startswith("有") and not t.startswith("有沒有"))
            no = t in (YES_NO[step][1], "沒有", "無", "沒", "否", "不")
            if not (yes or no):
                raise ValueError("請點下面的選項。")
            x[step] = yes
    elif step == "url":
        if not skip:
            url = text.strip()
            if not re.fullmatch(r"https?://[^\s]{4,500}", url):
                raise ValueError("網址要用 https:// 開頭；沒有就按略過。")
            x["url"] = url
    else:
        raise ValueError("請按確認儲存或取消。")
    draft["step"] = STEPS[STEPS.index(step) + 1]


def match_replies(found, *, intro: str, signature: str | None, head: str, quick=()) -> list[dict]:
    cards = flex.match_messages(found, intro=intro, signature=signature)[:MAX_CARD_MESSAGES]
    shown = min(len(found), flex.MAX_CAROUSEL * len(cards))
    more = f"\n（只列前 {shown} 組）" if shown < len(found) else ""
    return [message(head + more, quick)] + cards


def tell_colleagues(rec: dict, buyers: list[dict], *, adder: str, notify) -> list[str]:
    """全店共用：新物件配到「同事的買方」→ 通知那位同事（一人一則短訊，不帶客人資料以外的東西）。回通知了誰。

    notify(租戶, 文字) 由組裝的地方給（走推播閘、吃推播額度）；沒給就不通知。"""
    if notify is None:
        return []
    by_owner: dict[str, list[dict]] = {}
    for c in buyers:
        if c.get("owner") and c["owner"] != adder:
            by_owner.setdefault(c["owner"], []).append({k: v for k, v in c.items() if k != "owner_name"})
    told = []
    for owner, group in sorted(by_owner.items()):
        found = pairs([rec], group)
        if not found:
            continue
        names = "、".join(dict.fromkeys(label for _, label, _, _ in found))
        text = (f"🔔 {rec.get('owner_name') or '同事'}新增了 {rec['id']}（{rec.get('area') or ''}｜"
                f"{rec.get('property_type') or ''}｜{flex.format_price(rec)}），配到你的買方：{names}。\n"
                f"打「{rec['id']}」看物件，記得先跟同事確認再約看。")
        try:
            notify(owner, text)
            told.append(owner)
        except Exception:
            pass                                        # 通知失敗不影響存物件
    return told


def make_listing_handler(listings: CsvListingSource, customers: CsvCustomerSource, *, now=datetime.datetime.now,
                         auto_search=lambda: False, notify_colleague=None, adder_name=None):
    """auto_search()：搜尋服務接上了沒（接上才說「今日報告會自動找」，沒接就講實話）。
    notify_colleague(租戶, 文字)：全店共用時，新物件配到同事的買方就通知他；adder_name(租戶) 給通知裡的人名。"""
    store = listings.store

    def handler(ctx):
        if not ctx.is_owner_dm:
            return
        pb = AP.parse((ctx.event.get("postback") or {}).get("data")) if ctx.event.get("type") == "postback" else None
        if pb and pb.get("p") != PURPOSE:
            return
        text = ctx.text
        if not pb and not text:
            return
        tenant = ctx.tenant_id
        with store.lock(tenant, "listing-intake"):
            state = store.read_json(tenant, *STATE, default={}) or {}
            draft = state.get("draft")
            expired = bool(draft) and now().timestamp() - draft.get("started", 0) > drafts.TTL
            if expired:
                draft = None
            by_id = ID_COMMAND.fullmatch(text or "")
            command = text in START_WORDS or text in LIST_WORDS or text in DIGEST_WORDS or by_id
            if not draft and not pb and not command:
                return
            eid = ctx.event.get("webhookEventId")
            if eid and eid in state.get("seen", []):
                ctx.reply_text("這則已處理；打「我的物件」查看。")
                return
            replies = [message("新增物件的草稿超過 30 分鐘，已作廢；請重新新增。", ("新增物件",))] if expired else None
            profile = agent_profile.load(store, tenant)
            signature = profile.get("signature")
            if pb:
                if draft and draft["step"] == "area":
                    note, changed, advanced = AP.apply(draft["pick"], pb.get("ap", ""), pb.get("v", ""),
                                                       pb.get("k", ""), finished="done")
                    if draft["pick"]["step"] == "done":
                        county, district = draft["pick"]["cities"][0], draft["pick"]["areas"][draft["pick"]["cities"][0]][0]
                        draft["listing"].update(city=county, area=county + district)
                        draft.pop("pick")
                        draft["step"] = "road"
                    replies = ([message(note)] if note else []) + [question(draft)] if (advanced or changed) else \
                        [message("請點卡片上的縣市或行政區。"), question(draft)]
                elif draft:
                    replies = [message("這張卡已經用過了，請回答這一題："), question(draft)]
                elif not expired:
                    replies = [message("這張選區域的卡片已經用過了。", ("新增物件", "我的物件"))]
            elif text == "取消" and draft:
                draft = None
                replies = [message("已取消新增物件，既有物件不變。", ("新增物件", "我的物件"))]
            elif text in START_WORDS:
                other = drafts.busy(store, tenant, now().timestamp(), exclude="listing")
                if draft:
                    replies = [message("你正在新增物件，請回答這一題，或按取消："), question(draft)]
                elif other:
                    replies = [message(f"你還有{other}沒完成，請先完成或輸入取消。")]
                else:
                    draft = {"step": "transaction", "listing": {}, "started": now().timestamp()}
                    replies = [question(draft)]
            elif draft:
                if draft["step"] == "confirm" and text == "確認儲存":
                    rec = listings.save_one(tenant, {**draft["listing"], "own": True, "source": "line",
                                                     "listed_at": now().date().isoformat()})
                    draft = None
                    buyers = customers.list_customers(tenant)
                    found = pairs([rec], buyers)
                    if adder_name:
                        rec = {**rec, "owner_name": adder_name(tenant)}
                    told = tell_colleagues(rec, buyers, adder=tenant, notify=notify_colleague)
                    head = (f"✓ 已存成 {rec['id']}。買方裡有 {len(found)} 位可能適合，卡片往左滑；"
                            "按「📤 分享給客戶」用你自己的 LINE 傳給他。" if found else
                            f"✓ 已存成 {rec['id']}。目前買方裡沒有適合這間的；之後新增買方會自動比對。")
                    if told:
                        head += f"\n（已通知 {len(told)} 位同事：他們的買方也配到這間）"
                    replies = match_replies(found, intro=f"{rec['id']} 適合的客人", signature=signature, head=head,
                                            quick=("新增物件", "我的物件", "新增買方"))
                elif text in START_WORDS | LIST_WORDS | DIGEST_WORDS or by_id or drafts.looks_like_command(text):
                    replies = [message("你正在新增物件，請先回答這一題，或按取消："), question(draft)]
                else:
                    try:
                        advance(draft, text)
                        draft["started"] = now().timestamp()          # 30 分鐘「沒動」才作廢
                        replies = [question(draft)]
                    except ValueError as e:
                        replies = [message(str(e)), question(draft)]
            elif text in LIST_WORDS:
                rows = [x for x in listings.list_all(tenant) if not x.get("owner_name")]   # 全店共用時只列自己的
                own = [x for x in rows if x.get("own")] or rows
                if not own:
                    replies = [message("你還沒有物件。按「新增物件」把手上的委託加進來，馬上比對你的買方。", ("新增物件",))]
                else:
                    lines = [f"{x['id']}｜{'售' if x.get('transaction') == 'buy' else '租'}｜{x.get('area')}｜"
                             f"{flex.format_price(x)}｜{'上架中' if x.get('online') else '已下架'}" for x in own[-20:]]
                    replies = [message(f"你的物件（{len(own)} 間，列最近 20 間）：\n" + "\n".join(lines)
                                       + "\n卡片往左滑，按鈕直接點。", ("新增物件",)),
                               listing_cards(list(reversed(own[-flex.MAX_CAROUSEL:])))]
            elif by_id:
                verb, lid = by_id.group(1), by_id.group(2).upper()
                x = listings.get(tenant, lid)
                if not x:
                    replies = [message(f"找不到 {lid}；打「我的物件」看編號。", ("我的物件",))]
                elif verb == "物件":
                    toggle = f"下架 {lid}" if x.get("online") else f"上架 {lid}"
                    replies = [message(f"{lid}\n" + summary(x), (f"配對 {lid}", toggle, "我的物件"))]
                elif verb in ("下架", "上架"):
                    listings.mark_online(tenant, lid, verb == "上架")
                    replies = [message(f"✓ {lid} 已{verb}。" + ("下架的物件不會再配對。" if verb == "下架" else ""),
                                       ("我的物件",))]
                elif not x.get("online"):
                    replies = [message(f"{lid} 已下架，不配對。要重新配對請先「上架 {lid}」。", (f"上架 {lid}",))]
                else:
                    found = pairs([x], customers.list_customers(tenant))
                    replies = match_replies(found, intro=f"{lid} 適合的客人", signature=signature,
                                            head=(f"{lid} 有 {len(found)} 位買方可能適合：" if found
                                                  else f"{lid} 目前沒有適合的買方。"), quick=("我的物件",))
            elif text in DIGEST_WORDS:
                active = [x for x in listings.list_active(tenant)]
                if not active:
                    replies = [message("你還沒有新增物件，所以沒有可以配對的。\n"
                                       "按「新增物件」把你手上的委託加進來，馬上比對你的買方。\n"
                                       + ("外部平台的新物件：每天早上的「今日報告」會自動找，也可以打「今日報告」馬上找。"
                                        if auto_search() else
                                        "外部平台的新物件：網站自動搜尋尚未接通，先用每位客人的「找房連結」。"),
                                       ("新增物件", "客戶清單"))]
                else:
                    found = pairs(active, customers.list_customers(tenant))
                    replies = match_replies(found, intro="新案配對", signature=signature,
                                            head=(f"你的 {len(active)} 間物件 × 你的買方：共 {len(found)} 組配對。"
                                                  if found else f"你的 {len(active)} 間物件目前都沒有適合的買方。"),
                                            quick=("我的物件", "新增物件"))
            if not replies:
                return
            state["draft"] = draft
            if eid:
                state["seen"] = (state.get("seen", []) + [eid])[-500:]
            store.write_json(tenant, *STATE, data=state)
            ctx.reply(replies)
    return handler


def listing_cards(rows: list[dict]) -> dict:
    """我的物件的卡片：一間一張（最新的在前），按鈕直接做事。"""
    bubbles = []
    for x in rows[:flex.MAX_CAROUSEL]:
        lid, online = x["id"], bool(x.get("online"))
        specs = " ｜ ".join(flex.listing_specs(x))
        bubbles.append(flex.action_card(
            f"{lid} {x.get('title') or flex.place_text(x) or ''}".strip() + ("" if online else "（已下架）"),
            [flex.place_text(x), flex.format_price(x), specs],
            ([("配對買方", f"配對 {lid}")] if online else []) + [("看這間", f"物件 {lid}"),
                                                                 ("下架" if online else "重新上架", f"{'下架' if online else '上架'} {lid}")]))
    return flex.flex_message(f"我的物件：{len(bubbles)} 間", flex.carousel(bubbles))
