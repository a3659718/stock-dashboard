"""
scripts/run_upside_screen.py

執行 upside_screener 並列印結果. 用法:

    cd /path/to/stock_dashboard
    python -m scripts.run_upside_screen                  # 預設 200 檔
    python -m scripts.run_upside_screen --max 500        # 掃 500 檔
    python -m scripts.run_upside_screen --market twse    # 只看上市
    python -m scripts.run_upside_screen --json out.json  # 存 JSON 給後續用

輸出三類: 起漲初期 / 動能繼續 / 反轉型, 每類顯示前 N 檔.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 確保可以從 scripts/ 跑時也能 import 上層模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import upside_screener  # noqa: E402


def _progress(stage: str, pct: int):
    print(f"\r[{pct:3d}%] {stage:30s}", end="", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["all", "twse", "tpex"], default="all")
    ap.add_argument("--max", type=int, default=200, help="max stocks to scan")
    ap.add_argument("--per-category", type=int, default=5, help="show top N per category")
    ap.add_argument("--json", type=str, default=None, help="save full result to JSON")
    ap.add_argument("--include-etf", action="store_true")
    args = ap.parse_args()

    result = upside_screener.run_upside_screen(
        market=args.market,
        max_stocks=args.max,
        progress_cb=_progress,
        exclude_etf=not args.include_etf,
    )
    print()  # 換行
    meta = result.get("meta", {})
    print(f"\n== 掃描完成: {meta.get('scanned', 0)} / {meta.get('universe_size', 0)} 檔 ({meta.get('data_date')}) ==\n")
    print(f"   起漲初期 {meta.get('early_count', 0)} 檔 · 動能繼續 {meta.get('momentum_count', 0)} 檔 · 反轉型 {meta.get('reversal_count', 0)} 檔\n")

    for key in ("early_stage", "momentum", "reversal"):
        label = upside_screener.CATEGORY_LABEL.get(key, key)
        picks = (result.get(key) or [])[:args.per_category]
        print(f"\n──── 【{label}】Top {len(picks)} ────")
        if not picks:
            print("  (無符合標的)")
            continue
        for i, p in enumerate(picks, 1):
            lv = p.get("levels") or {}
            m = p.get("metrics") or {}
            print(f"\n  {i}. {p['stock_id']:6s} {p.get('name', ''):10s}  分數 {p.get('score')}/100  空間 ~{p.get('upside_pct')}%")
            print(f"     現價 {p.get('current')} · 進場 {lv.get('entry_low')}~{lv.get('entry_high')} · 目標 {lv.get('target')} · 停損 {lv.get('stop')} · R:R {lv.get('rr')}")
            print(f"     RSI {m.get('rsi')} · 量比 {m.get('vol_ratio_today')}x · 籌碼 {m.get('chip_health')}/100 ({m.get('chip_consensus', '?')}) · 距 52w 高 {m.get('pct_from_52w_high')}%")
            for r in p.get("reasons", [])[:4]:
                print(f"        ✓ {r}")
            for w in p.get("warnings", [])[:2]:
                print(f"        ⚠ {w}")

    if args.json:
        # 過濾掉不能序列化的部分 (numpy types)
        def _clean(o):
            if isinstance(o, dict):
                return {k: _clean(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [_clean(x) for x in o]
            try:
                json.dumps(o)
                return o
            except (TypeError, ValueError):
                return str(o)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(_clean(result), f, ensure_ascii=False, indent=2)
        print(f"\n✓ 已將完整結果寫入 {args.json}")


if __name__ == "__main__":
    main()
