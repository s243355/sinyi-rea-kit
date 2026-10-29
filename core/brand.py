# -*- coding: utf-8 -*-
"""品牌：名稱與顏色集中在這裡（LINE 卡片、精靈、管理員面板、條款頁都從這裡拿）。

本分支是信義房屋店內使用的版本：信義綠＋「信義房仲小幫手」。名稱可用 REA_PRODUCT_NAME 改（例：店名＋小幫手）。
"""
import os

NAME = (os.environ.get("REA_PRODUCT_NAME") or "信義房仲小幫手").strip()[:20]

GREEN = "#0a8a43"        # 信義綠：按鈕、標題、卡片色條
GREEN_DARK = "#076b34"   # 按下去／深色字
TINT = "#e7f5ec"         # 淡綠底
INK = "#05401f"          # 綠底上的深色字

# Apache-2.0：本版改自 rea-kit，署名完整內容見 NOTICE（轉散布時必須保留 NOTICE 檔）
ATTRIBUTION = "本工具改自開源專案 rea-kit（作者：林其衞／留白事務所，Apache-2.0 授權）"
