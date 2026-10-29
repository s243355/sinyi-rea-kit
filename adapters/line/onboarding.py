"""歡迎卡與新手體驗。負責人 2026-10-04：「要上線了，給大家使用」→ 加好友按「同意並開始」就能用（不用綁定碼）。"""
from core import brand


PROFILE = ("line", "demo_profile.json")
MORE_WORDS = {"更多功能", "更多", "功能選單", "選單"}


def _action(label, command):
    if command.startswith("https://"):                       # 網址 → 開網頁（服務條款）
        return {"type": "uri", "label": label, "uri": command}
    return {"type": "message", "label": label, "text": command}


def card(title, text, choices):
    return {"type": "flex", "altText": title,
            "contents": {"type": "bubble", "body": {"type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg", "wrap": True},
                             {"type": "text", "text": text, "wrap": True, "size": "sm", "margin": "md"}]},
                "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                    {"type": "button", "action": _action(label, command),
                     **({"style": "primary", "color": brand.GREEN} if i == 0 else {})}
                    for i, (label, command) in enumerate(choices)]}}}


INTRO = ("給房仲、土地開發、不動產業務用的 LINE 小幫手：\n"
         "・每天自動找「想買房、想租房」的人的貼文\n"
         "・每天整理你區域各品牌新上架的物件\n"
         "・幫你的買方自動配對新物件\n")


def welcome(bound=False, terms_url=None):
    if bound:
        return card(f"歡迎使用{brand.NAME}", "第一次使用按「開始新手體驗」；之後直接點下面的功能，或聊天室下方的選單。",
                    [("開始新手體驗", "新手體驗"), ("新增物件・配對買方", "新增物件"), ("替買方找房", "新增買方"),
                     ("找新買方", "找新買方"), ("客戶清單", "客戶清單"), ("我的資料", "我的資料")])
    return card(f"歡迎使用{brand.NAME}", INTRO + "\n不用下載、不用填資料，按「同意並開始」就能用。"
                "按下即表示同意服務條款與個資委託處理條款。",
                [("同意並開始", "同意並開始")] + ([("看服務條款", terms_url)] if terms_url else []))


def make_onboarding_handler(store, *, now, is_admin=None):
    def handler(ctx):
        if not ctx.is_owner_dm:
            return
        text = ctx.text
        if text == "新手體驗":
            ctx.reply([card("第一步：你的基本資料", "先告訴我你在哪個品牌：找房連結和海巡會先避開你自己品牌的物件"
                            "（你要的話也可以列）。分享物件給客人時，可以附你自己的署名。",
                            [("設定我的資料", "設定我的資料"), ("先跳過", "確認合成檔案"), ("取消", "退出新手體驗")])])
        elif text == "確認合成檔案":
            with store.lock(ctx.tenant_id, "demo-profile"):
                profile = store.read_json(ctx.tenant_id, *PROFILE, default=None)
                if not profile:
                    store.write_json(ctx.tenant_id, *PROFILE, data={"label": "合成測試仲介", "synthetic": True,
                                                                    "consent_at": now().isoformat()})
            ctx.reply([card("第二步：想先做哪件事？", "新增物件：把你手上的委託加進來，馬上比對你的買方。\n"
                            "替買方找房：建立買方需求，每天自動找各平台的新刊登推給你。\n"
                            "找新買方：設定服務範圍，每天自動找想買房的人的貼文推給你（早上報告，下午有新的再加報）。",
                            [("新增物件・配對買方", "新增物件"), ("替買方找房", "新增買方"), ("找新買方", "找新買方"),
                             ("清除合成檔案", "清除合成檔案")])])
        elif text in MORE_WORDS:
            from adapters.line import flex
            groups = [("找客人", ["今日海巡：馬上找想買房的人的貼文", "海巡設定：地區、關鍵字、推播時間"],
                       [("今日海巡", "今日海巡"), ("海巡設定", "找新買方"), ("今日報告", "今日報告")]),
                      ("客人與物件", ["客戶清單、我的物件都是卡片，按鈕直接點"],
                       [("客戶清單", "客戶清單"), ("我的物件", "我的物件"), ("新增買方", "新增買方"),
                        ("新增物件", "新增物件")]),
                      ("查行情", ["打「行情 社區名」或「行情 行政區」", "實價登錄概況，不是估價"],
                       [("怎麼查行情", "行情")]),
                      ("工具與設定", ["你的品牌與署名、隱私與資料"],
                       [("我的資料", "我的資料"), ("隱私說明", "隱私說明")])]
            if is_admin and is_admin(ctx.user_id):           # 管理員才看得到這張
                groups.append(("管理員", ["仲介名單、海巡品質、額度、全市場盤點"], [("管理員面板", "管理員面板")]))
            ctx.reply([flex.flex_message("更多功能", flex.carousel(
                [flex.action_card(t, lines, buttons) for t, lines, buttons in groups]))])
        elif text == "清除合成檔案":
            with store.lock(ctx.tenant_id, "demo-profile"):
                store.write_json(ctx.tenant_id, *PROFILE, data=None)
            ctx.reply_text("已清除合成基本檔案；既有買方與服務設定不變。")
        elif text == "退出新手體驗":
            ctx.reply_text("已退出，本次未建立新檔案；既有資料不變。")
    return handler
