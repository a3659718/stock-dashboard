# 架構優化計畫(中長期)

這份文件涵蓋四個「會動到大量現有程式、需要實機測試」的重構。
每一項都拆成可獨立交付、可回滾、可測試的階段,**不要一次全改**。
建議順序:先做「安全網」(已完成的 smoke test + 自檢),再依風險由低到高逐項進行。

> 已完成的前置(這次):
> - `safe_fmt.py` — 共用安全格式化
> - `scripts/smoke_test_full.py` — 靜態完整性 + 排程覆蓋 + fmt_* 空輸入
> - `scripts/daily_selfcheck.py` — 每日健康自檢 + 主動告警
>
> 動任何下面的重構前,先把 smoke test 接成 pre-push / CI gate,當作回歸防線。

---

## A. 統一安全格式化(低風險,先做)

**問題**:`f"{x.get('k'):.2f}"` 套到 None/NaN/字串會崩,散落在多個檔案。

**做法**:全面改用 `safe_fmt.fmt_num / fmt_pct / fmt_price`。

**階段**
1. 先在「會崩到使用者」的檔案(`notifier.py`、`app.py`)逐函式替換;每換一個跑一次 smoke test。
2. 再擴及各 `*_alert.py`。
3. 加一條 lint/grep gate:CI 內 `grep -nE "get\([^)]*\)\s*:\s*[.,+0-9]*f"` 找殘留的危險寫法。

**風險**:低(純顯示層)。**測試**:smoke test [3] 已覆蓋空輸入;人工比對幾則推播格式。

---

## B. 資料源韌性層(中風險)

**問題**:yfinance/FinMind 限流是反覆的痛;retry/cache 邏輯散落各處;失敗常變成「安靜的空資料」。

**做法**:集中成一個 `data_gateway` 層。
- 單一進入點包住所有外部抓取,內建:速率限制(token bucket)、指數退避、空結果不進長 cache(已在 `fetch_intraday_metrics` 做過局部修)、失敗計數上報 `system_health`。
- 可選備援來源(e.g. yfinance 失敗 → Stooq / FinMind 替補)。

**階段**
1. 抽出 `data_gateway.fetch_history(symbol, ...)` 包住現有 `ds.fetch_yf_history`,先不改行為,只加「失敗計數 + 空結果短 TTL」。
2. 把各 caller 逐步改指向 gateway。
3. 再加速率限制與備援。

**風險**:中(動到資料正確性)。**測試**:對拍 gateway 與舊路徑的輸出一致;故意斷網測退避與告警。

---

## C. 狀態層併發(中高風險)

**現況**:`monitor_state` 整包 JSON。這次已做:
- 移除 GSheet `clear()` 空窗;
- `_flush_state` 加 shallow top-level merge(不同 key 不再互蓋)。

**仍未解**:同一個 key 的併發更新(兩個 runner 同秒都 `+1`)仍 last-writer-wins。

**做法(擇一)**
1. **關鍵計數器原子化**:把 `push_cap` / `push_dedup` 的計數從「load→改→save 整包」改成 GSheet 單格的原子操作(或檔案鎖 + 單一 sub-key 寫入)。範圍最小、效益最大。
2. **真正的跨行程鎖**:所有 `save_monitor_state` 走一個分散式鎖(GSheet 上一個 lock 欄位 + 過期時間)。範圍大、要小心死鎖。

**建議**:先做 1(只動 push_cap/push_dedup),不要一上來做 2。

**風險**:中高(動到去重/上限的正確性)。**測試**:寫一支併發模擬(多 thread 同時 claim/consume),斷言計數不漏。

---

## D. 排程 dispatch 簡化(中高風險)

**問題**:`market_open_alert.yml` 用「UTC 分鐘分桶」反推 market,drift 邊界脆弱(morning_recap 那次就踩到)。

**做法**:改成「每個推播 = 一個明確的 job」,時間直接決定 mode,不再靠分鐘反推。
- 維持單一 workflow,但 `Determine market` 改成查一張「cron→mode」對照表(以 cron 表達式本身為 key),而非用當下分鐘猜。
- 或拆成數個小 workflow,各自固定 mode。

**階段**
1. 先把 smoke test [4]「排程覆蓋」接進 CI(已做),確保改動不會 emit 出沒 handler 的 mode。
2. 把分鐘分桶換成對照表,逐 slot 驗證(可用 workflow_dispatch 手動觸發每個 mode 對拍)。
3. 觀察一個完整交易週,比對推播時間/類型。

**風險**:中高(動到所有排程推播)。**測試**:對每個 cron 時間點手動 dispatch 一次,確認 routed mode 正確。

---

## E. `app.py` 拆分(中風險,純結構)

**問題**:單檔 ~174KB,難維護。

**做法**:按分頁抽成 `ui/` 子模組(`ui/tab_pulse.py`、`ui/tab_us.py`…),`app.py` 只留 tab 容器 + 共用狀態。

**階段**:一次只搬一個分頁,搬完跑 app 確認該分頁正常再搬下一個。純剪貼、不改邏輯。

**風險**:中(import 循環/共用狀態要理清)。**測試**:每搬一頁,手動點過該頁所有按鈕。

---

## 總結:建議執行順序

1. ✅(已完成)smoke test + 每日自檢 → **接成 CI/pre-push gate**
2. A 安全格式化(低風險,快速見效)
3. C-1 關鍵計數器原子化(去重/上限正確性)
4. B 資料源韌性層
5. D 排程簡化
6. E app.py 拆分

每一項都「小步、可回滾、跑 smoke test」。要動哪一項時告訴我,我可以陪你一個階段一個階段做,而不是盲改。
