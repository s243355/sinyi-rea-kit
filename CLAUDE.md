# 房仲自動化神器（rea-kit）— 開發守則

> 給在這個 repo 工作的人與 AI 助手（Claude、Codex 等）。使用說明見 README.md。

## 定位

- 給台灣不動產仲介、土地開發仲介、不動產業務的 LINE 自動化工具；優先個人業務使用。
- 開源（Apache-2.0），可自架；也提供租主機與客製化服務（README 結尾）。
- 一般使用者不會寫程式：**任何需要外部串接的設定，都要能在一鍵串接精靈（`/setup`）用點擊＋貼上完成**，不能要求使用者改設定檔。

## 紅線

1. **程式碼零硬編碼**：店名、人名、LINE 群組／使用者 ID、電話、地址、金鑰一律走設定檔、環境變數或資料庫。測試資料一律合成（`tests/fixtures/`、`synthetic_line_id()`）。
2. **金鑰**：只存本機設定檔（`adapters/settings_store.py`，Windows 用 DPAPI）或部署平台的私密環境變數；任何 API、畫面、log 都不回傳金鑰內容。
3. **網路採集**：只用官方 API 或政府開放資料。不爬禁止爬蟲的平台（591、樂屋等改走搜尋服務 API）；不登入別人的系統；有速率上限；遇到 429、驗證碼、robots 禁止就停。
4. **付費 API**：使用者自己申請、自己付費；要「確認」才呼叫、結果快取、每日上限（參考 `adapters/moi_cop.py`）。
5. **個資**：客戶資料是仲介蒐集的，本系統是受託處理者。匯入時電話、地址、身分證欄位一律不收；要有匯出與刪除功能（`adapters/line/my_data.py`）。
6. **背景程式不跳視窗**：Windows 一律 `pythonw`＋`CREATE_NO_WINDOW` 或 VBS `Run …, 0, False`（見 `tools/start.py`）。

## 每次 commit 前

```bash
python tools/run_tests.py          # 原版測試用 solo 跑；全店共用與 ToEstate 測試自己切模式
python tools/validate_skills.py
python tools/pii_scan.py --all
```

## 信義版

- 全店共用（`adapters/team.py`）：客戶與物件放在 `sys-team`，每筆有 `owner`；LINE 互動用全店來源，
  排程與隱私匯出／刪除用 `own_view()`。改別人的資料要擋（`NotOwner`）。新功能不要繞過這兩個來源直接讀寫租戶資料夾。
- 行情（`adapters/toestate.py`）：ToEstate MCP，快取 6 小時、每日上限、429 當天停；行政區失敗改內政部。
- 品牌集中在 `core/brand.py`，不要在別處寫死顏色或產品名稱。
- 修改清單記在 `CHANGES-信義版.md`（Apache-2.0 §4(b)）。

`tools/pii_scan.py` 可以掛 pre-commit（`python tools/install_hooks.py`）；本機另有不入版控的識別字清單 `tools/identifiers.txt`（格式見 `identifiers.example.txt`）。

## 架構速覽

- `core/`：純邏輯（不碰網路與檔案）。
- `adapters/`：所有外部串接；每個串接的「檢查格式＋實測連線＋白話錯誤」集中在 `adapters/integration_checks.py`，精靈與文件都用它。
- `web/`：服務本體；`tools/serve_line.py` 的 `build_wsgi()` 把 webhook、精靈、條款、管理員面板組起來。
- 新增一個串接：① `settings_store.FIELDS` 加欄位 ② `integration_checks` 加檢查 ③ `setup_wizard.ROUTES`＋`setup.html` 加一步 ④ `docs/金鑰申請.md` 加教學 ⑤ `tests/test_setup_wizard.py` 用假網路走一遍。

## 說話方式

對使用者（房仲）一律白話：錯誤訊息要說「發生什麼事＋怎麼修」，不丟英文例外。對外文件加「原則上」等保護語，費用以各官方公告為準。
