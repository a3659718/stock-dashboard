"""
scripts/run_us_upside.py

Run us_upside_screener for US explosive stocks.

Usage:
    python -m scripts.run_us_upside                       # default universe
    python -m scripts.run_us_upside --per-category 3
    python -m scripts.run_us_upside --universe NVDA,SMCI,RKLB
    python -m scripts.run_us_upside --no-cache
    python -m scripts.run_us_upside --no-themes           # skip narrative
    python -m scripts.run_us_upside --json out.json

5 categories: breakout / acceleration / squeeze_setup / revival_setup / narrative_leader
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
                     help="comma-separated symbols, default DEFAULT_US_UNIVERSE")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-themes", action="store_true",
                     help="skip theme / news / sector / earnings (technical-only, faster)")
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
    print()
    print("== US upside scan ==")
    print("   Scanned " + str(meta.get("scanned", 0)) + " / " + str(meta.get("universe_size", 0)) +
          ", themes " + str(meta.get("themes_loaded", 0)) +
          ", gemini targets " + str(meta.get("gemini_targets_loaded", 0)))
    print("   breakout " + str(meta.get("breakout_count", 0)) +
          " | acceleration " + str(meta.get("acceleration_count", 0)) +
          " | squeeze " + str(meta.get("squeeze_count", 0)) +
          " | revival " + str(meta.get("revival_count", 0)) +
          " | narrative " + str(meta.get("narrative_count", 0)))
    print()

    for key in ("breakout", "acceleration", "squeeze_setup", "revival_setup", "narrative_leader"):
        label = ups.CATEGORY_LABEL_US[key]
        picks = (result.get(key) or [])[:args.per_category]
        print()
        print("---- [" + label + "] Top " + str(len(picks)) + " ----")
        if not picks:
            print("  (no matches)")
            continue
        for i, p in enumerate(picks, 1):
            lv = p.get("levels") or {}
            m = p.get("metrics") or {}
            cur = p["current"]
            theme_str = ""
            if m.get("theme_score") is not None:
                theme_str = " | theme " + str(m["theme_score"]) + " (" + str(m.get("theme_strength", "?")) + ")"
                if p.get("theme_multiplier") and p["theme_multiplier"] != 1.0:
                    theme_str += " x" + str(p["theme_multiplier"])
            print()
            print("  " + str(i) + ". " + p["symbol"].ljust(6) +
                  "  score " + str(p["score"]) + "/100  upside ~" + str(p["upside_pct"]) + "%" + theme_str)
            tags = m.get("narrative_tags") or []
            if tags:
                print("     tags: " + ", ".join(tags[:3]))
            print("     $" + str(cur) + " | entry $" + str(lv.get("entry_low")) + "-$" + str(lv.get("entry_high")) +
                  " | short tgt $" + str(lv.get("target")) + " | stop $" + str(lv.get("stop")) +
                  " | R:R " + str(lv.get("rr")))
            mid_targets = []
            if lv.get("target_fib_127"):
                mid_targets.append("Fib1.27 $" + str(lv["target_fib_127"]))
            if lv.get("target_fib_162"):
                mid_targets.append("Fib1.62 $" + str(lv["target_fib_162"]))
            if lv.get("target_fib_262"):
                mid_targets.append("Fib2.62 $" + str(lv["target_fib_262"]))
            if lv.get("target_measured_move"):
                mid_targets.append("MM $" + str(lv["target_measured_move"]))
            if mid_targets:
                print("     mid targets: " + " | ".join(mid_targets))
            if lv.get("target_fundamental_3m"):
                t3 = lv["target_fundamental_3m"]
                t6 = lv.get("target_fundamental_6m", 0)
                bull = lv.get("target_fundamental_bull", 0)
                conf = lv.get("fundamental_confidence", 0)
                pct3 = (t3 / cur - 1) * 100 if cur else 0
                pct6 = (t6 / cur - 1) * 100 if cur and t6 else 0
                print("     long (Gemini): 3m $" + str(round(t3, 1)) + " (" + ("{:+.0f}".format(pct3)) + "%)" +
                      " | 6m $" + str(round(t6, 1)) + " (" + ("{:+.0f}".format(pct6)) + "%)" +
                      " | bull $" + str(round(bull, 1)) + " | conf " + str(conf))
                if lv.get("fundamental_reasoning"):
                    print("        why: " + lv["fundamental_reasoning"][:120])
            print("     RSI " + str(m.get("rsi")) + " | RVOL " + str(m.get("rvol")) + "x" +
                  " | from ATH " + str(m.get("pct_from_ath")) + "%" +
                  " | RS " + str(m.get("rs_vs_spy")))
            for r in p.get("reasons", [])[:4]:
                print("        + " + r)
            for w in p.get("warnings", [])[:2]:
                print("        ! " + w)

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
        print()
        print("JSON saved: " + args.json)


if __name__ == "__main__":
    main()
