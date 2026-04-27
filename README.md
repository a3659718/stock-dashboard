# 台美股雷達 Dashboard

Streamlit 網站，手機 / 桌機都能直接打開使用，含台股盤後四條件篩選、即時強勢族群、美股 Top 5 推薦、Fear & Greed 市場情緒，並可一鍵推送到 Telegram。

## 功能

### 🇹🇼 台股篩選 (上市 + 上櫃)
- 突破月線 (MA20) 或季線 (MA60)
- 今日量為 5 日均量的 5–10 倍
- 融券今日餘額 較前日增加 ≥ 50 張
- 投信近 30 日「首次」買超

可切換「全部命中 / 至少 2 / 3 / 4 項」檢視，並顯示各條件原始明細。
若當日盤後資料尚未落地，畫面會出現警告提示。

### 🚀 強勢族群 (即時)
台股流動性 Top 200 個股當下漲跌幅，依產業分組計算族群熱度與龍頭。

### 🇺🇸 美股 Top 5
- 技術面：突破 MA20 / MA50、量比
- 動能：日 / 5d / 20d 漲幅、相對 SPY 強度 (RS)
- 題材：自動抓取 Yahoo Finance 新聞，比對 AI / 半導體 / 雲端 / 能源 / Fed 等熱門題材
- 市場情緒：CNN Fear & Greed Index + S&P SPDR 板塊輪動

### 🧭 市場情緒
Fear & Greed 指數、板塊輪動表、近 24h 市場新聞。指數異常 (≤25 / ≥75) 自動推送提示。

### ✈️ Telegram 推播
1. **手動**：每個分頁都有「送到 Telegram」按鈕
2. **命中即發**：勾選後，篩選有結果就自動推
3. **異常觸發**：強勢族群形成、F&G 異常時主動推送

---

## 部署到 Streamlit Cloud (免費，5 分鐘有公開網址)

### 步驟 1 — Push 到 GitHub
```bash
cd C:\Users\user\Desktop\Project\stock_dashboard
git init
git add .
git commit -m "Initial dashboard"
gh repo create stock-dashboard --public --source=. --push
# 或自己在 github.com 建 repo 後：
# git remote add origin https://github.com/<你的帳號>/stock-dashboard.git
# git push -u origin main
```

> ⚠️ 不要把 `.streamlit/secrets.toml` 推上 GitHub。`.gitignore` 已經幫你擋掉。

### 步驟 2 — 連到 Streamlit Cloud
1. 開 https://share.streamlit.io 用 GitHub 帳號登入
2. 點 **New app** → 選剛剛的 repo
3. Branch: `main`，Main file path: `app.py`
4. 點 **Deploy**

### 步驟 3 — 設定 Secrets
進入剛部署好的 app → **Settings → Secrets**，貼上：
```toml
FINMIND_TOKEN = "你的 FinMind token (可重用 BreakMA.py 裡的那組)"
TELEGRAM_BOT_TOKEN = "7863908871:AAERo-fkKAT6P51erdqK8NmYINfMLnuQ7eQ"
TELEGRAM_CHAT_ID = "987155792"
# US_WATCHLIST = "AAPL,MSFT,NVDA"   # 選填
# FINNHUB_TOKEN = "..."              # 選填
```
按 **Save**，App 會自動重啟。

### 步驟 4 — 取得網址
部署完成後上方會顯示類似：
```
https://<你的帳號>-stock-dashboard-app-xxxxx.streamlit.app
```
**這就是您手機可以直接打開的網址。** 在手機 Safari / Chrome 直接點開即可，下拉重新整理就重跑掃描。

> 💡 把網址加到手機主畫面 (Safari → 分享 → 加入主畫面 / Chrome → 加到主畫面)，看起來就像 App。

---

## 本機測試

```bash
cd C:\Users\user\Desktop\Project\stock_dashboard
python -m pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
# 編輯 secrets.toml 填入 token
streamlit run app.py
```
瀏覽器會自動開啟 http://localhost:8501。

---

## 檔案結構

```
stock_dashboard/
├── app.py                  # Streamlit 主程式 (4 個分頁)
├── data_sources.py         # FinMind / yfinance / F&G / 新聞
├── tw_screener.py          # 台股四條件篩選邏輯
├── sector_pulse.py         # 即時強勢族群
├── us_screener.py          # 美股 Top 5 評分模型
├── notifier.py             # Telegram 推送 + 訊息模板
├── requirements.txt
├── .streamlit/
│   ├── config.toml         # 主題設定
│   └── secrets.toml.example
├── .gitignore
└── README.md
```

---

## 已知限制

- **FinMind 免費 tier 有 hourly call quota**，第一次掃描全市場時建議耐心等待 ~30s。Streamlit cache TTL = 15 分鐘，重複按重新整理不會重打 API。
- yfinance 對 .TWO（上櫃）部分代碼回應較慢，「強勢族群」分頁採前 200 大流動性個股以控時。
- 「投信首買」邏輯：累積 30d 投信淨買 ≤ 0 且今日 > 0；採 FinMind `Investment_Trust` 欄位。
- 美股新聞題材抓 Yahoo Finance；如要更精準可申請 Finnhub 免費 token，填入 `FINNHUB_TOKEN` 即啟用。
- **本網站僅供研究參考，非投資建議。**
