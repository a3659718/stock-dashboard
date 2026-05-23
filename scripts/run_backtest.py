"""
scripts/run_backtest.py

在本機跑 upside_screener 完整回測 (含 FinMind 籌碼資料).

用法:
    cd /path/to/stock_dashboard
    # 純技術面 (快, 不耗 FinMind quota)
    python -m scripts.run_backtest --universe top30 --days 180 --tech-only

    # 含籌碼 (慢, 但更接近實盤)
    python -m scripts.run_backtest --universe top30 --days 180

    # 自訂 universe (逗號分隔)
    python -m scripts.run_backtest --universe 2330,2317,2454,2603 --days 90

    # 輸出 xlsx 報告
    python -m scripts.run_backtest --universe top30 --days 180 --out reports/bt.xlsx

預設 universe "top30" = 大型權值 + 中型成長 + 部分小型題材的 30 檔組合.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pandas as pd

# 支援從 scripts/ 直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest_upside as bt  # noqa
import chip_analyzer  # noqa
import data_sources as ds  # noqa


# 預設 30 檔代表性 universe
TOP30 = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2412": "中華電",
    "2308": "台達電", "2382": "廣達", "2891": "中信金", "2882": "國泰金",
    "2881": "富邦金", "1301": "台塑",
    "3008": "大立光", "2379": "瑞昱", "3231": "緯創", "2376": "技嘉",
    "2603": "長榮", "2609": "陽明", "3661": "世芯-KY", "6669": "緯穎",
    "3017": "奇鋐", "2618": "長榮航",
    "3711": "日月光投控", "8069": "元太", "4904": "遠傳",
    "1216": "統一", "1227": "佳格", "2207": "和泰車",
    "2354": "鴻準", "1402": "遠東新", "2912": "統一超",
    "6531": "愛普",
}


def fetch_prices(stock_ids, days: int) -> dict:
    """從 FinMind 抓 daily."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=days + 80)).strftime("%Y-%m-%d")  # 留 80 天緩衝給 52w / MA60
    end = today.strftime("%Y-%m-%d")
    print(f"抓 {len(stock_ids)} 檔 daily ({start} ~ {end})…", flush=True)
    prices = {}
    for i, sid in enumerate(stock_ids, 1):
        try:
            df = ds._finmind_get_one("TaiwanStockPrice", sid, start, end)
            if df.empty:
                continue
            if "max" in df.columns and "high" not in df.columns:
                df = df.rename(columns={"max": "high", "min": "low"})
            df["date"] = pd.to_datetime(df["date"])
            df = df.rename(columns={"Trading_Volume": "volume"})
            df = df[["date", "open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
            prices[sid] = df
            print(f"  [{i}/{len(stock_ids)}] ✓ {sid}: {len(df)} 筆", flush=True)
        except Exception as e:
            print(f"  [{i}/{len(stock_ids)}] ✗ {sid}: {e}", flush=True)
    return prices


def make_chip_provider(stock_ids):
    """回傳一個 chip_provider(sid, as_of_date) -> dict.

    為了效率, 預先抓每檔股票 1 年的籌碼資料, 然後 query 時切片.
    """
    print(f"預載 {len(stock_ids)} 檔籌碼資料 (歷史 90 天)…", flush=True)
    chip_cache = {}
    today = dt.date.today()
    for i, sid in enumerate(stock_ids, 1):
        try:
            # 抓 360 天的法人 / 融資融券 (回測期間需要不同日期切片)
            start = (today - dt.timedelta(days=365)).strftime("%Y-%m-%d")
            end = today.strftime("%Y-%m-%d")
            inst = ds._finmind_get_one("TaiwanStockInstitutionalInvestorsBuySell", sid, start, end)
            margin = ds._finmind_get_one("TaiwanStockMarginPurchaseShortSale", sid, start, end)
            if not inst.empty:
                inst["date"] = pd.to_datetime(inst["date"])
                inst["net"] = inst["buy"].astype(float) - inst["sell"].astype(float)
            if not margin.empty:
                margin["date"] = pd.to_datetime(margin["date"])
            chip_cache[sid] = {"inst": inst, "margin": margin}
            if i % 5 == 0:
                print(f"  [{i}/{len(stock_ids)}] ", end="", flush=True)
        except Exception as e:
            print(f"\n  ✗ {sid}: {e}", flush=True)
    print(" done.", flush=True)

    def provider(sid: str, as_of: pd.Timestamp) -> dict:
        cache = chip_cache.get(sid)
        if not cache:
            return None
        inst = cache["inst"]
        margin = cache["margin"]
        # 切到 as_of 為止的 30 天視窗
        cutoff = as_of - dt.timedelta(days=35)
        inst_slice = inst[(inst["date"] <= as_of) & (inst["date"] >= cutoff)] if not inst.empty else inst
        margin_slice = margin[(margin["date"] <= as_of) & (margin["date"] >= cutoff)] if not margin.empty else margin

        # 計算 inst summary
        inst_summary = {}
        if not inst_slice.empty and "name" in inst_slice.columns:
            for nm, g in inst_slice.groupby("name"):
                g = g.sort_values("date")
                nets = g["net"].tolist()
                inst_summary[nm] = {
                    "30d_total": int(sum(nets)),
                    "5d_total": int(sum(nets[-5:])),
                    "today": int(nets[-1]) if nets else 0,
                    "consecutive_days": chip_analyzer._count_consecutive(nets),
                }

        # margin summary
        margin_summary = {}
        if not margin_slice.empty:
            last = margin_slice.iloc[-1]
            first = margin_slice.iloc[0]
            if "MarginPurchaseTodayBalance" in margin_slice.columns:
                cur_m = int(last.get("MarginPurchaseTodayBalance", 0) or 0)
                old_m = int(first.get("MarginPurchaseTodayBalance", 0) or 0)
                margin_summary["融資餘額"] = cur_m
                margin_summary["融資30日變化%"] = round((cur_m / old_m - 1) * 100, 1) if old_m > 0 else None
            for col in ["ShortSaleTodayBalance", "ShortSaleAfterBalance"]:
                if col in margin_slice.columns:
                    cur_s = int(last.get(col, 0) or 0)
                    old_s = int(first.get(col, 0) or 0)
                    margin_summary["融券餘額"] = cur_s
                    margin_summary["融券30日變化%"] = round((cur_s / old_s - 1) * 100, 1) if old_s > 0 else None
                    break

        # 用 chip_analyzer 的 helper 算共識 / 健康度
        consensus = chip_analyzer.calc_chip_consensus(inst_summary)
        # 沒 price_summary 就只看 inst + margin 算分
        chip_health = chip_analyzer.calc_chip_health_score(inst_summary, margin_summary, {})

        it = inst_summary.get("Investment_Trust") or {}
        fi = inst_summary.get("Foreign_Investor") or {}

        return {
            "chip_health": chip_health,
            "chip_consensus": consensus["direction"],
            "chip_consensus_score": consensus["score"],
            "it_consecutive": it.get("consecutive_days", 0),
            "fi_consecutive": fi.get("consecutive_days", 0),
            "it_5d_net": it.get("5d_total", 0),
            "fi_5d_net": fi.get("5d_total", 0),
        }

    return provider


def fetch_benchmark(days: int) -> pd.DataFrame:
    """抓 TWII (^TWII via yfinance, FinMind 也可)."""
    try:
        import yfinance as yf
        today = dt.date.today()
        start = today - dt.timedelta(days=days + 50)
        df = yf.download("^TWII", start=start, end=today, progress=False, auto_adjust=False, threads=False)
        if df.empty:
            return pd.DataFrame()
        df = df.reset_index()
        df.columns = [c if isinstance(c, str) else c[0] for c in df.columns]
        df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                  "Low": "low", "Close": "close", "Volume": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", "open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
    except Exception as e:
        print(f"benchmark fetch failed: {e}", flush=True)
        return pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="top30",
                     help="'top30' 或逗號分隔的 stock_ids")
    ap.add_argument("--days", type=int, default=180, help="回測 lookback 天數")
    ap.add_argument("--rescan-every", type=int, default=5)
    ap.add_argument("--hold-days", type=str, default="5,10,20")
    ap.add_argument("--tech-only", action="store_true", help="不抓籌碼 (純技術面)")
    ap.add_argument("--out", type=str, default=None, help="輸出 xlsx / csv 路徑")
    args = ap.parse_args()

    # universe
    if args.universe == "top30":
        ids = list(TOP30.keys())
        names = TOP30
    else:
        ids = [x.strip() for x in args.universe.split(",") if x.strip()]
        names = {sid: sid for sid in ids}

    # 抓資料
    prices = fetch_prices(ids, args.days)
    if not prices:
        print("無資料, 結束.", flush=True)
        return

    chip_provider = None if args.tech_only else make_chip_provider(list(prices.keys()))
    benchmark = fetch_benchmark(args.days)

    # 期間
    all_dates = sorted(set().union(*[set(df["date"].tolist()) for df in prices.values()]))
    start = all_dates[60].strftime("%Y-%m-%d") if len(all_dates) > 60 else all_dates[0].strftime("%Y-%m-%d")
    end = all_dates[-25].strftime("%Y-%m-%d") if len(all_dates) > 25 else all_dates[-1].strftime("%Y-%m-%d")
    hold_days = [int(x) for x in args.hold_days.split(",")]

    print(f"\n回測 {start} ~ {end}  ·  rescan_every={args.rescan_every}  ·  hold={hold_days}  ·  chip={chip_provider is not None}\n", flush=True)
    result = bt.backtest(
        prices=prices, names=names,
        start_date=start, end_date=end,
        hold_days=hold_days,
        chip_provider=chip_provider,
        benchmark_df=benchmark if not benchmark.empty else None,
        rescan_every=args.rescan_every,
        verbose=True,
    )

    print("\n===== Summary =====")
    print(result["summary"].to_string(index=False))
    print(f"\nBench returns: {result['benchmark_returns']}")
    print(f"Meta: {result['meta']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if str(out_path).endswith(".xlsx"):
            with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
                result["picks_df"].to_excel(xw, sheet_name="picks", index=False)
                result["summary"].to_excel(xw, sheet_name="summary", index=False)
        else:
            result["picks_df"].to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\n✓ 已輸出: {out_path}")


if __name__ == "__main__":
    main()
