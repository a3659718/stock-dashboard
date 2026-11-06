# 台美股雷達 Dashboard

Streamlit 網站 + GitHub Actions 排程推播。手機 / 桌機都能開，涵蓋台股盤後篩選、即時強勢族群、美股精選、
市場情緒、持倉 / 自選股警報、政策情境族群，並推送到 Telegram。

> **本專案僅供研究參考，非投資建議。**

---

## 目錄

1. [功能](#功能)
2. [專案結構](#專案結構)
3. [部署](#部署)
4. [推播延遲（重要）](#推播延遲重要)
5. [政策情境族群：2027 民主黨國會](#政策情境族群2027-民主黨國會)
6. [本機開發與測試](#本機開發與測試)
7. [已知限制](#已知限制)

---

## 功能

| 分頁 / 推播 | 內容 |
|---|---|
| 🇹🇼 台股篩選 | 突破 MA20 / MA60、量能倍增、融券增加、投信首買，可切換命中條件數 |
| 🚀 強勢族群 | 流動性 Top 200 依產業 / 題材分組，族群熱度、龍頭、潛伏題材、漲停前兆 |
| 🇺🇸 美股 | 技術面 + 動能 + 相對 SPY 強度 + 新聞題材，進場 / 停損 / 目標價 |
| 🧭 市場情緒 | Fear & Greed、板塊輪動矩陣、市場新聞、**政策情境族群** |
| 💼 持倉 / ⭐ 自選 | 停損、漲跌門檻、條件觸發（價位、均線、KD、MACD；**觸發即推播**，收盤後判斷交叉） |
| 📦 主動式 ETF | 00981A 前十大持股異動監控（MoneyDJ） |
| ✈️ 排程推播 | 晨報、盤前（日韓）、台股開盤 / 中盤 / 盤後、外資籌碼、美股盤前 / 開盤 / 中盤 / 收盤、盤中監控、週報 |

---

## 專案結構

2026-11 重構：原本根目錄 98 個平鋪的 `.py` 依職責搬進 `radar/` 套件。入口不變：
Streamlit 跑 `app.py`，排程跑 `scripts/*.py`。

```
stock_dashboard/
├── app.py                      # Streamlit 入口 (Main file path 仍是 app.py)
├── radar/                      # 核心套件
│   ├── core/                   # 基礎設施: 推播去重 push_dedup / 上限 push_cap、遲到守衛 schedule_guard、
│   │                           #   狀態 watchlist_store / holdings_store、健康 system_health / heartbeat、
│   │                           #   訊號追蹤 signal_tracker / pick_review、安全格式化 safe_fmt
│   ├── data/                   # 外部資料: data_sources (FinMind / yfinance / F&G)、news_sources、
│   │                           #   event_sources、asia_markets、fundamentals_us、earnings / econ_calendar
│   ├── notify/                 # notifier — Telegram 送出 (分段 / 重試 / 429) + 所有訊息模板
│   ├── analysis/               # 分析引擎: 指標、籌碼、題材、族群、AI 評分、進場評估、回測、policy_regime
│   ├── screeners/              # 選股器: tw_screener / us_screener / upside / actionable_picks …
│   ├── alerts/                 # 各類排程警報: index_alerts、pre_market_alert、watchlist_alerts …
│   └── ui/                     # Streamlit 輔助: app_styles、dashboard_actions
├── scripts/
│   ├── market_open_alert.py    # 排程主入口 (依 market 參數分派)
│   ├── morning_brief.py        # 08:00 晨報
│   ├── punctual_slots.json     # 準時觸發時刻表 (單一來源)
│   ├── setup_cronjob_org.py    # 一鍵註冊 cron-job.org 雲端準時鬧鐘
│   ├── gh_dispatch.ps1 / register_punctual_tasks.ps1   # 備案: 用 Windows 工作排程器準時觸發
│   ├── smoke_test_full.py / check_conventions.py / daily_selfcheck.py   # 靜態與健康檢查
│   └── test_*.py               # 離線測試 (不打網路、不發 TG)
├── .github/workflows/          # market_open_alert / pre_market / morning_brief / ci_smoke
├── .streamlit/                 # config.toml、secrets.toml.example
└── requirements.txt
```

**import 慣例**：一律從套件匯入，例如

```python
from radar.notify import notifier
from radar.data import data_sources as ds
from radar.screeners.tw_screener import TWParams
```

新增模組時放進對應子資料夾即可；`scripts/` 底下的程式已自行把專案根目錄加進 `sys.path`。

---

## 部署

### 1. 用 git 同步到 GitHub

2026-11 起 GitHub 上的程式已是 `radar/` 套件版本（舊的根目錄平鋪 `.py` 已刪除）。
**請改用 git 同步，不要再用網頁「Add files via upload」**——上傳不會刪除舊檔，容易出現兩份程式碼或漏傳
`__init__.py`。建議直接拿 clone 下來的資料夾當工作目錄：

```bash
git clone https://github.com/a3659718/stock-dashboard.git
```

改完之後：

```bash
git add -A
git commit -m "說明這次改了什麼"
git push
```

`.gitignore` 已排除 secrets、狀態檔 (`monitor_state.json` 等)、log、虛擬環境與本機備份資料夾
（`_backup_*`、`Claude outputs`、`_workflow_updates`）。

> ⚠️ 不要把 `.streamlit/secrets.toml` 推上 GitHub（`.gitignore` 已擋）。任何 token 都只放在
> Streamlit Secrets / GitHub Actions Secrets，**不要寫進 README 或範例檔**。

### 2. Streamlit Cloud

<https://share.streamlit.io> → New app → 選 repo，Branch `main`，Main file path `app.py`。
Settings → Secrets 貼上（格式見 `.streamlit/secrets.toml.example`）：

```toml
FINMIND_TOKEN = "..."
TELEGRAM_BOT_TOKEN = "..."     # 向 @BotFather 取得
TELEGRAM_CHAT_ID = "..."
GEMINI_API_KEY = "..."
# FINNHUB_TOKEN = "..."         # 選填
# US_WATCHLIST = "AAPL,MSFT,NVDA"  # 選填
```

### 3. GitHub Actions Secrets

repo → Settings → Secrets and variables → Actions，新增：
`FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`GEMINI_API_KEY`、`FINNHUB_API_KEY` /
`FINNHUB_TOKEN`、`GCP_SERVICE_ACCOUNT_JSON`、`GOOGLE_SHEETS_ID`（去重狀態存 Google Sheets，缺了所有去重失效）、
`US_WATCHLIST`（選填）。

---

## 推播延遲（重要）

### 問題出在哪

實測 GitHub API 的 run 紀錄（2026-07 ~ 11）：**`schedule:` cron 在這個 repo 常態延遲 4 ~ 6.5 小時，且逐月變長**。

| 推播 | 排定（台北） | 實際開跑（台北，2026-11 上旬） |
|---|---|---|
| 晨報 | 08:02 | 13:54 ~ 14:39 |
| 盤前（日韓） | 08:17 / 08:33 | 14:21 ~ 15:19 |

每次 run 本身只跑 40 秒 ~ 5 分鐘，**延遲幾乎全部發生在 GitHub 的排程佇列**，不是程式慢。
GitHub 官方說明排程在高負載時可能延遲或跳過，調 cron 分鐘、加備援 cron 都救不了。

### 這次的處理

1. **外部準時觸發（真正的解法）**：由 cron-job.org 在準點呼叫 GitHub API 的 `workflow_dispatch`，
   這條路徑幾乎立即開跑。運算與 secrets 仍全部在 GitHub，cron-job.org 只負責「按鈴」，電腦不用開。
2. **遲到守衛 `radar/core/schedule_guard.py`**：GitHub cron 觸發的 run 若遲到超過該推播的容忍值
   （例如盤前 40 分、開盤 60 分），就**不推送已過時的內容**，避免收盤後才收到「盤前買進」。
   盤後總結、心跳、週報等晚到仍有價值的不擋；手動 / 外部觸發一律放行。
   要關閉：repo → Settings → Variables → 新增 `STALE_GUARD` = `0`。
3. **去重窗 5 小時 → 16 小時**：外部觸發準時推完後，6 小時後才到的 GitHub cron 不會再推一次。
4. **快取整個 Python 環境**：命中時跳過 `pip install`，每次 run 省 40 ~ 90 秒。
5. **Telegram 429 限流**：照 Telegram 回應的 `retry_after` 等待後重送（上限 30 秒），不再 1、2 秒就放棄。

### 設定 cron-job.org（一次，約 10 分鐘）

1. **GitHub Fine-grained PAT**：GitHub → Settings → Developer settings → Fine-grained tokens →
   Repository access 只選 `stock-dashboard` → Permissions：**Actions = Read and write**。
2. **cron-job.org**：免費註冊 → Settings → API → 建立 API key。
3. 在專案資料夾執行（PowerShell）：

```powershell
$env:CRONJOB_API_KEY="你的 cron-job.org API key"
$env:GH_DISPATCH_TOKEN="github_pat_..."
python scripts/setup_cronjob_org.py --repo a3659718/stock-dashboard --dry-run
python scripts/setup_cronjob_org.py --repo a3659718/stock-dashboard
```

4. 到 cron-job.org 對任一 job 按 **Test run**，GitHub Actions 頁面應立刻出現一個 `workflow_dispatch` run。

會建立 13 個 job（時刻表在 `scripts/punctual_slots.json`，比目標時間早約 3 分觸發）。
美股 job 直接用 `America/New_York` 時區，夏令 / 冬令自動換算。重跑腳本只會更新、不會重複建立；
`--delete` 可全部移除。GitHub 上原本的 cron 保留當備援。

> 備案：若不想用第三方服務，`scripts/register_punctual_tasks.ps1` 可註冊到 Windows 工作排程器
> （需要電腦開機）。

---

## 政策情境族群：2027 民主黨國會

**背景**：2026-11-03 期中選舉，依 AP / CBS 等初步結果，民主黨拿下**參眾兩院**；2027-01-03 新國會就任，
川普任期後半段為**分裂政府**。關稅、行政命令仍在白宮手上；新的減稅、去監管、預算刪減需要國會，難度大增；
國會監督聽證與預算 / 舉債上限攻防會變多。

清單在 `radar/analysis/policy_regime.py`，Dashboard「🧭 市場情緒 → 🏛️ 政策情境族群」可即時計算，
排程每週一 09:47 推一次週報（`policy_watch`，也可手動觸發）。

| 方向 | 族群 | 理由 | 美股代表 | 台股代表 |
|---|---|---|---|---|
| 🟢 受惠 | 健保 ACA / Medicaid | 延長 ACA 補貼、回補 Medicaid 是民主黨首要談判籌碼 | CNC MOH OSCR ELV HCA THC | — |
| 🟢 受惠 | 綠能 / 電網 | IRA 抵免不會再被砍、可能延長；電網為兩黨共識 | FSLR ENPH NEE PWR ETN GEV | 1513 1519 1503 9958 |
| 🟢 受惠 | 利率敏感 | 大規模財政刺激難過 → 殖利率下行 | XLU XLRE TLT IYR | 00679B 00687B |
| 🟢 受惠 | 必需消費 | 政策僵局 + 成長放緩時的防禦 | XLP PG KO WMT COST | 1216 2912 |
| 🟡 觀察 | 大型科技平台 | 基本面強，但反壟斷 / 隱私聽證增加 | AAPL MSFT GOOGL META AMZN | 2330 2317 3008 |
| 🟡 觀察 | 關稅 / 台灣出口鏈 | 國會可能限縮總統關稅權（利多），但白宮仍可另起關稅 | EWT SOXX | 2330 2454 2303 3711 |
| 🔴 受壓 | AI 資料中心 / 算力基建 | 多州新任州長主張資料中心暫停、電價與用水審查 | CRWV NBIS VST CEG SMCI VRT | 2382 3231 6669 2308 |
| 🔴 受壓 | 國防軍工 | 國防預算增幅需國會撥款，可能被卡或改分配 | LMT RTX NOC GD ITA | 2634 |
| 🔴 受壓 | 金融 / 去監管 | 去監管動能消失、監督加強、利差收窄 | XLF JPM GS SCHW KRE | — |
| 🔴 受壓 | 藥價 | 擴大 Medicare 議價是少數兩黨都想做的事 | PFE MRK BMY ABBV XLV | — |
| 🔴 受壓 | 傳統油氣 | 鑽探鬆綁類立法停滯（油價仍是主因） | XOM CVX OXY XLE | 6505 |

**怎麼讀「價格驗證」欄**：族群籃子近 20 日報酬減大盤（美股對 SPY、台股對 0050），
受惠族群跑贏 ≥2% =「市場已反映」，跑輸 ≥2% =「與假設相反」，其餘「尚未反映」。
它只描述價格是否支持假設，不預測未來。

**要盯的時間點**：2027-01 新國會就任 → 年度撥款 / 停擺期限 → 舉債上限 → ACA 補貼與 IRA 抵免談判 →
國會限縮關稅權法案 → 各州資料中心法規。

資料來源：[CBS 眾院結果](https://www.cbsnews.com/live-updates/house-election-results-2026/)、
[US News](https://www.usnews.com/news/national-news/articles/2026-midterm-election-results-maps-show-how-democrats-took-congress)、
[CNBC 投資影響](https://www.cnbc.com/2026/11/04/2026-midterms-investments.html)、
[Morgan Stanley](https://www.morganstanley.com/insights/articles/2026-election-market-impact-investor-guide)、
[Morningstar](https://www.morningstar.com/markets/us-midterms-how-markets-are-reacting-democrat-victory)。
結果仍屬初步，以 AP 最終認證為準。

---

## 本機開發與測試

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   # 填入 token
.venv\Scripts\streamlit run app.py
```

> Windows 中文環境請先設 `set PYTHONUTF8=1`，否則 pip 讀 `requirements.txt` 的中文註解會報 cp950 錯誤。

離線測試（不打網路、不發 Telegram）：

```bash
python scripts/smoke_test_full.py --static     # 語法 / import 符號 / 排程覆蓋
python scripts/smoke_test_full.py              # 實際匯入全部模組 + fmt_* 空輸入
python scripts/check_conventions.py            # 專案慣例 (claim 回滾、silent except …)
python scripts/test_tg_regressions.py
python scripts/test_schedule_guard.py
python scripts/test_policy_regime.py
python scripts/test_core_fixes.py              # state 回滾 / 批次 / 體積防線 / 詞界比對 等回歸
python scripts/test_delivery_fixes.py          # 條件觸發送達 / 盤中推播回滾 / 新聞 AI 等待上限
python scripts/test_pick_review.py
python scripts/test_tabs_integration.py
python -m unittest discover -s scripts -p test_dashboard_ui.py   # 真實 Streamlit AppTest, 約 2 分鐘
```

---

## 更新紀錄

### 2026-11-06
- **架構化**：98 個平鋪模組搬進 `radar/` 套件（core / data / notify / analysis / screeners / alerts / ui）。
- **推播延遲**：實測 GitHub cron 延遲 4~6.5 小時 → 新增 cron-job.org 準時觸發（`scripts/setup_cronjob_org.py`）、
  遲到守衛（不推過時的盤前 / 開盤內容）、去重窗 16 小時、快取 Python 環境、準時觸發獨立並發組。
- **除錯（三輪，約 85 項）**：推播遺失 / 重複（送達才登記、失敗回滾、部分送達、假日用排定日期）、
  state 回滾與 Sheets 單格上限、yfinance 併發拿錯股價、篩選器只掃代號最小的股票、升貼水訊號相反、
  自選股觸發從未送達、盤中合併推播失敗不回滾、Gemini 額度用完時急報永遠延後、
  00981A ETF 監控與股本資料從未成功解析等。完整清單與「仍待決定」項目見 `bug_audit_2026-11-06.md`。
- **政策情境族群**：2027 民主黨國會 / 分裂政府的受惠 / 受壓族群清單 + 相對大盤強弱驗證（每週一推播）。
- **安全**：移除 README / 範例檔中外洩的 Telegram Bot Token（**請到 @BotFather `/revoke` 換新 token**，
  舊 token 仍留在 git 歷史紀錄中）。

## 已知限制

> 稽核紀錄：`bug_audit_2026-11-06.md`（最新一輪，含「未修、待你決定」清單）、
> `隱藏bug稽核與修正_2026-10-22.md`、`bug_audit_2026-09-28.md`。

- FinMind 免費額度有每小時上限；首次全市場掃描約 30 秒，Streamlit cache 15 分鐘。
- yfinance 對上櫃 `.TWO` 回應較慢，強勢族群只取流動性前 200 檔。
- 去重狀態存 Google Sheets；未設定 `GCP_SERVICE_ACCOUNT_JSON` / `GOOGLE_SHEETS_ID` 時去重失效。
- 外部準時觸發沒設定前，GitHub cron 的時效性推播會被遲到守衛擋下（這是刻意的：寧可不推也不推過時內容）。
  不想要這個行為可設 `STALE_GUARD=0`。
