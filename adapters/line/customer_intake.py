"""Bound-agent click/text intake. No personal LINE access or paid AI calls.

區域這一題用點選卡（跟找新買方同一套：縣市可複選或全台 → 每個縣市點行政區或全區）；
習慣打字的人照樣可以直接打「台中市西屯區、台中市南屯區」。
"""
import datetime
import re
import unicodedata

from adapters.csv.customer_source import CsvCustomerSource, validate_customer_label
from adapters.line import area_picker as AP
from adapters.line.drafts import looks_like_command
from core.property_schema import PROPERTY_TYPES, canonical_type
from core.tw_districts import COUNTIES, DISTRICTS, area_names

STATE = ("line", "customer_intake.json")
PURPOSE = "intake"
POLICIES = {"必要": "required", "偏好": "preferred", "不限": "any"}
# 仲介平常的講法（2026-10-02 模擬整天操作：打「要車位」被當成格式錯誤、還卡在同一題）
POLICY_WORDS = {
    "required": ("必要", "一定要", "一定要有", "要", "要有", "需要", "必須", "有"),
    "preferred": ("偏好", "最好有", "有更好", "加分", "盡量", "希望有", "可以有", "最好"),
    "any": ("不限", "都可以", "不用", "不需要", "不要", "無所謂", "沒差", "沒關係", "隨便", "略過", "沒有也可以",
            "沒有", "無"),
}
RENT_TYPES = ("電梯大樓", "華廈", "公寓", "透天", "套房", "店面", "辦公", "廠房")
# 常用區間：點的就好（圖卡點選化）；自己打的照樣收
BUY_BUDGETS = ("500以內", "500-800", "800-1200", "1200-1800", "1800-2500", "2500-3500", "3500-5000", "5000-8000")
RENT_BUDGETS = ("1萬以內", "1-1.5萬", "1.5-2.5萬", "2.5-4萬", "4-6萬", "6萬以上")
SIZES = ("不限", "20以上", "30以上", "40以上", "50以上", "20-35", "35-50", "50-80")
WHOLE_FLOOR = ("整層", "整層住家", "整層出租", "住家")
STEPS = ("label", "transaction", "area", "budget", "type", "rooms", "size", "age", "parking", "elevator", "confirm")
HELP = "你的客人想找什麼？\n新增買方｜客戶清單｜找客戶 暱稱\n需求 客戶編號｜修改需求 客戶編號\n改名 客戶編號 新暱稱｜暫停 客戶編號｜恢復 客戶編號\n不需要上傳聊天紀錄；請用暱稱，不要填電話或身分證。"


class IntakeError(ValueError):
    """給使用者看的白話錯誤（我們自己寫的字，不含使用者輸入），可以直接回覆。"""


def message(text, options=()):
    out = {"type": "text", "text": text[:4800]}
    if options:
        out["quickReply"] = {"items": [{"type": "action", "action": {
            "type": "message", "label": x[:20], "text": x}} for x in options[:13]]}
    return out


def _n(v, empty="不限"):
    """2.0 → 2、35.5 → 35.5；沒有值顯示「不限」。"""
    if v is None or v == "":
        return empty
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def summary(customer):
    need = customer.get("need_card", customer.get("need", {}))
    unit = "萬元" if need.get("transaction") == "buy" else "元/月"
    labels = {v: k for k, v in POLICIES.items()}
    return (f"{customer.get('customer_id', '尚未儲存')}｜{customer.get('label', '')}\n"
            f"{'買' if need.get('transaction') == 'buy' else '租'}｜{'、'.join(need.get('preferred_areas', []))}\n"
            f"必要預算：{need.get('budget_min') or 0:g}–{need.get('budget_max') or 0:g} {unit}\n"
            f"型態：{need.get('property_type') or '不限'}｜最少房數：{_n(need.get('rooms_min'))}\n"
            f"坪數：{_n(need.get('size_min') or None)}–{_n(need.get('size_max'))}｜屋齡上限：{_n(need.get('age_max'))}\n"
            f"車位：{labels.get(need.get('parking', 'any'))}｜電梯：{labels.get(need.get('elevator', 'any'))}")


def prompt(draft):
    step = draft["step"]
    if step == "area":
        return AP.card(draft.setdefault("pick", AP.new_pick()), PURPOSE)
    unit = "萬元（例：800-1500）" if draft["need"].get("transaction") == "buy" else "元/月（例：15000-25000）"
    questions = {
        "label": ("先取一個好記的客戶暱稱（最多30字，不填電話）。這不會更改私人 LINE 聯絡人。", ()),
        "transaction": ("客人要買還是租？", ("買", "租")),
        "budget": (f"必要預算範圍：{unit}。點下面的區間，或自己打；只打一個數字代表上限。",
                   BUY_BUDGETS if draft["need"].get("transaction") == "buy" else RENT_BUDGETS),
        "type": ("必要房型？可選不限。" + ("\n整層住家：選大樓、公寓或透天；不挑就選不限。"
                                          if draft["need"].get("transaction") == "rent" else ""),
                 (*(RENT_TYPES if draft["need"].get("transaction") == "rent" else PROPERTY_TYPES), "不限")),
        "rooms": ("必要最少房數？", ("不限", "1", "2", "3", "4", "5")),
        "size": ("必要坪數？點下面的，或自己打（例：30＝至少 30 坪、20-40、40以下）。", SIZES),
        "age": ("必要屋齡上限（年）？", ("不限", "5", "10", "20", "30")),
        "parking": ("車位是一定要有、加分偏好，還是不限制？", tuple(POLICIES)),
        "elevator": ("電梯是一定要有、加分偏好，還是不限制？", tuple(POLICIES)),
        "confirm": (summary(draft) + "\n確認你有權提供這筆需求，再按確認儲存。通知只給你，由你選擇是否轉傳買方。", ("確認儲存", "取消")),
    }
    return message(*questions[step])


ZH_DIGITS = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _clean(text):
    """全形數字、全形減號 → 半形（審查：「１５００」「800－1500」被擋）。"""
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _range(text, *, single="max"):
    """「800-1500」「1500以內」「30以上」。single：只打一個數字算上限（預算）還是下限（坪數）。"""
    t = _clean(text).replace(",", "").replace("萬", "").replace("元", "").replace("坪", "").replace(" ", "")
    floor = bool(re.search(r"(以上|起)$", t))
    ceil = bool(re.search(r"(以內|以下|內)$", t))
    t = re.sub(r"(以內|以下|內|以上|起)$", "", t)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:[-~～〜–—至到](\d+(?:\.\d+)?))?", t)
    if not match:
        raise IntakeError("請打數字，或「下限-上限」（例：800-1500），單位照題目。")
    first, second = match.groups()
    if second:
        lo, hi = float(first), float(second)
    elif floor or (single == "min" and not ceil):
        lo, hi = float(first), None
    else:
        lo, hi = 0, float(first)
    if hi is None:
        return lo, None
    if hi <= 0 or lo > hi:
        raise IntakeError("上限要大於零，而且不能小於下限。")
    return lo, hi


def _count(text, step):
    """「3房」「三房」「3房以上」「10年」「10年內」→ 數字；「3~4房」不收（以前會變 34 房）。"""
    t = _clean(text).replace(" ", "")
    t = re.sub(r"^(至少|最少)", "", t)
    t = re.sub(r"(房|間|年|歲)?(以上|以內|以下|內)?$", "", t)
    if t in ZH_DIGITS:
        return ZH_DIGITS[t]
    if re.fullmatch(r"\d{1,3}", t) and int(t) <= (10 if step == "rooms" else 100):
        return int(t)
    raise IntakeError("房數請打 0–10、屋齡請打 0–100（例：3、3房、10年），或按不限。")


def _typed_areas(text):
    """打字的區域要對得上行政區（審查：「台中市西屯」「台中市西屯區 南屯區」以前存成不存在的地區，永遠配不到）。
    可以：台中市西屯區、台中市西屯、台中西屯區、台中市（整個市）、「台中市西屯區 南屯區」（後面沿用同一縣市）。"""
    out, county = [], None
    for raw in re.split(r"[、,，;；/／\s]+", _clean(text).replace("臺", "台")):
        if not raw:
            continue
        hit = None
        for c in sorted(COUNTIES, key=len, reverse=True):
            for prefix in (c, c[:2]):
                if raw.startswith(prefix):
                    hit = (c, raw[len(prefix):])
                    break
            if hit:
                break
        if hit:
            county, rest = hit
        elif county:
            rest = raw
        else:
            raise IntakeError(f"「{raw[:10]}」要加縣市，例：台中市西屯區。")
        if not rest or rest in ("全區", "全部", "全市", "全縣"):
            out.append(county)
            continue
        names = DISTRICTS[county]
        found = rest if rest in names else next((d for d in names if d[:-1] == rest or d.startswith(rest) and len(rest) >= 2), None)
        if not found:
            raise IntakeError(f"{county}沒有「{rest[:10]}」這個區，請再確認（或點卡片選）。")
        out.append(county + found)
    if not out or len(out) > 10:
        raise IntakeError("請打 1–10 個區域，例：台中市西屯區、台中市南屯區。")
    return list(dict.fromkeys(out))


def _policy(text, step):
    t = text.replace("車位", "").replace("電梯", "").replace("的", "").strip() or text.strip()
    for policy, words in POLICY_WORDS.items():
        if t in words:
            return policy
    raise IntakeError(f"{'車位' if step == 'parking' else '電梯'}請選：必要（一定要有）、偏好（有更好）或不限。")


def advance(draft, text):
    step, need = draft["step"], draft["need"]
    if step == "label":
        if looks_like_command(text):
            raise IntakeError("這看起來是指令，不是客人暱稱。請先取暱稱，或按取消。")
        try:
            draft["label"] = validate_customer_label(text)
        except ValueError as e:                        # 我們自己寫的提示（不含使用者輸入）
            raise IntakeError(str(e) + "。") from None
    elif step == "transaction":
        if text in ("買", "買房", "購買", "要買"):
            need["transaction"] = "buy"
        elif text in ("租", "租屋", "承租", "要租"):
            need["transaction"] = "rent"
        else:
            raise IntakeError("請選買或租。")
    elif step == "area":
        need["preferred_areas"] = _typed_areas(text)
        draft.pop("pick", None)
    elif step == "budget":
        lo, hi = _range(text)
        if hi is None:
            raise IntakeError("預算要有上限，例：800-1500、1500以內。")
        if need.get("transaction") == "rent" and "萬" in text:      # 月租打「2萬」「1.5-2.5萬」
            lo, hi = lo * 10_000, hi * 10_000
        # 單位防呆（實務經驗：「我沒要租」的買方被當租客，500 萬變成月租 500 萬元 → 永遠配不到）
        if need.get("transaction") == "buy" and not 10 <= hi <= 1_000_000:
            raise IntakeError("總價單位是萬元（例：800-1500）。")
        if need.get("transaction") == "rent" and not 1_000 <= hi <= 500_000:
            raise IntakeError("月租單位是元（例：15000-25000）。")
        need["budget_min"], need["budget_max"] = lo, hi
    elif step == "type":
        kind = canonical_type(text)
        if text in WHOLE_FLOOR:
            raise IntakeError("整層住家請選建物型態（大樓、公寓、透天…），不挑就選不限。")
        if text != "不限" and kind not in PROPERTY_TYPES:
            raise IntakeError("請點下面的型態，不挑就選不限。")
        need["property_type"] = None if text == "不限" else kind
    elif step in ("rooms", "age"):
        key = "rooms_min" if step == "rooms" else "age_max"
        if text == "不限":
            need.pop(key, None)
        else:
            # 實務經驗：「3~4 房」被存成 34 房 → 九成物件被濾掉、畫面不報錯（_count 不收）
            need[key] = _count(text, step)
    elif step == "size":
        if text == "不限":
            need.pop("size_min", None)
            need.pop("size_max", None)
        else:
            lo, hi = _range(text, single="min")           # 坪數只打一個數字＝至少幾坪（審查：30 以前被當成最多 30）
            need.pop("size_max", None)
            need["size_min"] = lo
            if hi is not None:
                need["size_max"] = hi
    elif step in ("parking", "elevator"):
        need[step] = _policy(text, step)
    else:
        raise IntakeError("請按確認儲存，或按取消。")
    draft["step"] = STEPS[STEPS.index(step) + 1]


def _pick_area(draft, op, value, card_county):
    """區域題的一次點選 → 這次要回的訊息。選完就把選擇寫進需求、換到預算題。"""
    pick = draft.setdefault("pick", AP.new_pick())
    note, _, advanced = AP.apply(pick, op, value, card_county, finished="done")
    if pick["step"] == "done":
        draft["need"]["preferred_areas"] = area_names(pick["areas"])
        draft.pop("pick")
        draft["step"] = "budget"
    if advanced:
        return ([message(note)] if note else []) + [prompt(draft)]
    if note:
        return [message(note, AP.quick(pick))]
    return [message("請點選卡片上的按鈕，或直接打區域（例：台中市西屯區）。"), prompt(draft)]


def _own_listing_matches(store, listings, tenant_id, cust):
    """新買方存好 → 跟這位仲介自己上架中的物件比一次，有適合的就直接附配對卡。"""
    if listings is None:
        return []
    from adapters.line import agent_profile, flex           # 避免循環匯入（agent_profile 用本檔的 message）
    from web.jobs.match_cards import pairs
    found = pairs(listings.list_active(tenant_id), [cust])
    if not found:
        return []
    signature = agent_profile.load(store, tenant_id).get("signature")
    head = message(f"你的物件裡有 {len(found)} 間可能適合 {cust.get('label') or cust['customer_id']}，卡片往左滑：")
    return [head] + flex.match_messages(found, intro="適合這位客人的物件", signature=signature)[:2]  # 留一格給找房連結


def search_card(store, tenant_id, cust):
    """這位客人的找房連結卡；還沒設定品牌就照列全部，並提醒去設定。"""
    from adapters.line import agent_profile, flex
    from core.search_links import links_for
    from web.jobs.match_cards import need_line
    profile = agent_profile.load(store, tenant_id)
    need = cust.get("need_card") or {}
    if profile.get("brand") and not profile.get("include_own_brand"):
        brand_note = f"已避開你的品牌：{profile['brand']}"
    elif profile.get("brand"):
        brand_note = "你的品牌也一起列。"
    else:
        brand_note = "還沒設定你的品牌，所以沒有避開任何品牌（打「設定我的資料」）。"
    notes = [need_line(need), "591 直接開最新上架；其他平台用 Google 找該站近一週的頁面。", brand_note]
    return flex.links_message(f"找房連結：{cust.get('label') or cust['customer_id']}", notes, links_for(need, profile))


def make_intake_handler(customers: CsvCustomerSource, *, listings=None, now=datetime.datetime.now,
                        auto_search=lambda: False):
    store = customers.store

    def handler(ctx):
        if not ctx.is_owner_dm:
            return
        pb = AP.parse((ctx.event.get("postback") or {}).get("data")) if ctx.event.get("type") == "postback" else None
        if pb and pb.get("p") != PURPOSE:
            return
        if not pb and not ctx.text:
            return
        # One bound owner per tenant; includes durable event dedup for restarts.
        with store.lock(ctx.tenant_id, "customer-intake"):
            state = store.read_json(ctx.tenant_id, *STATE, default={}) or {}
            seen = state.get("seen", [])
            eid = ctx.event.get("webhookEventId")
            if eid and eid in seen:
                ctx.reply_text("這則操作已處理；打「客戶清單」查看，或繼續填下一題。")
                return
            draft = state.get("draft")
            expired = False
            if draft and now().timestamp() - draft.get("started", 0) > 1800:
                draft = None
                expired = True
            words = ctx.text.split(maxsplit=2) or [""]
            command = words[0]
            replies = [message("草稿已逾30分鐘失效，請重新新增買方。", ("新增買方",))] if expired else None
            try:
                if pb:
                    if draft and draft["step"] == "area":
                        replies = _pick_area(draft, pb.get("ap", ""), pb.get("v", ""), pb.get("k", ""))
                        draft["started"] = now().timestamp()
                    elif draft:
                        replies = [message("這張選區域的卡片已經用過了，請回答這一題："), prompt(draft)]
                    elif not expired:
                        replies = [message("這張選區域的卡片已經用過了。", ("新增買方", "客戶清單"))]
                elif ctx.text in ("客戶", "功能", "開始"):
                    replies = [message(HELP, ("新增買方", "客戶清單", "新案"))]
                elif ctx.text == "取消":
                    draft = None
                    replies = [message("已取消草稿，既有客戶不變。", ("新增買方", "客戶清單"))]
                elif draft and draft["step"] == "area" and ctx.text in AP.TEXT_OPS:
                    op, value = AP.TEXT_OPS[ctx.text]
                    replies = _pick_area(draft, op, value, "")
                elif ctx.text == "新增買方" or command == "修改需求":
                    old = None
                    if command == "修改需求":
                        old = customers.get(ctx.tenant_id, words[1]) if len(words) >= 2 else None
                        if not old:
                            raise ValueError("請輸入：修改需求 客戶編號")
                    draft = {"step": "transaction" if old else "label", "need": {"budget_tolerance_pct": 0},
                             "started": now().timestamp()}
                    if old:
                        draft.update(label=old["label"], customer_id=old["customer_id"],
                                     version=old["need_card"].get("version", 1))
                    replies = [prompt(draft)]
                elif command in ("客戶清單", "找客戶"):
                    rows = customers.list_customers(ctx.tenant_id, active_only=False)
                    if command == "找客戶":
                        query = ctx.text.partition(" ")[2].strip().casefold()
                        if not query:
                            raise ValueError("請輸入：找客戶 暱稱或編號")
                        rows = [c for c in rows if query in (c["customer_id"] + " " + (c.get("label") or "")).casefold()]
                        page = 1
                    else:
                        page = int(words[1]) if len(words) > 1 else 1
                    pages = max(1, (len(rows) + 9) // 10)
                    if not 1 <= page <= pages:
                        raise ValueError("頁碼超出範圍")
                    if not rows:
                        replies = [message("還沒有客人。按「新增買方」建第一位，存好會自動比對你的物件。"
                                           if command == "客戶清單" else "找不到符合的客人。", ("新增買方", "客戶清單"))]
                    else:
                        shown = rows[(page-1)*10:page*10]
                        quick = ((f"客戶清單 {page+1}",) if page < pages and command == "客戶清單" else ()) + \
                            tuple(f"需求 {c['customer_id']}" for c in shown)
                        replies = [message(f"共{len(rows)}組｜第{page}/{pages}頁（卡片往左滑，按鈕直接點）",
                                           quick[:1] + ("新增買方",)),
                                   customer_cards(shown)]
                elif command in ("需求", "找房", "找房連結"):
                    old = customers.get(ctx.tenant_id, words[1]) if len(words) > 1 else None
                    if not old:
                        raise ValueError("找不到客戶，請用客戶清單裡的編號")
                    replies = [message(summary(old), (f"修改需求 {old['customer_id']}",)),
                               search_card(store, ctx.tenant_id, old)]
                elif command in ("改名", "暫停", "恢復"):
                    if len(words) < (3 if command == "改名" else 2):
                        raise ValueError("請填客戶編號；改名還需要新暱稱")
                    kwargs = {"label": words[2]} if command == "改名" else {"status": "paused" if command == "暫停" else "active"}
                    cust = customers.update_identity(ctx.tenant_id, words[1], **kwargs)
                    replies = [message(f"已更新 {cust['customer_id']}｜{cust['label']}｜{cust['status']}")]
                elif draft:
                    if draft["step"] == "confirm" and ctx.text == "確認儲存":
                        try:
                            cust = customers.save_need(ctx.tenant_id, label=draft["label"], need=draft["need"],
                                                       customer_id=draft.get("customer_id"),
                                                       expected_version=draft.get("version"))
                        except ValueError:
                            draft = None
                            raise IntakeError("沒存成：這位客人的需求剛被改過（或資料不完整）。"
                                              "請打「客戶清單」看最新的，再「修改需求 編號」重來。") from None
                        draft = None
                        replies = [message("已儲存。\n" + summary(cust) + "\n有符合的新案會標出這個編號與暱稱，請你確認後再分享給他。\n"
                                           + ("每天早上的「今日報告」會自動找各平台的新刊登給你；下面的找房連結也可以馬上看。"
                                              if auto_search() else
                                              "網站自動查詢尚未啟用；下面的找房連結點了就是各平台最新的頁面。"),
                                           (f"找房 {cust['customer_id']}", "新增買方", "客戶清單"))]
                        replies += _own_listing_matches(store, listings, ctx.tenant_id, cust)
                        replies.append(search_card(store, ctx.tenant_id, cust))
                        replies = replies[:5]
                    else:
                        advance(draft, ctx.text)
                        draft["started"] = now().timestamp()          # 30 分鐘「沒動」才作廢，不是從開始算
                        replies = [prompt(draft)]
            except (ValueError, KeyError) as e:
                # Do not echo private input or exception values into logs; IntakeError 是我們自己寫的白話提示。
                hint = str(e) if isinstance(e, IntakeError) else ("這一題的答案看不懂。" if draft else "找不到這個客戶編號，")
                replies = [message(hint + ("請照題目重填，或按取消。" if draft else "打「客戶清單」看編號。"),
                                   () if draft else ("客戶清單",))]
                if draft:
                    question = prompt(draft)
                    if question["type"] == "text":
                        replies[0]["text"] += "\n" + question["text"]
                        if "quickReply" in question:               # 答錯時按鈕也要留著，不然只能自己打字
                            replies[0]["quickReply"] = question["quickReply"]
                    else:
                        replies.append(question)
            if replies:
                if eid:
                    seen = (seen + [eid])[-1000:]
                store.write_json(ctx.tenant_id, *STATE, data={"draft": draft, "seen": seen})
                ctx.reply(replies)
    return handler


def customer_cards(rows: list[dict]) -> dict:
    """客戶清單的卡片：一位一張，按鈕直接做事（不用記「需求 B-…」這種指令）。"""
    from adapters.line import flex
    from web.jobs.match_cards import need_line
    bubbles = []
    for c in rows[:flex.MAX_CAROUSEL]:
        cid, paused = c["customer_id"], c.get("status") != "active"
        bubbles.append(flex.action_card(
            (c.get("label") or "未命名") + (f"（{c['owner_name']}）" if c.get("owner_name") else "")
            + ("（暫停中）" if paused else ""),
            [need_line(c.get("need_card") or {}), f"編號 {cid}"],
            [("看需求・找房", f"需求 {cid}"), ("修改需求", f"修改需求 {cid}"),
             ("恢復配對" if paused else "暫停配對", f"{'恢復' if paused else '暫停'} {cid}")]))
    return flex.flex_message(f"客戶清單：{len(bubbles)} 位", flex.carousel(bubbles))
