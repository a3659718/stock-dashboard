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

import data_sources as ds
import notifier
import sector_pulse
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

    st.divider()
    st.subheader("自動 Telegram 通知")
    auto_send_on_hit = st.checkbox("命中條件即自動發送", value=False)
    auto_send_on_alert = st.checkbox("強勢族群 / 恐慌指數異常時推播", value=True)


tw_params = tw_screener.TWParams(
    vol_min_ratio=float(vol_min),
    vol_max_ratio=float(vol_max),
    short_inc_lots=int(short_inc),
    max_stocks=int(max_stocks),
)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_tw, tab_pulse, tab_us, tab_mood = st.tabs(
    ["🇹🇼 台股篩選", "🚀 強勢族群", "🇺🇸 美股 Top 5", "🧭 市場情緒"]
)


# =============================================================================
# Tab 1 — 台股篩選
# =============================================================================
with tab_tw:
    st.subheader("台股盤後四條件篩選")
    st.caption(
        "1) 突破月/季線 · 2) 量 5–10 倍均量 · 3) 融券+50 張以上 · 4) 投信 30 日首買"
    )

    colA, colB, colC = st.columns([1, 1, 1])
    with colA:
        run_btn = st.button("🔄 重新整理 / 開始掃描", use_container_width=True, type="primary")
    with colB:
        send_tg_btn = st.button("✈️ 把結果送到 Telegram", use_container_width=True, disabled=not notifier.is_configured())
    with colC:
        filter_mode = st.selectbox(
            "顯示模式",
            options=["全部命中", "符合任一條件", "至少 2 項", "至少 3 項", "全部 4 項"],
            index=0,
        )

    if run_btn:
        if not ds.finmind_available():
            st.error("FinMind 套件未安裝，無法掃描。請先把 Python version 改成 3.11。")
        else:
            try:
                with st.spinner("掃描全市場中…(首次約 30~60 秒)"):
                    res = tw_screener.run_all_screens(market=market_choice, params=tw_params)
                st.session_state["tw_result"] = res
            except Exception as e:
                st.error(f"掃描失敗：{e}")

    res = st.session_state.get("tw_result")
    if not res:
        st.info("按上方「重新整理」開始掃描。")
    else:
        latest = res["latest_date"]
        if latest is not None:
            latest_str = pd.Timestamp(latest).strftime("%Y-%m-%d")
        else:
            latest_str = "N/A"
        if not res["ready"]:
            st.warning(
                f"⚠️ 今日({dt.date.today().strftime('%Y-%m-%d')})盤後資料尚未更新，"
                f"目前以最新一個交易日 {latest_str} 的資料計算。"
                "建議台股 14:30 之後再查詢。"
            )
        else:
            st.success(f"資料日期: {latest_str} ✅")

        # 個別條件統計
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("突破月/季線", len(res["break_ma"]))
        c2.metric(f"量 {int(vol_min)}-{int(vol_max)} 倍", len(res["volume_burst"]))
        c3.metric(f"融券增加 {int(short_inc)}+", len(res["short_increase"]))
        c4.metric("投信 30 日首買", len(res["invtrust_first_buy"]))

        combined: pd.DataFrame = res["combined"]
        if combined.empty:
            st.info("今日無任何條件命中。")
        else:
            # 篩選顯示模式
            if filter_mode == "全部命中":
                view = combined
            elif filter_mode == "符合任一條件":
                view = combined
            elif filter_mode == "至少 2 項":
                view = combined[combined["hit_count"] >= 2]
            elif filter_mode == "至少 3 項":
                view = combined[combined["hit_count"] >= 3]
            else:
                view = combined[combined["hit_count"] >= 4]

            st.markdown(f"**共 {len(view)} 檔符合**")
            display = view.rename(
                columns={
                    "stock_id": "代號",
                    "stock_name": "名稱",
                    "market": "市場",
                    "hit_count": "命中數",
                    "hits_label": "命中條件",
                }
            )[["代號", "名稱", "市場", "命中數", "命中條件"]]
            st.dataframe(display, use_container_width=True, hide_index=True)

            with st.expander("📁 各條件原始明細"):
                t1, t2, t3, t4 = st.tabs(["突破均線", "量能爆增", "融券增加", "投信首買"])
                with t1:
                    st.dataframe(res["break_ma"], use_container_width=True, hide_index=True)
                with t2:
                    st.dataframe(res["volume_burst"], use_container_width=True, hide_index=True)
                with t3:
                    st.dataframe(res["short_increase"], use_container_width=True, hide_index=True)
                with t4:
                    st.dataframe(res["invtrust_first_buy"], use_container_width=True, hide_index=True)

            # Telegram 自動推播 (命中即發)
            if auto_send_on_hit and notifier.is_configured() and not view.empty:
                already = st.session_state.get("_tw_last_sent")
                fingerprint = (latest_str, tuple(view["stock_id"].head(40)))
                if already != fingerprint:
                    msg = notifier.fmt_tw_combined(view, latest_str, market_label="自動推播")
                    ok, info = notifier.send_message(msg)
                    if ok:
                        st.session_state["_tw_last_sent"] = fingerprint
                        st.toast("已自動推送 Telegram", icon="✈️")
                    else:
                        st.warning(f"自動推播失敗: {info}")

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
    st.subheader("台股即時 / 開盤強勢族群")
    st.caption("以前 200 大流動性個股當下漲跌幅，依產業分組計算族群熱度。")

    cA, cB = st.columns([1, 1])
    with cA:
        pulse_btn = st.button("🔄 更新即時資料", use_container_width=True, type="primary")
    with cB:
        send_pulse_tg = st.button("✈️ 把族群熱度送到 Telegram", use_container_width=True,
                                  disabled=not notifier.is_configured())

    if pulse_btn:
        if not ds.finmind_available():
            st.error("FinMind 套件未安裝，無法掃描。請先把 Python version 改成 3.11。")
        else:
            try:
                with st.spinner("計算族群熱度中…"):
                    st.session_state["pulse"] = sector_pulse.compute_strong_sectors(top_n=200)
            except Exception as e:
                st.error(f"族群分析失敗：{e}")

    pulse = st.session_state.get("pulse", {})
    sectors = pulse.get("sectors")
    leaders = pulse.get("leaders")
    if sectors is None or sectors.empty:
        st.info("尚未取得即時資料 (盤前/休市 或 yfinance 暫時無回應)。")
    else:
        first_col = sectors.columns[0]
        top5 = sectors.head(5).copy()
        st.markdown("#### Top 5 強勢族群")
        st.dataframe(
            top5.rename(columns={first_col: "產業", "avg_change": "平均%", "median_change": "中位%",
                                 "up_count": "上漲家數", "n": "樣本數", "up_ratio": "上漲比率"}),
            use_container_width=True, hide_index=True,
        )
        if leaders is not None and not leaders.empty:
            with st.expander("各族群龍頭 (前 3 名)"):
                show_cols = [c for c in ["industry_category", "stock_id", "stock_name", "change_pct", "last"] if c in leaders.columns]
                st.dataframe(leaders[show_cols], use_container_width=True, hide_index=True)

        # 異常觸發推播
        if auto_send_on_alert and notifier.is_configured():
            top1 = sectors.iloc[0]
            avg = float(top1["avg_change"])
            if avg >= 1.5:  # 平均漲幅 > 1.5% 視為強勢
                fingerprint = ("strong_sector", top1[first_col], round(avg, 2))
                if st.session_state.get("_pulse_last_alert") != fingerprint:
                    ok, info = notifier.send_message(notifier.fmt_strong_sectors(sectors))
                    if ok:
                        st.session_state["_pulse_last_alert"] = fingerprint
                        st.toast("已推送強勢族群通知", icon="🚀")

        if send_pulse_tg:
            ok, info = notifier.send_message(notifier.fmt_strong_sectors(sectors))
            if ok:
                st.success("已送出 ✅")
            else:
                st.error(info)


# =============================================================================
# Tab 3 — 美股 Top 5
# =============================================================================
with tab_us:
    st.subheader("美股 Top 5 推薦 (技術 + 動能 + 題材 + 市場情緒)")
    st.caption("候選池可在 Streamlit secrets 加入 `US_WATCHLIST=AAPL,MSFT,...` 自訂。")

    cA, cB = st.columns([1, 1])
    with cA:
        us_btn = st.button("🔄 更新美股推薦", use_container_width=True, type="primary")
    with cB:
        send_us_tg = st.button("✈️ 把 Top 5 送到 Telegram", use_container_width=True,
                               disabled=not notifier.is_configured())

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

    if fg and fg.get("score") is not None:
        st.metric("CNN Fear & Greed Index", round(float(fg["score"]), 1), fg.get("rating", ""))
        alert_msg = notifier.fmt_fear_greed_alert(fg)
        if alert_msg:
            st.warning(alert_msg, icon="⚠️")
            if auto_send_on_alert and notifier.is_configured():
                fp = ("fg", round(float(fg["score"]), 1))
                if st.session_state.get("_fg_last_alert") != fp:
                    ok, _ = notifier.send_message(alert_msg)
                    if ok:
                        st.session_state["_fg_last_alert"] = fp

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
