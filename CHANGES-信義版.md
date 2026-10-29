# 信義版修改紀錄

本版改自 [rea-kit](https://github.com/Larryveryhandsome/rea-kit)（作者：林其衞／留白事務所，Apache-2.0）。
依 Apache-2.0 第 4(b) 條，以下列出本版修改與新增的檔案；原作者署名見 [NOTICE](NOTICE)，轉散布時 LICENSE 與 NOTICE 都要保留。

## 2026-10-29 信義版第一版

### 1. 全店共用買方與物件（`REA_TEAM_MODE=shared`，預設）

同一個官方帳號底下的同仁，買方與物件放在同一個池子：

- 大家都看得到全店的買方與物件；同仁新增物件時，會配到**全店**的買方。
- 配到「別人的買方」時，卡片標「王先生（家儒的客人）」，並用 LINE 通知那位同仁。
- 只有建立的人或店長（管理員）能修改、暫停、下架。
- 「客戶清單」自己的排前面；「我的物件」只列自己的。
- 每日報告、隱私匯出／刪除只動「自己建的」資料（不會重複跑別人的客人，也不會刪到別人的）。
- 管理員面板：邀請新成員時可填姓名、可改顯示名稱（配對卡與通知用這個名字）。
- 串接精靈 LINE 第 7 步可切回「各自獨立」（`solo`，跟原版一樣）。

新增：`adapters/team.py`、`tests/test_team_mode.py`
修改：`web/line_app.py`、`adapters/line/listing_intake.py`、`adapters/line/customer_intake.py`、
`adapters/line/my_data.py`、`web/jobs/match_cards.py`、`web/admin_app.py`、`web/setup_wizard.py`、`web/setup.html`、
`adapters/settings_store.py`

### 2. 店內共用物件試算表

- 認得信義常用欄名：委託編號、售價、委託價、權狀坪數……；新增「負責同仁」欄（對到名冊上的名字，那間就算他的）。
- 沒有「交易」欄的表一律當出售。
- 每天同步進來的新物件會配全店買方，配到誰的買方就 LINE 通知誰（一人一則）。
- 修正：物件表同步「表上沒有＝下架」只套用在表單來源的物件，不會把同仁在 LINE 上新增的物件下架。

新增：`web/jobs/team_notify.py`
修改：`adapters/csv/listing_source.py`、`adapters/gsheet_sync.py`、`adapters/integration_checks.py`、
`web/scheduler.py`、`web/setup_wizard.py`（範本加「負責同仁」欄）

### 3. 行情查詢：ToEstate＋內政部備援

- LINE 指令「行情 社區名」「行情 行政區」（也接受「實價」「實登」「成交」）。
- 先查 ToEstate（MCP 端點，社區層級、已扣車位，含最近成交與瑕疵備註提醒）；
  連不上、限流或額度用完，行政區改用內政部開放資料。
- 每日上限 300 次、6 小時快取、429 當天停止；可選填商用授權金鑰。
- 串接精靈「實價登錄・行情」分頁新增 ToEstate 連線測試與預設縣市。

新增：`adapters/toestate.py`、`core/market_query.py`、`adapters/line/market_lookup.py`、`tests/test_toestate.py`
修改：`adapters/integration_checks.py`（`check_toestate`、`Resp.session_id`）、`adapters/settings_store.py`、
`web/setup_wizard.py`、`web/setup.html`、`adapters/line/onboarding.py`（功能選單加「查行情」）、`docs/金鑰申請.md`

### 4. 信義綠品牌

- 名稱改為「信義房仲小幫手」（`REA_PRODUCT_NAME` 可改），主色信義綠 `#0a8a43`。
- 拿掉原版的租主機與客製化廣告，改成頁尾署名（完整署名仍在 NOTICE）。

新增：`core/brand.py`
修改：`adapters/line/flex.py`、`adapters/line/area_picker.py`、`adapters/line/onboarding.py`、`adapters/line/my_data.py`、
`adapters/line/webhook.py`、`adapters/line/shared.py`、`web/admin_app.py`、`web/terms_page.py`、`web/setup.html`、
`web/setup_wizard.py`、`web/index.html`、`web/styles.css`、`web/mcp_tools.py`、`tools/start.py`、`tools/serve_line.py`

### 5. 測試

- `python tools/run_tests.py`：原版測試固定用 `solo` 跑（它們直接檢查個人資料夾），全店共用由 `tests/test_team_mode.py` 自己切 `shared` 測。
- 因品牌與廣告調整而改的原版測試：`tests/test_custom_ad_console.py`、`tests/test_public_signup.py`、`tests/test_setup_wizard.py`。
- CI：`.github/workflows/test.yml` 改用 `tools/run_tests.py`。
