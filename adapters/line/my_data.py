"""仲介自己的資料：隱私說明、客人告知範本、匯出、刪除（個資法：受委託處理者要做得到，CLAUDE.md §4）。

指令（只認綁定的仲介本人、一對一聊天；排在所有流程前面，有草稿也叫得到）：
  隱私說明｜客人告知範本｜匯出我的資料｜刪除我的資料 →（10 分鐘內）確定刪除我的資料
匯出：客人與物件各一份 CSV 文字（LINE 不能傳檔案；複製貼到記事本存成 .csv 就能用 Excel 開），太多就截斷並說明。
刪除：解除綁定＋整個租戶資料夾刪掉；備份在 14 天內輪替消失（看門狗只留 14 份）。
"""
from __future__ import annotations

import csv
import datetime
import io

from adapters.line import agent_profile
from adapters.line.customer_intake import message
from core import brand

STATE = ("line", "my_data.json")
PRIVACY, NOTICE, EXPORT, DELETE, CONFIRM = "隱私說明", "客人告知範本", "匯出我的資料", "刪除我的資料", "確定刪除我的資料"
WORDS = {PRIVACY, NOTICE, EXPORT, DELETE, CONFIRM}
CONFIRM_MINUTES = 10
CHUNK = 4500                      # 一則文字上限 5000，留空間給標題
LISTING_FIELDS = ("id", "transaction", "area", "road", "title", "price", "price_unit", "property_type", "rooms",
                  "size_ping", "age_years", "parking", "elevator", "url", "online", "listed_at")

PRIVACY_TEXT = (
    f"{brand.NAME}替你保存的資料：\n"
    "・你建的客人需求（暱稱、地區、預算、型態…，不收電話和身分證）\n"
    "・你新增的物件、海巡設定、你的品牌與署名\n"
    "・推播紀錄（只記則數，不留別人的貼文內容）\n\n"
    "存在服務主機（台灣），只有你本人看得到；不給其他仲介、不拿去做別的用途。\n"
    "客人資料是你蒐集的，我們只是替你保管（受託處理）；要告訴客人的話，打「客人告知範本」。\n\n"
    "你隨時可以「匯出我的資料」或「刪除我的資料」。刪除後，備份也會在 14 天內輪替消失。")


def notice_text(signature: str | None) -> str:
    """給客人的簡短告知（個資法第 8 條：誰、為什麼、收什麼、留多久給誰、你的權利、不給的影響）。"""
    who = signature or "（你的名字｜公司）"
    return (f"您好，我是{who}。\n"
            "為了幫您找房，我會記下您的需求（想找的地區、預算、房型等，不需要身分證字號），"
            "只用來替您配對物件、和您聯絡；保存到您不再需要服務為止。\n"
            "這些資料由我使用的房仲工具在台灣的主機保管，只有我看得到，不會提供給其他人。\n"
            "您隨時可以請我查詢、更正或刪除；不想再收到物件資訊，跟我說一聲就好。\n"
            "如果不方便提供需求，我就沒辦法替您配對物件，但不影響您自己看房。")


def _csv(rows: list[dict], fields) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: "" if r.get(k) is None else r.get(k) for k in fields})
    return buf.getvalue()


def _chunks(text: str) -> list[str]:
    """照行切，每段不超過 CHUNK 字（不把一筆資料切成兩半）。"""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > CHUNK:
            out.append(cur)
            cur = ""
        cur += line[:CHUNK]
    return out + ([cur] if cur else [])


def export_messages(customers, listings, tenant_id: str) -> list[dict]:
    people = customers.list_customers(tenant_id, active_only=False)
    rows = listings.list_all(tenant_id) if listings else []
    head = (f"這是你存在{brand.NAME}的資料：客人 {len(people)} 位、物件 {len(rows)} 間。\n"
            "複製下面的文字貼到記事本，存成 .csv 就能用 Excel 開。")
    msgs, cut = [message(head)], False
    for name, text in (("客人", customers.export(tenant_id)), ("物件", _csv(rows, LISTING_FIELDS))):
        parts = _chunks(text)
        for i, part in enumerate(parts[:2]):
            msgs.append(message(f"【{name}.csv" + (f"（{i + 1}）" if len(parts) > 1 else "") + f"】\n{part}"))
        cut |= len(parts) > 2
    if cut:
        msgs[0]["text"] += "\n資料比較多，LINE 一次只能給這些；完整檔案請跟管理員要，會整包給你。"
    return msgs[:5]


def make_my_data_handler(store, customers, listings, *, registry, now=datetime.datetime.now):
    """registry：拿綁定表的函式（App 建好之後才有，所以晚一點取）。"""
    def handler(ctx):
        if not ctx.is_owner_dm or ctx.text not in WORDS:
            return
        tenant = ctx.tenant_id
        if ctx.text == PRIVACY:
            ctx.reply([message(PRIVACY_TEXT, (NOTICE, EXPORT, DELETE))])
        elif ctx.text == NOTICE:
            sig = agent_profile.load(store, tenant).get("signature")
            ctx.reply([message("下面這段可以直接轉傳給客人（第一次記他的需求時傳一次就好）：", ("隱私說明",)),
                       message(notice_text(sig))])
        elif ctx.text == EXPORT:
            ctx.reply(export_messages(customers, listings, tenant))
        elif ctx.text == DELETE:
            n_c = len(customers.list_customers(tenant, active_only=False))
            n_l = len(listings.list_all(tenant)) if listings else 0
            store.write_json(tenant, *STATE, data={"pending": now().isoformat(timespec="seconds")})
            ctx.reply([message(f"⚠ 這會刪掉你在這裡的全部資料：客人 {n_c} 位、物件 {n_l} 間、海巡設定、推播紀錄，"
                               "並解除這個 LINE 的綁定。刪了就救不回來（備份也會在 14 天內輪替消失）。\n"
                               f"想先留一份就打「{EXPORT}」。確定要刪，{CONFIRM_MINUTES} 分鐘內按「{CONFIRM}」。",
                               (EXPORT, CONFIRM))])
        else:
            pending = (store.read_json(tenant, *STATE, default={}) or {}).get("pending")
            fresh = False
            if pending:
                try:
                    fresh = now() - datetime.datetime.fromisoformat(pending) <= datetime.timedelta(minutes=CONFIRM_MINUTES)
                except ValueError:
                    fresh = False
            if not fresh:
                ctx.reply([message(f"要刪除請先打「{DELETE}」，看清楚會刪什麼再確認。", (DELETE,))])
                return
            registry().unbind_tenant(tenant)
            # 全店共用時客人與物件放在店資料夾，刪租戶資料夾刪不到，要另外把「自己建的」刪掉
            customers.delete_all(tenant)
            if listings:
                listings.delete_all(tenant)
            from adapters import team
            team.drop_name(store, tenant)
            store.delete_tenant(tenant)
            ctx.reply([message(f"✓ 已刪除你在{brand.NAME}的全部資料，也解除了這個 LINE 的綁定。\n"
                               "備份會在 14 天內輪替消失。之後想再用，按「同意並開始」就好。")])
    return handler
