"""
scripts/run_us_upside.py

執行 us_upside_screener (美股潛在爆發股) 並列印結果.

用法:
    cd /path/to/stock_dashboard
    python -m scripts.run_us_upside                       # 預設 universe
    python -m scripts.run_us_upside --per-category 3      # 每類顯示 3 檔
    python -m scripts.run_us_upside --universe AAPL,NVDA,TSLA,SMCI  # 自訂
    python -m scripts.run_us_upside --no-cache            # 強制重算 (不用 cache)
    python -m scripts.run_us_upside --json out.json       # 存 JSON

三類:
  - breakout: 52w 突破 / Stage 2 + tight base
  - acceleration: 動能加速 (5d > 10d > 20d 速率遞增)
  - squeeze_setup: 壓縮整理待噴
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import us_upside_screener as ups  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-category", type=int, default=5)
    ap.add_argument("--universe", type=str, default=None,
                     help="逗號分隔的 symbols, 預設用 DEFAULT_US_UNIVERSE")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-themes", action="store_true",
                     help="跳過題材 / 新聞 / 板塊 / 財報分析 (純技術面, 較快)")
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    universe = None
    if args.universe:
        universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]

    result = ups.run_us_upside_screen(
        top_n_per_category=args.per_category,
        universe=universe,
        use_cache=not args.no_cache,
        with_themes=not args.no_themes,
    )

    meta = result.get("meta", {})
    print(f"\n== 美股 upside scan ==")
    print(f"   掃描 {meta.get('scanned', 0)} / {meta.get('universe_size', 0)} 檔, 題材已載入 {meta.get('themes_loaded', 0)} 檔")
    print(f"   突破 {meta.get('breakout_count', 0)} · 加速 {meta.get('acceleration_count', 0)} · 壓縮 {meta.get('squeeze_count', 0)} · 題材領跑 {meta.get('narrative_count', 0)}\n")

    for key in ("breakout", "acceleration", "squeeze_setup", "narrative_leader"):
        label = ups.CATEGORY_LABEL_US[key]
        picks = (result.get(key) or [])[:args.per_category]
        print(f"\n──── 【{label}】Top {len(picks)} ────")
        if not picks:
            print("  (無符合標的)")
            continue
        for i, p in enumerate(picks, 1):
            lv = p.get("levels") or {}
            m = p.get("metrics") or {}
            theme_str = ""
            if m.get("theme_score") is not None:
                theme_str = f" · 題材 {m['theme_score']:.0f} ({m.get('theme_strength', '?')})"
                if p.get("theme_multiplier") and p["theme_multiplier"] != 1.0:
                    theme_str += f" ×{p['theme_multiplier']}"
            print(f"\n  {i}. {p['symbol']:6s}  分數 {p['score']}/100  空間 ~{p['upside_pct']}%{theme_str}")
            tags = m.get("narrative_tags") or []
            if tags:
                print(f"     題材標籤: {', '.join(tags[:3])}")
            print(f"     現價 ${p['current']} · 進場 ${lv.get('entry_low')}~${lv.get('entry_high')} · 目標 ${lv.get('target')} · 停損 ${lv.get('stop')} · R:R {lv.get('rr')}")
            print(f"     RSI {m.get('rsi')} · RVOL {m.get('rvol')}x · 距 ATH {m.get('pct_from_ath')}% · RS {m.get('rs_vs_spy')}")
            for r in p.get("reasons", [])[:4]:
                print(f"        ✓ {r}")
            for w in p.get("warnings", [])[:2]:
                print(f"        ⚠ {w}")

    if args.json:
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
        print(f"\n✓ JSON 已存於 {args.json}")


if __name__ == "__main__":
    main()
