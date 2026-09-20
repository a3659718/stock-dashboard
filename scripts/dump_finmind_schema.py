#!/usr/bin/env python3
"""
dump_finmind_schema.py — 一次把外部 API 的「真實欄位名與單位」印出來 (2026-09-20)

為什麼有這支
------------
稽核程式碼時有一整類 bug 是靜態讀不出來的: 我們只能看到程式「假設」外部 API 回什麼,
看不到它「實際」回什麼。這個專案目前至少有這些地方卡在這上面:

  short_interest_alert.py:47-50   借券餘額欄位名 (SecuritiesLendingBalance / balance /
                                  ShortSaleBalance ... 一串 or 鏈, 全沒對到就靜默回 0,
                                  外觀跟「今天沒有空單」一模一樣)
  short_interest_alert.py:121-124 融資融券欄位名 + today[0] 只取單列 (若是 long-format
                                  就會抓錯類別)
  chip_advanced.py:235-237        券商分點 buy/sell 的單位是股還是張 (門檻寫 1000/500
                                  並印「張」, 若實際是股就差 1000 倍)
  institutional_positioning.py:74 三大法人 buy/sell 的單位 (目前註「元」再 /1e8 標成
                                  「億元」, 但 chip_analyzer.py:144 說是「股」)
  data_sources.py:341-346         資產負債表「股本」的 type 值有哪些 (關鍵字比對可能
                                  同時命中普通股/特別股/預收股本)
  stock_deep_analyzer.py:549      月營收的 last_year_revenue 欄位是否真的存在
  stock_deep_analyzer.py:531      財報 date 是「期末日」還是「公告日」(決定 to_period("Q")
                                  對不對得上去年同季)

跑一次就能把上面全部變成確定的事實。

用法
----
    FINMIND_TOKEN=xxx python scripts/dump_finmind_schema.py
    FINMIND_TOKEN=xxx python scripts/dump_finmind_schema.py --stock 2330 --out schema.txt

輸出: 每個 dataset 的欄位清單、dtype、前 3 列原始值, 以及數值欄的量級提示
(量級是判斷「股 vs 張 vs 元」最快的依據 —— 例如台積電單日外資買賣超若是
 1e7 量級就是「股」, 1e4 量級才是「張」)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (dataset, 是否需要 data_id, 說明, 特別想確認什麼)
DATASETS = [
    ("TaiwanStockPrice", True, "日線 (成交量/成交值)",
     "Trading_Volume 是股還是張? 有沒有 Trading_money?"),
    ("TaiwanStockInstitutionalInvestorsBuySell", True, "三大法人買賣超",
     "buy/sell 的單位 — 股 or 元? (institutional_positioning 當成元, chip_analyzer 當成股)"),
    ("TaiwanStockTotalInstitutionalInvestors", False, "三大法人合計 (全市場)",
     "這個才是金額嗎? 欄位名是什麼?"),
    ("TaiwanStockSecuritiesLending", True, "借券",
     "餘額欄位到底叫什麼 (short_interest_alert 猜了 5 個名字)"),
    ("TaiwanStockTotalMarginPurchaseShortSale", False, "融資融券 (全市場)",
     "是 long-format 還是 wide-format? today[0] 取單列對不對?"),
    ("TaiwanStockMarginPurchaseShortSale", True, "融資融券 (個股)",
     "欄位名 + 單位"),
    ("TaiwanStockTradingDailyReport", True, "券商分點",
     "buy/sell 單位 — 股 or 張? (chip_advanced 門檻寫 1000/500 並印「張」)"),
    ("TaiwanStockBalanceSheet", True, "資產負債表",
     "type 欄有哪些值含「股本」? (fetch_shares_outstanding 用關鍵字比對)"),
    ("TaiwanStockFinancialStatements", True, "財報 (EPS)",
     "date 是期末日還是公告日? EPS 的 type 值是什麼?"),
    ("TaiwanStockMonthRevenue", True, "月營收",
     "有沒有 last_year_revenue 欄位? (stock_deep_analyzer:549 讀它)"),
    ("TaiwanStockShareholding", True, "集保股權分散",
     "ForeignInvestmentShareRatio 存在嗎?"),
    ("TaiwanStockInfo", False, "上市櫃清單", "有沒有可以拿來排流動性的欄位?"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", default="2330", help="用來試探的個股代號 (預設 2330)")
    ap.add_argument("--days", type=int, default=30, help="往回抓幾個日曆日 (預設 30)")
    ap.add_argument("--out", default="", help="同時寫到檔案")
    args = ap.parse_args()

    try:
        import pandas as pd
        import data_sources as ds
    except Exception as e:
        print(f"匯入失敗 (請先 pip install -r requirements.txt): {e}")
        return 2

    if not ds.get_finmind_token():
        print("找不到 FINMIND_TOKEN。請用 FINMIND_TOKEN=xxx python scripts/dump_finmind_schema.py")
        return 2

    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    start = (today - dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    buf = []

    def emit(line: str = "") -> None:
        print(line, flush=True)
        buf.append(line)

    emit(f"FinMind schema dump — {today}  (stock={args.stock}, {start} ~ {end})")
    emit("=" * 78)

    for name, need_id, desc, question in DATASETS:
        emit()
        emit(f"### {name}  — {desc}")
        emit(f"    想確認: {question}")
        try:
            params = {"start_date": start, "end_date": end}
            if need_id:
                params["data_id"] = args.stock
            df = ds._finmind_get(name, **params)
        except Exception as e:
            emit(f"    ❌ 抓取失敗: {e}")
            continue
        if df is None or df.empty:
            emit("    ⚠️ 回傳空 (可能是 token 等級不足 / 這個區間沒資料)")
            continue

        emit(f"    rows={len(df)}  columns={list(df.columns)}")
        # dtype + 量級 — 判斷單位最快的依據
        for c in df.columns:
            try:
                sv = pd.to_numeric(df[c], errors="coerce").dropna()
                if sv.empty:
                    vals = df[c].astype(str).unique()[:6]
                    emit(f"      {c:<34} (非數值) 相異值範例: {list(vals)}")
                else:
                    mx = float(sv.abs().max())
                    mag = f"1e{len(str(int(mx))) - 1}" if mx >= 1 else "<1"
                    emit(f"      {c:<34} min={sv.min():,.2f}  max={sv.max():,.2f}  量級≈{mag}")
            except Exception:
                pass
        emit("    前 3 列:")
        for _, row in df.head(3).iterrows():
            emit(f"      {dict(row)}")

    emit()
    emit("=" * 78)
    emit("判讀提示:")
    emit("  · 台積電單日法人買賣超: 量級 1e7 = 『股』, 1e4 = 『張』, 1e9 = 『元』")
    emit("  · 券商分點 buy/sell 同理 —— chip_advanced.py 的門檻寫 1000/500 並印「張」,")
    emit("    若量級是 1e6~1e7 就代表那兩條加分條件實際上是『買 1 張就 +8 分』。")
    emit("  · long-format 的判斷: 同一個 date 出現多列、且有一個欄位在描述類別。")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(buf))
        print(f"\n已寫出 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
