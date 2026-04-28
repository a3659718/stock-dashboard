"""
app.py — 台美股盤前盤後雷達網站
========================================================
* 在手機 / 桌機都能直接使用
* 重新整理會重新計算 (有 cache)
* Telegram 推播：手動 / 命中即發 / 異常觸發三種模式
"""

from __future__ import annotations

import datetime as dt
from typing import List

import pandas as pd
import streamlit as st

import ai_analyzer
import data_sources as ds
import news_picks
import notifier
import sector_pulse
import stock_analyzer
import tw_screener
import us_screener


# ---------------------------------------------------------------------------
# Page 設定 (必須先呼叫)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="台美股雷達",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="auto",
)

# 行動裝置友善 CSS
st.markdown(
    """
    <style>
      .block-container { padding-top: 1rem; padding-bottom: 4rem; }
      .stMetric { background: rgba(255,255,255,0.04); padding: 8px; border-radius: 8px; }
      .pill { display:inline-block; padding:2px 8px; border-radius:999px;
              background:#1f6feb22; color:#1f6feb; font-size:12px; margin:2px; }
      .pill.warn { background:#d2940022; color:#d29400; }
      .pill.bad  { background:#d3000022; color:#d30000; }
      @media (max-width: 640px) {
        .block-container { padding-left: 0.6rem; padding-right: 0.6rem; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Sidebar (參數 / 設定狀態)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ 設定")
    st.caption(f"今天: {dt.date.today().strftime('%Y-%m-%d')}")

    fm_ok = bool(ds.get_finmind_token())
    tg_ok = notifier.is_configured()
    fm_pkg = ds.finmind_available()
    st.markdown(
        f"- FinMind 套件: {'✅' if fm_pkg else '❌ (未安裝/Python 版本不相容)'}  \n"
        f"- FinMind Token: {'✅' if fm_ok else '❌'}  \n"
        f"- Telegram: {'✅' if tg_ok else '❌'}"
    )

    with st.expander("🔧 Secrets 偵錯"):
        keys = ds.list_secret_keys()
        st.caption("st.secrets 偵測到的 key：")
        if keys:
            st.code(", ".join(keys))
        else:
            st.code("(空)")
        st.caption(
            "若顯示空，代表 Streamlit Cloud Settings > Secrets 還沒儲存成功。"
            "格式必須是 TOML，例如：\n"
            "FINMIND_TOKEN = \"eyJ0eXAi...\""
        )

    if not fm_pkg:
        st.error(
            "❗ FinMind 套件沒裝起來，最常見原因是 Python 版本是 3.14（套件還沒支援）。"
            "請到 App ⋮ → Settings → 把 Python version 改成 3.11 後 Save。"
        )
    elif not (fm_ok and tg_ok):
        st.info(
            "尚未設定 secrets？請參考 README，到 Streamlit Cloud > App > Settings > Secrets 貼上："
            "`FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` (可選 `FINNHUB_TOKEN`、`US_WATCHLIST`)"
        )

    st.divider()
    st.subheader("台股篩選參數")
    market_choice = st.radio(
        "市場別", options=["all", "twse", "tpex"],
        format_func=lambda x: {"all": "上市+上櫃", "twse": "上市", "tpex": "上櫃"}[x],
        horizontal=True,
    )
    vol_min = st.slider("量比下限 (倍)", 3, 10, 5)
    vol_max = st.slider("量比上限 (倍)", vol_min, 20, max(vol_min, 10))
    short_inc = st.number_input("融券增加門檻 (張)", min_value=10, value=50, step=10)
    max_stocks = st.slider(
        "掃描檔數 (FinMind 免費版上限 ~200)", 50, 500, 200, step=50,
        help="超過 200 檔可能會撞到每小時 API 配額"
    )
    min_avg_vol = st.number_input("日均量門檻 (張)", min_value=0, value=500, step=100,
                                   help="排除冷門股；強勢股 (今日漲幅>5%) 例外保留")
    min_price = st.number_input("股價下限 (元)", min_value=0.0, value=5.0, step=1.0,
                                 help="排除雞蛋水餃股")
    exclude_etf = st.checkbox("排除 ETF / 全額交割 / TDR", value=True)

    st.divider()
    st.subheader("📋 自選股 Watchlist")
    watchlist_raw = st.text_area(
        "代號 (逗號或換行分隔)", value="",
        placeholder="2330, 2454\n3017",
        height=80,
    )
    auto_alert_watchlist = st.checkbox("命中即推 Telegram (限 watchlist)", value=True)

    st.divider()
    st.subheader("自動 Telegram 通知")
    auto_send_on_hit = st.checkbox("全市場命中條件即自動發送", value=False)
    auto_send_on_alert = st.checkbox("強勢族群 / 恐慌指數異常時推播", value=True)


tw_params = tw_screener.TWParams(
    vol_min_ratio=float(vol_min),
    vol_max_ratio=float(vol_max),
    short_inc_lots=int(short_inc),
    max_stocks=int(max_stocks),
    min_avg_volume=int(min_avg_vol),
    min_price=float(min_price),
    exclude_etf=bool(exclude_etf),
)


def parse_watchlist(s: str) -> list:
    import re
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[,\s]+", s) if p.strip()]
    return [p for p in parts if p.isdigit() and 4 <= len(p) <= 6]


watchlist = parse_watchlist(watchlist_raw)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_tw, tab_pulse, tab_growth, tab_stock, tab_us, tab_mood = st.tabs(
    ["🇹🇼 台股篩選", "🚀 強勢族群", "🌱 成長動能 Top10", "🔍 個股分析", "🇺🇸 美股 Top 5", "🧭 市場情緒"]
)


# =============================================================================
# Tab 1 — 台股篩選
# =============================================================================
with tab_tw:
    st.subheader("台股盤後條件篩選")
    st.caption("勾選要使用的條件，按重新整理開始掃描。每個條件背後會抓對應的資料集。")

    # 台股市場情緒 banner
    tw_pulse = ds.fetch_tw_market_pulse()
    if tw_pulse and tw_pulse.get("score") is not None:
        s = tw_pulse["score"]
        rating_zh = tw_pulse.get("rating_zh", "")
        color = ("#A32D2D" if s <= 25 else "#D85A30" if s <= 45 else
                 "#888780" if s <= 55 else "#3B6D11" if s <= 75 else "#791F1F")
        st.markdown(
            f"<div style='padding:8px 12px; background:rgba(127,127,127,0.08); "
            f"border-left:4px solid {color}; border-radius:6px; margin-bottom:8px'>"
            f"🇹🇼 <b>台股市場情緒指數: {s} ({rating_zh})</b> · "
            f"加權 {tw_pulse['raw'].get('TWII'):,.0f} · "
            f"5日 {tw_pulse['raw'].get('5日%')}% · "
            f"距 MA60 {tw_pulse['raw'].get('距 MA60 %')}%"
            f"</div>",
            unsafe_allow_html=True,
        )
        # 異常推播
        if auto_send_on_alert and notifier.is_configured():
            alert = notifier.fmt_tw_pulse_alert(tw_pulse)
            if alert:
                fp = ("tw_pulse", s)
                if st.session_state.get("_tw_pulse_alert") != fp:
                    ok, _ = notifier.send_message(alert)
                    if ok:
                        st.session_state["_tw_pulse_alert"] = fp

    # 8 個條件 checkbox（分兩列）
    cond_keys = list(tw_screener.CONDITION_LABELS.keys())
    default_on = {"break_ma", "volume_burst", "short_increase", "invtrust_first_buy"}
    cb_cols = st.columns(4)
    enabled_conditions = []
    for i, k in enumerate(cond_keys):
        with cb_cols[i % 4]:
            if st.checkbox(tw_screener.CONDITION_LABELS[k], value=(k in default_on), key=f"cb_{k}"):
                enabled_conditions.append(k)

    if not enabled_conditions:
        st.warning("請至少勾選一個條件。")

    colA, colB, colC = st.columns([1, 1, 1])
    with colA:
        run_btn = st.button("🔄 重新整理 / 開始掃描", use_container_width=True, type="primary",
                            disabled=not enabled_conditions)
    with colB:
        send_tg_btn = st.button("✈️ Send to TG", use_container_width=True,
                                disabled=not notifier.is_configured(),
                                key="send_tw_tg")
    with colC:
        min_hits_label = st.selectbox(
            "顯示需符合幾項",
            options=["全部勾選都中", "至少 1 項", "至少 2 項", "至少 3 項", "至少 4 項"],
            index=1,
        )

    if run_btn:
        try:
            with st.spinner(f"掃描中…(共 {params_max := int(max_stocks)} 檔 × {len(enabled_conditions)} 條件)"):
                res = tw_screener.run_all_screens(
                    market=market_choice, params=tw_params, enabled=enabled_conditions
                )
            st.session_state["tw_result"] = res
        except Exception as e:
            st.error(f"掃描失敗：{e}")

    res = st.session_state.get("tw_result")
    if not res:
        st.info("按上方「重新整理」開始掃描。")
    else:
        latest = res["latest_date"]
        latest_str = pd.Timestamp(latest).strftime("%Y-%m-%d") if latest is not None else "N/A"
        if not res["ready"]:
            st.warning(
                f"⚠️ 今日({dt.date.today().strftime('%Y-%m-%d')})盤後資料尚未更新，"
                f"目前以最新一個交易日 {latest_str} 的資料計算。"
                "建議台股 14:30 之後再查詢。"
            )
        else:
            st.success(f"資料日期: {latest_str} ✅")

        # 各條件命中數
        results_dict = res.get("results", {})
        cols = st.columns(min(4, max(1, len(results_dict))))
        for i, (k, df) in enumerate(results_dict.items()):
            cols[i % len(cols)].metric(tw_screener.CONDITION_LABELS[k], len(df))

        combined: pd.DataFrame = res["combined"]
        if combined.empty:
            st.info("今日無任何條件命中。")
        else:
            # 顯示模式 → hit_count 篩選
            n_enabled = len(res["enabled"])
            if min_hits_label == "全部勾選都中":
                min_hits = n_enabled
            elif min_hits_label == "至少 1 項":
                min_hits = 1
            elif min_hits_label == "至少 2 項":
                min_hits = 2
            elif min_hits_label == "至少 3 項":
                min_hits = 3
            else:
                min_hits = 4
            view = combined[combined["hit_count"] >= min_hits]

            st.markdown(f"**共 {len(view)} 檔符合 (顯示條件: {min_hits_label})**")
            # 動態挑可顯示的欄位 (有的條件沒打開就沒對應數值)
            base_cols = ["stock_id", "stock_name", "market", "hit_count", "hits_label"]
            extra_cols = ["現價", "今日%", "今日量", "量比", "投信今日(張)", "投信5日(張)", "投本比%"]
            show_cols = base_cols + [c for c in extra_cols if c in view.columns]
            display = view[show_cols].rename(columns={
                "stock_id": "代號", "stock_name": "名稱", "market": "市場",
                "hit_count": "命中數", "hits_label": "命中條件",
            })
            st.dataframe(display, use_container_width=True, hide_index=True)

            # ===== Watchlist 命中即推送 =====
            if watchlist and auto_alert_watchlist and notifier.is_configured():
                hit_in_wl = view[view["stock_id"].isin(watchlist)]
                fp_wl = (latest_str, tuple(hit_in_wl["stock_id"].tolist()))
                if not hit_in_wl.empty and st.session_state.get("_wl_last_alert") != fp_wl:
                    msgs = []
                    for _, row in hit_in_wl.iterrows():
                        msgs.append(notifier.fmt_watchlist_alert(
                            row["stock_id"], row.get("stock_name", ""),
                            row.get("hit", []), latest_str
                        ))
                    ok, info = notifier.send_message("\n\n".join(msgs))
                    if ok:
                        st.session_state["_wl_last_alert"] = fp_wl
                        st.toast(f"✈️ Watchlist 命中 {len(hit_in_wl)} 檔已推送", icon="🔔")

            with st.expander("📁 各條件原始明細"):
                tab_keys = list(results_dict.keys())
                if tab_keys:
                    sub_tabs = st.tabs([tw_screener.CONDITION_LABELS[k] for k in tab_keys])
                    for st_tab, k in zip(sub_tabs, tab_keys):
                        with st_tab:
                            st.dataframe(results_dict[k], use_container_width=True, hide_index=True)

            # Telegram 自動推播
            if auto_send_on_hit and notifier.is_configured() and not view.empty:
                fingerprint = (latest_str, tuple(view["stock_id"].head(40)))
                if st.session_state.get("_tw_last_sent") != fingerprint:
                    msg = notifier.fmt_tw_combined(view, latest_str, market_label="自動推播")
                    ok, info = notifier.send_message(msg)
                    if ok:
                        st.session_state["_tw_last_sent"] = fingerprint
                        st.toast("已自動推送 Telegram", icon="✈️")

            if send_tg_btn:
                msg = notifier.fmt_tw_combined(view, latest_str, market_label="台股篩選")
                ok, info = notifier.send_message(msg)
                if ok:
                    st.success("已送出至 Telegram ✅")
                else:
                    st.error(f"送出失敗: {info}")


# =============================================================================
# Tab 2 — 強勢族群
# =============================================================================
with tab_pulse:
    st.subheader("台股強勢族群 / 熱門題材")
    st.caption(
        "兩種視角：① 證交所產業分類；② 熱門題材股池 (無人機 / AI 伺服器 / 低軌衛星 / 重電 / 散熱…)。"
        "盤中即時資料來自 yfinance。"
    )

    cA, cB, cC, cD = st.columns([1, 1, 1, 1])
    with cA:
        pulse_btn = st.button("🔄 產業分類", use_container_width=True, type="primary")
    with cB:
        theme_btn = st.button("🔥 熱門題材", use_container_width=True, type="primary")
    with cC:
        stealth_btn = st.button("🌱 潛伏題材股", use_container_width=True, type="primary")
    with cD:
        send_pulse_tg = st.button("✈️ Send to TG", use_container_width=True,
                                  disabled=not notifier.is_configured(),
                                  key="send_pulse_tg")

    if pulse_btn:
        try:
            with st.spinner("計算族群熱度中…"):
                st.session_state["pulse"] = sector_pulse.compute_strong_sectors(top_n=200)
        except Exception as e:
            st.error(f"族群分析失敗：{e}")

    if theme_btn:
        try:
            with st.spinner("計算熱門題材中…"):
                st.session_state["themes"] = sector_pulse.compute_hot_themes()
        except Exception as e:
            st.error(f"題材分析失敗：{e}")

    if stealth_btn:
        try:
            with st.spinner("挖掘潛伏題材股…"):
                st.session_state["stealth"] = sector_pulse.find_stealth_followers(top_themes=5)
        except Exception as e:
            st.error(f"潛伏股分析失敗：{e}")

    # === 潛伏題材股 ===
    stealth_data = st.session_state.get("stealth", {})
    stealth_df = stealth_data.get("stealth")
    if stealth_df is not None and not stealth_df.empty:
        st.markdown("### 🌱 潛伏題材股 (族群熱、本身還沒大漲、有量能跡象)")
        st.dataframe(stealth_df, use_container_width=True, hide_index=True)
        send_stealth_tg = st.button(
            "✈️ Send to TG", use_container_width=True, key="send_stealth_tg",
            disabled=not notifier.is_configured()
        )
        if send_stealth_tg and notifier.is_configured():
            ok, info = notifier.send_message(
                notifier.fmt_stealth_picks(stealth_df, stealth_data.get("hot_themes"))
            )
            if ok:
                st.success("已送出 ✅")
            else:
                st.error(info)

    # === 熱門題材區塊 ===
    themes_data = st.session_state.get("themes", {})
    themes_df = themes_data.get("themes")
    leaders_map = themes_data.get("leaders") or {}
    if themes_df is not None and not themes_df.empty:
        st.markdown("### 🔥 熱門題材熱度排行")
        st.dataframe(themes_df, use_container_width=True, hide_index=True)

        st.markdown("### 🎯 題材龍頭 Top 5 強勢股 (含盤中資訊)")
        for theme in themes_df["題材"].head(8):
            df = leaders_map.get(theme)
            if df is None or df.empty:
                continue
            with st.expander(f"📌 {theme}", expanded=(themes_df.iloc[0]["題材"] == theme)):
                show = df[["stock_id", "stock_name", "現價", "今日%", "振幅%", "量比", "5日%"]].copy()
                show = show.rename(columns={"stock_id": "代號", "stock_name": "名稱"})
                st.dataframe(show, use_container_width=True, hide_index=True)

    # === 證交所產業分類區塊 ===
    pulse = st.session_state.get("pulse", {})
    sectors = pulse.get("sectors")
    leaders = pulse.get("leaders")
    if sectors is not None and not sectors.empty:
        st.markdown("### 🏢 證交所產業分類 Top 5")
        first_col = sectors.columns[0]
        top5 = sectors.head(5).copy()
        st.dataframe(
            top5.rename(columns={first_col: "產業", "avg_change": "平均%", "median_change": "中位%",
                                 "up_count": "上漲家數", "n": "樣本數", "up_ratio": "上漲比率"}),
            use_container_width=True, hide_index=True,
        )
        if leaders is not None and not leaders.empty:
            with st.expander("各產業龍頭 (前 5 名 + 盤中資訊)"):
                show_cols = [c for c in ["industry_category", "stock_id", "stock_name",
                                          "現價", "今日%", "振幅%", "量比", "5日%"]
                             if c in leaders.columns]
                st.dataframe(leaders[show_cols], use_container_width=True, hide_index=True)

        # 異常觸發推播
        if auto_send_on_alert and notifier.is_configured():
            top1 = sectors.iloc[0]
            avg = float(top1["avg_change"])
            if avg >= 1.5:
                fingerprint = ("strong_sector", top1[first_col], round(avg, 2))
                if st.session_state.get("_pulse_last_alert") != fingerprint:
                    ok, info = notifier.send_message(notifier.fmt_strong_sectors(sectors))
                    if ok:
                        st.session_state["_pulse_last_alert"] = fingerprint
                        st.toast("已推送強勢族群通知", icon="🚀")

    if (sectors is None or sectors.empty) and (themes_df is None or themes_df.empty):
        st.info("按上方按鈕開始分析 (盤前/休市時 yfinance 資料可能尚未更新)。")

    if send_pulse_tg and (sectors is not None and not sectors.empty):
        ok, info = notifier.send_message(notifier.fmt_strong_sectors(sectors))
        if ok:
            st.success("已送出 ✅")
        else:
            st.error(info)


# =============================================================================
# Tab 3 — 美股 Top 5
# =============================================================================
# =============================================================================
# Tab — 成長動能 Top 10 台股 (消息面 + K 線健康度)
# =============================================================================
with tab_growth:
    st.subheader("🌱 消息面 + 成長動能 Top 10 台股")
    st.caption(
        "從熱門題材股池 (約 100+ 檔) 評估每檔的 K 線健康度：站上月線 / 起漲位 / KD 黃交 / MACD 翻紅 / 量能配合 …"
        "排除已大漲(5d>20%) 與跌破月線者。"
    )
    cG1, cG2 = st.columns([1, 1])
    with cG1:
        growth_btn = st.button("🔄 更新成長動能榜", use_container_width=True, type="primary")
    with cG2:
        send_growth_tg = st.button("✈️ Send to TG", use_container_width=True, key="send_growth",
                                    disabled=not notifier.is_configured())

    if growth_btn:
        try:
            with st.spinner("評估題材股 K 線健康度中…約 30 秒"):
                st.session_state["growth"] = news_picks.run_news_growth_picks(top_n=10)
        except Exception as e:
            st.error(f"成長動能分析失敗：{e}")

    growth = st.session_state.get("growth", {})
    picks = growth.get("picks")
    if picks is None or picks.empty:
        st.info("按上方按鈕開始分析。")
    else:
        st.dataframe(picks, use_container_width=True, hide_index=True)
        if send_growth_tg and notifier.is_configured():
            ok, info = notifier.send_message(notifier.fmt_growth_picks(picks))
            if ok:
                st.success("已送出 ✅")
            else:
                st.error(info)


# =============================================================================
# Tab — 個股深度分析
# =============================================================================
with tab_stock:
    st.subheader("🔍 個股深度分析 (台股 / 美股)")
    cS1, cS2, cS3 = st.columns([3, 1, 1])
    with cS1:
        sid_input = st.text_input(
            "輸入股票代號",
            value="",
            placeholder="台股: 2330  /  美股: NVDA, AAPL, TSLA",
            help="4 位數字 = 台股；含字母 = 美股 (用 yfinance)",
        )
    with cS2:
        analyze_btn = st.button("🔍 分析", use_container_width=True, type="primary")
    with cS3:
        ai_btn = st.button("🤖 AI 深度", use_container_width=True,
                           disabled=not ai_analyzer.gemini_available(),
                           help=("需先在 secrets 設 GEMINI_API_KEY 並重啟"
                                 if not ai_analyzer.gemini_available() else
                                 "依基本面/技術面/籌碼面/新聞綜合分析"))

    # 收到任何按鈕都要執行 (analyze 或 ai)，但 ai 需要先有 analyze 的資料
    do_fetch = (analyze_btn or ai_btn) and sid_input.strip()
    if do_fetch:
        sid = sid_input.strip()
        try:
            with st.spinner(f"抓取 {sid} 完整資料中…"):
                full = stock_analyzer.fetch_stock_full(sid)
            if full["daily"].empty:
                st.error(f"找不到 {sid} 的日線資料 (代號可能錯誤或為下市股)。")
                full = None
            else:
                ind = stock_analyzer.compute_indicators(full["daily"])
                hits = stock_analyzer.evaluate_conditions(sid, full, tw_params)
                score, reasons = stock_analyzer.overall_score(hits)
                # 暫存以便 AI 使用
                st.session_state["last_stock"] = {
                    "sid": sid, "full": full, "ind": ind, "hits": hits, "score": score, "reasons": reasons
                }
        except Exception as e:
            st.error(f"分析失敗：{e}")
            full = None

    last_stock = st.session_state.get("last_stock")
    if last_stock and (analyze_btn or ai_btn):
        full = last_stock["full"]; ind = last_stock["ind"]
        hits = last_stock["hits"]; score = last_stock["score"]; reasons = last_stock["reasons"]
        sid = last_stock["sid"]

        # 標題列
        cN1, cN2, cN3, cN4 = st.columns(4)
        cN1.metric("代號", full["stock_id"])
        cN2.metric("名稱", full["name"] or "—")
        cN3.metric("市場", full["market"] or "—")
        cN4.metric("產業", full["industry"] or "—")

        # 核心指標
        last = ind.iloc[-1]
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("收盤", f"{last['close']:.2f}")
        if "MA20" in ind.columns and pd.notna(last["MA20"]):
            m2.metric("MA20", f"{last['MA20']:.2f}")
        if "MA60" in ind.columns and pd.notna(last["MA60"]):
            m3.metric("MA60", f"{last['MA60']:.2f}")
        if "K" in ind.columns and pd.notna(last["K"]):
            m4.metric("K / D", f"{last['K']:.0f} / {last['D']:.0f}")
        if "Hist" in ind.columns and pd.notna(last["Hist"]):
            m5.metric("MACD Hist", f"{last['Hist']:.3f}")

        # 評分
        st.markdown(f"### 綜合評分: **{score} / 10**")
        if reasons:
            st.success("命中條件: " + " · ".join(reasons))
        else:
            st.info("目前沒有命中任何條件。")

        # K 線圖
        with st.expander("📈 6 個月 K 線 + MA + 量能", expanded=True):
            chart_df = ind.tail(120).set_index("date")[["close", "MA20", "MA60"]]
            st.line_chart(chart_df)
            st.bar_chart(ind.tail(120).set_index("date")["Trading_Volume"])

        with st.expander("📊 KD 與 MACD"):
            if "K" in ind.columns:
                st.line_chart(ind.tail(120).set_index("date")[["K", "D"]])
            if "DIF" in ind.columns:
                st.line_chart(ind.tail(120).set_index("date")[["DIF", "MACD", "Hist"]])

        # 三大法人 / 融資融券 (僅台股)
        is_us = full.get("is_us", False)
        if not is_us:
            with st.expander("🏛️ 三大法人 30 日累計"):
                summary = stock_analyzer.institutional_summary(full["inst"])
                if summary.empty:
                    st.info("無法人資料")
                else:
                    st.dataframe(summary, use_container_width=True, hide_index=True)

            with st.expander("💰 融資融券摘要"):
                ms = stock_analyzer.margin_summary(full["margin"])
                if not ms:
                    st.info("無融資融券資料")
                else:
                    for k, v in ms.items():
                        st.text(f"{k}: {v:,}")
        else:
            st.info("ℹ️ 美股不提供台股式法人/融資融券資料；技術面 + AI 分析仍可使用。")

        # 條件命中明細
        with st.expander("✅ 各條件命中狀況"):
            rows = [{"條件": tw_screener.CONDITION_LABELS.get(k, k), "命中": "✅" if v else ""}
                    for k, v in hits.items()]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # ================== AI 深度分析 ==================
        if ai_btn:
            if not ai_analyzer.gemini_available():
                st.error("尚未設定 GEMINI_API_KEY 或 google-generativeai 未安裝。")
            else:
                with st.spinner("🤖 Gemini 思考中…約 10–20 秒"):
                    ok, ai_text = ai_analyzer.analyze(
                        stock_meta={
                            "stock_id": full["stock_id"], "name": full["name"],
                            "industry": full["industry"], "market": full["market"],
                        },
                        daily=full["daily"], ind=ind,
                        inst=full["inst"], margin=full["margin"],
                        hits=hits, score=score,
                    )
                if ok:
                    st.session_state["last_ai"] = {"sid": sid, "text": ai_text}
                    st.markdown("---")
                    st.markdown("### 🤖 Gemini 深度分析")
                    st.markdown(ai_text)
                else:
                    st.error(f"AI 分析失敗：{ai_text}")

        # 顯示快取的 AI 結果 (即使這次沒按 AI 按鈕)
        last_ai = st.session_state.get("last_ai")
        if last_ai and last_ai.get("sid") == sid and not ai_btn:
            with st.expander("🤖 上次 AI 分析", expanded=False):
                st.markdown(last_ai["text"])

        # 推送 AI 到 TG
        last_ai = st.session_state.get("last_ai")
        if last_ai and last_ai.get("sid") == sid and notifier.is_configured():
            if st.button("✈️ Send AI 分析 to TG", key="send_ai_tg", use_container_width=True):
                ok, info = notifier.send_message(
                    notifier.fmt_ai_analysis(sid, full["name"] or "", last_ai["text"])
                )
                if ok:
                    st.success("AI 分析已推送 ✅")
                else:
                    st.error(info)

    # ================== 🖼️ 上傳 K 線圖讓 Gemini 分析 ==================
    st.divider()
    st.markdown("### 🖼️ 上傳 K 線圖 → AI 視覺分析")
    st.caption("拍/截圖任意股票走勢圖丟上來，Gemini 會結合恐慌指數和市場新聞做綜合判讀。")

    uploaded = st.file_uploader(
        "選擇圖片 (PNG / JPG)", type=["png", "jpg", "jpeg"],
        key="chart_upload",
    )
    extra_note = st.text_input(
        "備註 (可選 — 例如：這是 NVDA 日 K，您想知道現在能不能進場)",
        value="", key="chart_note",
    )

    cI1, cI2 = st.columns([1, 1])
    with cI1:
        analyze_image_btn = st.button(
            "🤖 用 Gemini 分析這張圖", use_container_width=True, type="primary",
            disabled=(not ai_analyzer.gemini_available()) or uploaded is None,
        )
    with cI2:
        send_image_tg = st.button(
            "✈️ Send 圖片分析 to TG", use_container_width=True,
            disabled=not notifier.is_configured(), key="send_image_tg",
        )

    if uploaded is not None:
        st.image(uploaded, caption="上傳的圖片", use_column_width=True)

    if analyze_image_btn and uploaded is not None:
        # 抓最新市場 context
        fg = ds.fetch_fear_greed()
        market_news = ds.fetch_market_news_themes()
        with st.spinner("🤖 Gemini 看圖思考中…約 10–20 秒"):
            ok, ai_text = ai_analyzer.analyze_chart_image(
                uploaded.getvalue(), extra_note=extra_note,
                fg=fg, market_news=market_news,
            )
        if ok:
            st.session_state["last_image_ai"] = ai_text
            st.markdown("---")
            st.markdown("### 🤖 Gemini 圖片分析")
            st.markdown(ai_text)
        else:
            st.error(f"分析失敗：{ai_text}")

    last_img = st.session_state.get("last_image_ai")
    if last_img and not analyze_image_btn:
        with st.expander("🤖 上次圖片分析", expanded=False):
            st.markdown(last_img)

    if send_image_tg and last_img and notifier.is_configured():
        ok, info = notifier.send_message(
            notifier.fmt_ai_analysis("圖片分析", "上傳圖片", last_img)
        )
        if ok:
            st.success("已送出 ✅")
        else:
            st.error(info)


with tab_us:
    st.subheader("美股 Top 5 推薦 (技術 + 動能 + 題材 + 市場情緒)")
    st.caption("候選池可在 Streamlit secrets 加入 `US_WATCHLIST=AAPL,MSFT,...` 自訂。")

    cA, cB = st.columns([1, 1])
    with cA:
        us_btn = st.button("🔄 更新美股推薦", use_container_width=True, type="primary")
    with cB:
        send_us_tg = st.button("✈️ Send to TG", use_container_width=True,
                               disabled=not notifier.is_configured(),
                               key="send_us_tg")

    if us_btn:
        try:
            with st.spinner("掃描美股候選池中…(約 30~90 秒)"):
                st.session_state["us_result"] = us_screener.run_us_recommendation(top_n=5)
        except Exception as e:
            st.error(f"美股掃描失敗：{e}")

    us = st.session_state.get("us_result", {})
    top_picks = us.get("top_picks")
    fg = us.get("fear_greed", {})

    if fg and fg.get("score") is not None:
        cm1, cm2, cm3 = st.columns(3)
        cm1.metric("Fear & Greed", round(float(fg["score"]), 1), fg.get("rating", ""))
        prev = fg.get("previous_close", {})
        if isinstance(prev, dict) and prev.get("score") is not None:
            cm2.metric("昨日", round(float(prev["score"]), 1), prev.get("rating", ""))
        wk = fg.get("previous_1_week", {})
        if isinstance(wk, dict) and wk.get("score") is not None:
            cm3.metric("一週前", round(float(wk["score"]), 1), wk.get("rating", ""))

    if top_picks is None or top_picks.empty:
        st.info("資料抓取中或無命中標的，請稍後再試。")
    else:
        show_df = top_picks.drop(columns=["近期新聞"], errors="ignore")
        st.dataframe(show_df, use_container_width=True, hide_index=True)
        with st.expander("📰 候選個股近期新聞 / 題材"):
            for _, row in top_picks.iterrows():
                st.markdown(f"**{row['symbol']}** — 題材: {row.get('題材') or '—'}")
                for n in row.get("近期新聞", []) or []:
                    title = n.get("title")
                    link = n.get("link")
                    if title and link:
                        st.markdown(f"- [{title}]({link}) · _{n.get('publisher','')}_")
                st.markdown("---")

        if send_us_tg:
            ok, info = notifier.send_message(notifier.fmt_us_top_picks(top_picks, fg))
            if ok:
                st.success("已送出 ✅")
            else:
                st.error(info)


# =============================================================================
# Tab 4 — 市場情緒 (Fear & Greed + 板塊輪動 + 新聞題材)
# =============================================================================
with tab_mood:
    st.subheader("Fear & Greed + 板塊輪動 + 市場新聞題材")
    mood_btn = st.button("🔄 抓取市場情緒", use_container_width=True, type="primary",
                         key="mood_btn_main")
    if mood_btn:
        try:
            with st.spinner("抓取市場情緒資料…"):
                st.session_state["us_result"] = us_screener.run_us_recommendation(top_n=5)
        except Exception as e:
            st.error(f"抓取失敗：{e}")
    us = st.session_state.get("us_result", {})
    fg = us.get("fear_greed", {})
    sectors_us = us.get("sectors")
    news_pool: List = us.get("news") or []

    # 並列顯示台股 + 美股市場情緒
    tw_pulse = ds.fetch_tw_market_pulse()
    pcol1, pcol2 = st.columns(2)
    with pcol1:
        st.markdown("#### 🇹🇼 台股市場情緒指數")
        if tw_pulse and tw_pulse.get("score") is not None:
            st.metric(tw_pulse.get("rating_zh", ""),
                       round(float(tw_pulse["score"]), 1),
                       help="加權指數動能 + 波動率 + 距 MA60 合成")
            with st.expander("子項分數 + 原始資料"):
                comp = tw_pulse.get("components", {})
                for k, v in comp.items():
                    st.text(f"{k}: {v}")
                st.divider()
                for k, v in tw_pulse.get("raw", {}).items():
                    st.text(f"{k}: {v}")
        else:
            st.info("尚未取得加權指數資料")

    with pcol2:
        st.markdown("#### 🇺🇸 CNN Fear & Greed")
        if fg and fg.get("score") is not None:
            st.metric(fg.get("rating", ""), round(float(fg["score"]), 1),
                       help="CNN 公開的美股市場情緒指數")
            prev = fg.get("previous_close", {})
            if isinstance(prev, dict) and prev.get("score") is not None:
                st.caption(f"昨日 {round(float(prev['score']),1)} ({prev.get('rating','')})")
        else:
            st.info("尚未取得 CNN 資料")

    # 美股 F&G 異常觸發
    if fg and fg.get("score") is not None:
        alert_msg = notifier.fmt_fear_greed_alert(fg)
        if alert_msg:
            st.warning(alert_msg, icon="⚠️")
            if auto_send_on_alert and notifier.is_configured():
                fp = ("fg", round(float(fg["score"]), 1))
                if st.session_state.get("_fg_last_alert") != fp:
                    ok, _ = notifier.send_message(alert_msg)
                    if ok:
                        st.session_state["_fg_last_alert"] = fp

    # 台股 F&G 異常觸發
    if tw_pulse and tw_pulse.get("score") is not None:
        tw_alert = notifier.fmt_tw_pulse_alert(tw_pulse)
        if tw_alert:
            st.warning(tw_alert, icon="⚠️")

    if sectors_us is not None and not sectors_us.empty:
        st.markdown("#### S&P SPDR 板塊輪動 (5 日 %)")
        st.dataframe(sectors_us, use_container_width=True, hide_index=True)

    if news_pool:
        st.markdown("#### 近 24 小時市場新聞題材")
        for n in news_pool[:15]:
            title = n.get("title")
            link = n.get("link")
            pub = n.get("publisher", "")
            if title and link:
                st.markdown(f"- [{title}]({link}) · _{pub}_")
    elif "us_result" in st.session_state:
        st.info("目前抓不到 24h 內的市場新聞。")
    else:
        st.info("按上方「抓取市場情緒」開始。")

st.markdown(
    "<div style='text-align:center;color:#888;font-size:12px;margin-top:24px'>"
    "本網站由 Streamlit + FinMind + yfinance 建構，僅供研究參考，非投資建議。"
    "</div>",
    unsafe_allow_html=True,
)
