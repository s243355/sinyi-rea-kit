"""Google 試算表 → 客戶需求／物件（只讀、單向）。

試算表要開「知道連結的任何人可以檢視」（或「發布到網路」），我們用 Google 官方的 CSV 匯出網址讀，
不需要 Google 帳號授權、不裝任何 Google 套件。匯入規則跟 CSV 一樣：電話、地址、身分證這類欄位一律不收。
資料寫進管理員自己的資料夾（和 LINE 上「客戶清單」「我的物件」是同一份）。
"""
from __future__ import annotations

import os

from adapters.integration_checks import Check, fetch_sheet_csv

ENV = {"customers": "REA_GSHEET_CUSTOMERS_URL", "listings": "REA_GSHEET_LISTINGS_URL"}


def sync(kind: str, tenant_id: str, *, customers, listings, url: str | None = None, opener=None, env=os.environ) -> Check:
    url = url or env.get(ENV[kind], "")
    if not url:
        return Check(False, "還沒串接這份試算表。")
    body, got = fetch_sheet_csv(url, opener=opener)
    if not got.ok:
        return got
    try:
        if kind == "customers":
            rep = customers.import_customers(tenant_id, body)
        else:
            # 試算表是「目前所有物件」的完整清單：沒出現的視為下架
            # 店內物件表多半只列出售物件、沒有「交易」欄：沒寫就當出售
            rep = listings.import_csv(tenant_id, body, source="gsheet", replace=True, default_transaction="buy")
    except ValueError as e:
        return Check(False, f"欄位對不上：{e}")
    return Check(True, f"同步完成：新增 {len(rep.new_ids)}、更新 {len(rep.changed_ids)}、沒變 {rep.unchanged_count}"
                       + (f"、有問題 {len(rep.errors)} 列" if rep.errors else ""),
                 data={"new": len(rep.new_ids), "changed": len(rep.changed_ids), "errors": rep.errors[:5],
                       "new_ids": list(rep.new_ids)})
