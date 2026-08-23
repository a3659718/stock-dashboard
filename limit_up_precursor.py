"""
limit_up_precursor.py
台股「漲停前兆 / 潛伏吸籌」篩選器 — 目標是在股票「噴出之前」抓到, 而不是噴出後
才追高。這是使用者明確要求的新功能, 跟現有兩個相近模組刻意做出差異:

  - smart_money_stealth.py: 只掃「已經很熱的 top 3 題材」的成分股 → 題材必須先
    熱起來個股才會被掃到, 抓不到「還沒人注意、憑空噴出」的那種標的。
  - breakout_consolidation_alert.py: 抓「已經突破」的當下 (今日漲幅 ≥2% 且收盤價
    突破 20 日高), 本質上是「事後」確認訊號, 不是「之前」。

這裡刻意拿掉「題材必須先熱」這個 gate, 直接掃 sector_pulse.TW_THEMES 全部題材的
全部成分股 (~110 檔, 不篩已經熱的), 純粹用「還在安靜吸籌階段」的價量 + 籌碼結構
去找標的, 且明確排除「已經漲一段」的股票 (5 日漲幅 > 15% 或今日已噴出 > 6% 直接
剔除, 避免又變成「追高清單」)。

4 個訊號維度:
  1. 價格收斂 (VCP 型): 近 20 日窄幅整理 或 BB 帶寬收斂到近 60 日低檔百分位
     → 代表籌碼在沉澱, 還沒噴出。(必要, 1 選 1)
  2. 量價背離 (悄悄吸籌訊號): VPT (Volume Price Trend) 近 20 日斜率為正, 代表
     上漲日的量能比下跌日大 — 這不是「單日爆量」(那已經是噴出訊號、太晚), 而是
     多日累積下來「量能站在多方」的訊號; 或退一步接受「近 5 日均量比 20 日均量
     溫和放大 (≥1.05x) 但沒有真的噴出」。(必要, 1 選 1)
  3. 籌碼面 (加分項, 重用 chip_advanced.py 三合一分數): 千張大戶持股增加 / 借券
     回補 / 外資或主力券商買超。
  4. 體質偏好 (加分項): 小型股 (權值股很難漲停) + 週轉率偏高 (籌碼流動、資金
     推動效果大) + 近 5 日還沒漲 (不是追高)。

⚠️ 重要限制, 務必讓使用者知道:
  台股漲停很多時候是消息面 / 市場謠言 / 隔日沖等短線資金炒作推動, 這些完全無法
  從價量 + 籌碼歷史資料預測。這個篩選器能提高的是「抓到體質健康、正在被安靜
  布局的補漲股」的機率, 不是漲停的保證, 一定要分批進場 + 嚴設停損, 且高分不代表
  高勝算, 只代表「符合的訊號比較多」。

目前狀態: 僅供 dashboard 手動查看 (使用者要求先不自動推播 Telegram), fmt_*_msg
先寫好格式化函式備用, 之後若要推播只要在 market_open_alert.py 掛一個 category
呼叫即可。

API:
  scan_limit_up_precursor(top_n=8, max_themes=None) -> List[Dict]
  fmt_limit_up_precursor_msg(picks) -> str
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import data_sources as ds
import indicators as ind
import sector_pulse as sp

try:
    import streamlit as st  # type: ignore
except Exception:
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# 小型股門檻 (概略, 新台幣): 80 億以下算小型股, 300 億以下略加分
_SMALL_CAP_NTD = 8e9
_MID_CAP_NTD = 3e10


def _build_universe(max_themes: Optional[int] = None) -> List[Dict]:
    """掃 sector_pulse.TW_THEMES 全部題材的全部成分股 (刻意不篩「已經熱」的題材).

    同一檔股票若跨多個題材, 只留第一次出現的題材標籤 (避免重複掃描同一檔股票)。
    """
    name_map: Dict[str, str] = {}
    try:
        info = ds.get_taiwan_stock_info()
        if info is not None and not info.empty and "stock_name" in info.columns:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
    except Exception as e:
        print(f"[limit_up_precursor] stock name lookup fail (non-fatal): {e}", flush=True)

    themes = list(sp.TW_THEMES.keys())
    if max_themes:
        themes = themes[:max_themes]

    seen: set = set()
    universe: List[Dict] = []
    for theme in themes:
        for sid in sp.TW_THEMES.get(theme, []):
            sid = str(sid)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            universe.append({"stock_id": sid, "name": name_map.get(sid, ""), "theme": theme})
    return universe


def _evaluate_one(stock_id: str, name: str, theme: str,
                   shares_map: Dict[str, float]) -> Optional[Dict]:
    """單檔評估. 回 candidate dict 或 None (不符合任一必要條件)."""
    try:
        df = ds.fetch_yf_history(f"{stock_id}.TW", period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 40:
            df = ds.fetch_yf_history(f"{stock_id}.TWO", period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 40:
            return None

        c = df["Close"].astype(float).reset_index(drop=True)
        h = df["High"].astype(float).reset_index(drop=True)
        l = df["Low"].astype(float).reset_index(drop=True)
        v = df["Volume"].astype(float).reset_index(drop=True)
        cur = float(c.iloc[-1])
        if cur <= 0:
            return None
        prev = float(c.iloc[-2]) if len(c) >= 2 else cur
        today_pct = (cur / prev - 1) * 100 if prev > 0 else 0.0
        pct_5d = (cur / float(c.iloc[-6]) - 1) * 100 if len(c) >= 6 else 0.0
        pct_20d = (cur / float(c.iloc[-21]) - 1) * 100 if len(c) >= 21 else 0.0

        # 已經噴出的不要 — 這裡要抓「之前」, 不是追高
        if pct_5d > 15:
            return None
        if today_pct > 6.0:
            return None

        # === 1. 價格收斂 (VCP 型), 必要 (1 選 1) ===
        is_tight, tight_range = ind.is_tight_consolidation(c, lookback=20, max_range_pct=12.0)
        _, _, _, bb_w = ind.bollinger_bands(c, 20, 2.0)
        bb_squeeze = ind.is_bb_squeeze(bb_w, lookback=60, percentile=25.0)
        if not (is_tight or bb_squeeze):
            return None

        # === 2. 量價背離 / 溫和量增 (悄悄吸籌), 必要 (1 選 1) ===
        is_vpt_up = ind.vpt_uptrend(c, v, lookback=20)
        vol_expand_mild, vol_ratio = ind.volume_expansion(v, recent=5, base=20, ratio_threshold=1.05)
        vol_dry, vol_dry_ratio = ind.volume_dryup(v, recent=5, base=20, ratio_threshold=0.85)
        if not (is_vpt_up or (vol_expand_mild and today_pct <= 4.0)):
            return None

        # === 3. 籌碼面 (加分項, 重用 chip_advanced 三合一分數) ===
        chip: Dict = {}
        try:
            import chip_advanced as _ca
            chip = _ca.chip_advanced_score(stock_id) or {}
        except Exception as e:
            print(f"[limit_up_precursor] chip_advanced fail {stock_id}: {e}", flush=True)

        # === 4. 體質偏好: 小型股 + 週轉率 ===
        # 注意: fetch_shares_outstanding 回傳單位是「張」(1 張 = 1000 股),
        # yfinance 的 Volume 對台股是「股」, 換算時要乘 1000 才是同單位, 否則
        # market_cap / turnover 會差 1000 倍。
        market_cap_est = None
        turnover_pct = None
        shares_lots = shares_map.get(stock_id)
        if shares_lots and shares_lots > 0:
            shares_actual = shares_lots * 1000.0
            market_cap_est = cur * shares_actual
            today_vol_shares = float(v.iloc[-1])
            turnover_pct = today_vol_shares / shares_actual * 100

        # === 綜合分數 ===
        reasons: List[str] = []
        warnings: List[str] = []
        score = 0

        # BUG FIX: is_tight 和 bb_squeeze 本質上都是在量測「同一件事」(價格區間收斂),
        # 只是用兩種不同算法, 原本兩者都成立時會疊加 +12+10=+22 分, 等於同一個訊號
        # 被重複計分兩次, 虛灌總分。改成: 取較強的那個當主分數, 兩者都成立只再加一
        # 個小的「雙重確認」加分, 不是整個再疊加一次。
        if is_tight:
            reasons.append(f"近 20 日窄幅整理 (振幅 {tight_range}%)")
            score += 12
            if bb_squeeze:
                reasons.append("BB 帶寬同時收斂到近 60 日低檔 (雙重確認)")
                score += 4
        elif bb_squeeze:
            reasons.append("BB 帶寬收斂到近 60 日低檔 (變盤前兆)")
            score += 10
        if is_vpt_up:
            reasons.append("VPT 量價背離向上 (上漲日量能較大, 悄悄吸籌訊號)")
            score += 15
        elif vol_expand_mild:
            reasons.append(f"近 5 日均量較 20 日均量溫和放大 {vol_ratio}x (沒有噴出)")
            score += 6
        if vol_dry:
            reasons.append(f"量縮至 20 日均量 {vol_dry_ratio}x (籌碼沉澱中)")
            score += 5

        adv = chip.get("advanced_score", 0) if chip else 0
        if adv:
            # chip_advanced_score() 實際範圍是 -15~30 (非文件寫的 0-30), *0.6 後
            # 真正的值域是 -9~18 — 原本的 max(-10, min(20, ...)) clamp 永遠不會生效
            # (數字對不上), 這裡改成跟實際值域一致, 避免誤導性的「假邊界」。
            chip_score = max(-9, min(18, int(adv * 0.6)))
            score += chip_score
            reasons.extend((chip.get("reasons") or [])[:2])
            warnings.extend((chip.get("warnings") or [])[:2])

        if market_cap_est is not None:
            if market_cap_est < _SMALL_CAP_NTD:
                reasons.append(f"小型股 (市值約 {market_cap_est/1e8:.0f} 億)")
                score += 8
            elif market_cap_est < _MID_CAP_NTD:
                score += 3
        if turnover_pct is not None and turnover_pct >= 1.0:
            reasons.append(f"今日週轉率 {turnover_pct:.2f}%")
            score += 5

        if pct_5d < 0:
            reasons.append(f"近 5 日 {pct_5d:+.1f}% (還沒漲, 非追高)")
            score += 5

        # BUG FIX: 門檻 20 分對「非重複計分後」的分數來說太低 — 光靠兩個必要 gate
        # 裡最弱的組合 (bb_squeeze 10 分 + 溫和量增 6 分 = 16) 再隨便加上小型股 (+8)
        # 就過關了, 等於門檻形同虛設, 沒有真的把「訊號薄弱」的標的擋掉。拉高到 28,
        # 讓純粹「兩個必要條件都壓線 + 體質加分」不足以過關, 需要籌碼面或量縮或
        # 5 日未漲等額外佐證才會真正進入清單。
        if score < 28:
            return None

        levels = ind.atr_based_levels(h, l, c, stop_atr_mult=1.5, target_atr_mult=3.0) or {}

        return {
            "stock_id": stock_id, "name": name, "theme": theme,
            "current": round(cur, 2), "today_pct": round(today_pct, 2),
            "pct_5d": round(pct_5d, 2), "pct_20d": round(pct_20d, 2),
            "tight_range": tight_range, "bb_squeeze": bb_squeeze,
            "is_vpt_up": is_vpt_up, "vol_ratio_5v20": vol_ratio,
            "market_cap_est": round(market_cap_est, 0) if market_cap_est is not None else None,
            "turnover_pct": round(turnover_pct, 2) if turnover_pct is not None else None,
            "advanced_score": adv,
            "reasons": reasons, "warnings": warnings, "score": score,
            "entry_low": levels.get("entry_low"), "entry_high": levels.get("entry_high"),
            "stop_loss": levels.get("stop"), "target": levels.get("target"),
            "rr": levels.get("rr"),
        }
    except Exception as e:
        print(f"[limit_up_precursor] {stock_id} eval failed: {e}", flush=True)
        return None


def scan_limit_up_precursor(top_n: int = 8, max_themes: Optional[int] = None) -> List[Dict]:
    """掃全部題材成分股 (~110 檔), 找「漲停前兆 / 潛伏吸籌」標的.

    任何一步失敗都 graceful return [], 不炸掉呼叫端 (dashboard 按鈕)。
    """
    try:
        universe = _build_universe(max_themes=max_themes)
        if not universe:
            return []

        # 股本一次批次抓 (fetch_shares_outstanding 是 cache_data 依 tuple 參數為 key,
        # 若在 loop 內對每檔各呼叫一次, 每次 tuple 只有 1 個元素, 會變成 ~110 次各自
        # 未命中 cache 的獨立呼叫 — 這裡改成整批一次抓, 大幅減少 FinMind API 次數).
        stock_ids = tuple(sorted({u["stock_id"] for u in universe}))
        try:
            shares_map = ds.fetch_shares_outstanding(stock_ids)
        except Exception as e:
            print(f"[limit_up_precursor] shares_outstanding batch fetch fail (non-fatal): {e}", flush=True)
            shares_map = {}

        candidates: List[Dict] = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {
                ex.submit(_evaluate_one, u["stock_id"], u["name"], u["theme"], shares_map): u["stock_id"]
                for u in universe
            }
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as e:
                    print(f"[limit_up_precursor] {futs[fut]} eval raised: {e}", flush=True)
                    r = None
                if r:
                    candidates.append(r)

        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        return candidates[:top_n]
    except Exception as e:
        print(f"[limit_up_precursor] scan failed: {e}", flush=True)
        return []


def fmt_limit_up_precursor_msg(picks: List[Dict]) -> str:
    """格式化 TG 訊息 (HTML). 目前尚未被任何推播呼叫 (使用者要求先只放 dashboard),
    先寫好備用 — 之後要推播的話只要在 market_open_alert.py 呼叫這個函式 +
    notifier.send_message(..., category="limit_up_precursor") 即可。
    """
    import html as _html
    import datetime as _dt

    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    def _f2(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "—"

    if not picks:
        return ""
    now_tpe = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=8)).strftime("%H:%M")
    lines = [
        f"🎯 <b>漲停前兆 / 潛伏吸籌 Top {len(picks)}</b> · {now_tpe} TPE",
        "<i>(價格收斂 + 量價背離向上 + 籌碼/小型股加分, 且尚未大漲)</i>",
        "",
    ]
    for i, p in enumerate(picks, 1):
        sid = _esc(p.get("stock_id", ""))
        name = _esc(p.get("name", ""))
        cur = p.get("current")
        tp = p.get("today_pct")
        theme = _esc(p.get("theme", ""))
        score = p.get("score", 0)
        tp_str = f"({tp:+.2f}%)" if isinstance(tp, (int, float)) else ""
        line1 = f"{i}. <code>{sid}</code> {name} · {_f2(cur)} {tp_str}".rstrip()
        lines.append(line1)
        if theme:
            lines.append(f"   🏷 題材: {theme} · 分數 {score}")
        for r in p.get("reasons", [])[:4]:
            lines.append(f"   ✓ {_esc(r)}")
        for w in p.get("warnings", [])[:2]:
            lines.append(f"   ⚠️ {_esc(w)}")
        lines.append(
            f"   📍 進場 {_f2(p.get('entry_low'))}-{_f2(p.get('entry_high'))} · "
            f"停損 {_f2(p.get('stop_loss'))} · 目標 {_f2(p.get('target'))} · "
            f"R:R {_f2(p.get('rr'))}"
        )
        lines.append("")

    lines.append(
        "<i>⚠️ 台股漲停常受消息面/短線資金主導, 純技術+籌碼資料無法保證抓到, "
        "本清單只是提高「安靜吸籌補漲股」的命中機率, 務必分批進場 + 嚴設停損。</i>"
    )
    return "\n".join(lines)
