"""
app.py — 台美股盤前盤後雷達網站
========================================================
* 在手機 / 桌機都能直接使用
* 重新整理會重新計算 (有 cache)
* Telegram 推播：手動 / 命中即發 / 異常觸發三種模式
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import pandas as pd
import streamlit as st

import ai_analyzer
import backtest
import data_sources as ds
import earnings_calendar
import market_open_picks
import news_picks
import notifier
import sector_pulse
import stock_analyzer
import stock_catalyst
import tracker
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

      /* 分頁列 — 變得更醒目，過窄時會自動換行 */
      .stTabs [data-baseweb="tab-list"] {
          gap: 6px;
          flex-wrap: wrap;
          overflow-x: auto;
          border-bottom: 1px solid rgba(127,127,127,0.18);
          padding-bottom: 4px;
      }
      .stTabs [data-baseweb="tab"] {
          background: rgba(127,127,127,0.10);
          border-radius: 8px;
          padding: 10px 14px;
          font-size: 13px;
          font-weight: 500;
          white-space: nowrap;
          margin-bottom: 4px;
          border: 1px solid transparent;
          transition: all 0.15s;
      }
      .stTabs [data-baseweb="tab"]:hover {
          background: rgba(127,127,127,0.20);
      }
      .stTabs [aria-selected="true"] {
          background: rgba(31, 111, 235, 0.22) !important;
          border: 1px solid rgba(31, 111, 235, 0.6) !important;
          color: #1f6feb !important;
      }

      @media (max-width: 640px) {
        .block-container { padding-left: 0.6rem; padding-right: 0.6rem; }
        .stTabs [data-baseweb="tab"] { padding: 8px 10px; font-size: 12px; }
      }

      /* 日期 banner */
      .date-banner {
          display: flex; align-items: center; gap: 14px;
          padding: 10px 14px;
          background: linear-gradient(90deg, rgba(31,111,235,0.12), rgba(31,111,235,0.04));
          border-radius: 10px;
          border-left: 4px solid #1f6feb;
          margin-bottom: 14px;
          font-size: 14px;
      }
      .date-banner b { font-size: 16px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# 紅綠色 helper — 篩選表格漲跌欄位上色
# 台股 (TW): 紅漲 / 綠跌  ; 美股 (US): 綠漲 / 紅跌
# ---------------------------------------------------------------------------
def _style_pct(df, market: str = "TW", pct_cols: list = None):
    """回傳 pandas Styler, 漲跌% 欄位上紅/綠色."""
    if df is None or (hasattr(df, "empty") and df.empty):
        return df
    if pct_cols is None:
        # auto-detect 含 "%" 的欄位
        pct_cols = [c for c in df.columns if "%" in str(c) or str(c).endswith("pct")]
    pct_cols = [c for c in pct_cols if c in df.columns]
    if not pct_cols:
        return df

    def _c(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return ""
        if pd.isna(x):
            return ""
        if market.upper() == "TW":
            if x > 0:
                return "color: #c62828; font-weight: bold"
            if x < 0:
                return "color: #2e7d32; font-weight: bold"
        else:  # US 反過來
            if x > 0:
                return "color: #2e7d32; font-weight: bold"
            if x < 0:
                return "color: #c62828; font-weight: bold"
        return ""

    try:
        return df.style.applymap(_c, subset=pct_cols)
    except Exception:
        return df


def _fmt_num(v):
    """格式化數值: 最多顯示 2 位小數, 無小數值時顯示整數.
    例: 100.0 -> "100", 100.5 -> "100.5", 100.567 -> "100.57"
    """
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        return str(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    rounded = round(f, 2)
    # 浮點誤差判斷: 接近整數視為整數
    if abs(rounded - round(rounded)) < 1e-9:
        return f"{int(round(rounded))}"
    # 兩位小數, 去掉尾隨 0 (例: 100.50 -> 100.5)
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _show_table(df, market: str = "TW", pct_cols: list = None, **kwargs):
    """st.dataframe wrapper — 漲跌自動上色 + 數值最多 2 位小數 (整數時不顯示小數)."""
    if df is None or (hasattr(df, "empty") and df.empty):
        st.dataframe(df, **kwargs)
        return
    # 統一數值欄位四捨五入到 2 位 (保留 int 欄位)
    try:
        df = df.copy()
        for col in df.columns:
            ser = df[col]
            # 不動 dtype object / int (張數那類); 只 round float
            if pd.api.types.is_float_dtype(ser):
                df[col] = ser.round(2)
    except Exception:
        pass
    styled = _style_pct(df, market, pct_cols)
    if hasattr(styled, "format"):
        try:
            # 用自訂 formatter: 最多 2 位小數, 整數則顯示為整數
            float_cols = [c for c in df.columns if pd.api.types.is_float_dtype(df[c])]
            if float_cols:
                styled = styled.format({c: _fmt_num for c in float_cols})
        except Exception:
            pass
    kwargs.setdefault("use_container_width", True)
    kwargs.setdefault("hide_index", True)
    st.dataframe(styled, **kwargs)


def _send_tg(msg: str, label: str = "推播",
              stock_id: Optional[str] = None, market: str = "TW") -> bool:
    """統一的 TG 推送 + 顯示成功/失敗 toast.
    用在所有「Send to TG」按鈕, 確保使用者看得到結果.

    若提供 stock_id, 會附加 inline keyboard (➕加自選 / 🤖AI / 🛡️停損 / 📊 看圖).
    """
    if not msg or not msg.strip():
        st.warning(f"{label}：沒有可推送內容")
        return False
    if not notifier.is_configured():
        st.error(
            f"{label} 失敗：TG bot 未設定。\n"
            "請到 Streamlit Cloud → App → Settings → Secrets 補 "
            "`TELEGRAM_BOT_TOKEN` 跟 `TELEGRAM_CHAT_ID`"
        )
        return False
    try:
        reply_markup = None
        if stock_id:
            reply_markup = notifier.build_stock_action_keyboard(stock_id, market=market)
        ok, info = notifier.send_message(msg, reply_markup=reply_markup)
        if ok:
            st.success(f"已推送到 TG：{label}")
            return True
        st.error(f"{label} 失敗：{info}")
        return False
    except Exception as e:
        st.error(f"{label} 例外：{type(e).__name__}: {e}")
        return False


# 頂部日期 banner
import pytz
_tw_now = dt.datetime.now(pytz.timezone("Asia/Taipei"))
_us_now = dt.datetime.now(pytz.timezone("America/New_York"))
_weekday = ["週一","週二","週三","週四","週五","週六","週日"][_tw_now.weekday()]
_tw_state = "✅ 開盤中" if (_tw_now.weekday() <= 4) and ((9 <= _tw_now.hour < 13) or (_tw_now.hour == 13 and _tw_now.minute <= 30)) else "🔴 休市"
_us_state = "✅ 開盤中" if (_us_now.weekday() <= 4) and ((_us_now.hour == 9 and _us_now.minute >= 30) or (10 <= _us_now.hour < 16)) else "🔴 休市"
st.markdown(
    f"<div class='date-banner'>"
    f"📅 <b>{_tw_now.strftime('%Y-%m-%d')} {_weekday}</b>"
    f"&nbsp;·&nbsp; 台北 {_tw_now.strftime('%H:%M')} 台股 {_tw_state}"
    f"&nbsp;·&nbsp; 紐約 {_us_now.strftime('%H:%M')} 美股 {_us_state}"
    f"</div>",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Sidebar (參數 / 設定狀態)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ 設定")
    st.caption(f"今天: {dt.date.today().strftime('%Y-%m-%d')}")

    # 快取強制清除 — 盤中懷疑資料 stale 可一鍵重抓
    cc1, cc2 = st.columns([3, 2])
    with cc1:
        st.caption("資料快取")
    with cc2:
        if st.button("🔄 重抓", help="清掉所有 cache, 強制重新呼叫 yfinance / FinMind / Gemini",
                     use_container_width=True, key="sidebar_clear_cache"):
            try:
                st.cache_data.clear()
                try:
                    st.cache_resource.clear()
                except Exception:
                    pass
                # 也清 yfinance in-mem cache (G9)
                try:
                    import data_sources as _ds_clear
                    if hasattr(_ds_clear, "_yf_cache_clear"):
                        _ds_clear._yf_cache_clear()
                except Exception:
                    pass
                st.toast("快取已清，重新跑一次", icon="✅")
                st.rerun()
            except Exception as _e:
                st.error(f"清快取失敗: {_e}")

    # ===== 系統健康狀態 (探外部 API, 解決「按鈕沒反應」根因) =====
    # 為什麼: 多個按鈕 (growth / pulse / theme / tw_open 等) 都依賴 FinMind / yfinance / Gemini.
    #         若任一外部 API 失效, 對應按鈕會「silent return empty」, 看起來像沒反應.
    #         這個 sidebar 一目了然顯示哪個 API 壞了, 省得每個按鈕單獨測試.
    with st.expander("🔧 系統狀態 (API 健康)", expanded=False):
        if st.button("🩺 測一次", use_container_width=True, key="sidebar_health_check",
                      help="會打 FinMind / yfinance / Gemini 各一次 (約 3-5 秒)"):
            with st.spinner("探測中..."):
                try:
                    import heartbeat as _hb
                    yf_ok, yf_info = _hb._probe_yfinance()
                    fm_ok, fm_info = _hb._probe_finmind()
                    gm_ok, gm_info = _hb._probe_gemini()
                    tg_ok, tg_info = _hb._probe_telegram_config()
                    # 儲存到 session_state 給後續 render
                    st.session_state["_sys_status"] = {
                        "yf": (yf_ok, yf_info),
                        "fm": (fm_ok, fm_info),
                        "gm": (gm_ok, gm_info),
                        "tg": (tg_ok, tg_info),
                        "ts": dt.datetime.now().strftime("%H:%M:%S"),
                    }
                except Exception as _hbe:
                    st.error(f"探測失敗: {_hbe}")
        sys_status = st.session_state.get("_sys_status")
        if sys_status:
            for label, (probe_key, probe_name) in [
                ("yfinance", ("yf", "yfinance (盤中行情)")),
                ("finmind", ("fm", "FinMind (台股清單/日線)")),
                ("gemini",  ("gm", "Gemini (AI 分析)")),
                ("tg",      ("tg", "Telegram (推播)")),
            ]:
                ok, info = sys_status.get(probe_key, (False, "未測試"))
                icon = "✅" if ok else "❌"
                st.markdown(f"{icon} **{probe_name}**: {info}")
            st.caption(f"上次測試: {sys_status.get('ts', '—')}")
            # 給「按鈕沒反應」的指引
            if not sys_status["fm"][0]:
                st.warning(
                    "⚠️ FinMind 失效 → 大部分台股按鈕 (篩選/題材/成長動能) 都會 silent fail. "
                    "到 https://finmindtrade.com/ 重新生成 token 並更新 Streamlit secrets."
                )
            if not sys_status["yf"][0]:
                st.warning(
                    "⚠️ yfinance 失效 → 大盤點數 / 美股 / 加密幣相關按鈕會 silent fail. "
                    "通常是 IP 被 rate-limit, 等 1-2 hr 自動恢復."
                )
        else:
            st.caption("尚未測試. 點上方 🩺 按鈕做一次健康檢查.")

    # 部位規模設定 — 用於 picks card 給張數建議
    with st.expander("💰 部位規模設定 (用於目標價推薦)"):
        try:
            import position_sizer as _ps_mod
            _ps_cfg = _ps_mod.load_user_config()
        except Exception:
            _ps_cfg = {"account_capital": 1_000_000, "risk_per_trade_pct": 1.0, "max_position_pct": 20.0}
        ps_cap = st.number_input("帳戶資金 (NTD)", min_value=10000, step=10000,
                                  value=int(_ps_cfg["account_capital"]),
                                  key="ps_cap")
        ps_risk = st.slider("單筆最大風險 (% 帳戶)", 0.25, 5.0,
                             float(_ps_cfg["risk_per_trade_pct"]), step=0.25,
                             help="保守 0.5-1%, 標準 1-2%, 積極 2-3%",
                             key="ps_risk")
        ps_max = st.slider("單筆最大部位 (% 帳戶)", 5.0, 50.0,
                            float(_ps_cfg["max_position_pct"]), step=5.0,
                            help="避免單檔過度集中, 建議 ≤ 25%",
                            key="ps_max")
        if st.button("儲存部位規模設定", use_container_width=True, key="ps_save"):
            try:
                import position_sizer as _ps_mod
                ok = _ps_mod.save_user_config(ps_cap, ps_risk, ps_max)
                if ok:
                    st.toast("已儲存", icon="✅")
                else:
                    st.warning("儲存失敗 (詳見 log)")
            except Exception as _e:
                st.error(f"儲存失敗: {_e}")

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


# 跨 session 共享 + 檔案持久化的 alert dedup 狀態
import json as _json
import os as _os
from pathlib import Path as _Path

_ALERT_STATE_FILE = _Path(".alert_state.json")


@st.cache_resource
def _alert_dedup_state() -> dict:
    """跨 session 共用的去重 dict。優先從檔案讀，避免容器重啟後重發。"""
    if _ALERT_STATE_FILE.exists():
        try:
            return _json.loads(_ALERT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_dedup_state(state: dict) -> None:
    """Atomic 寫入 dedup state — 用 watchlist_store._atomic_write_text 保證
    並發 cron / Streamlit 不會看到半寫狀態."""
    try:
        import watchlist_store
        watchlist_store._atomic_write_text(
            _ALERT_STATE_FILE,
            _json.dumps(state, ensure_ascii=False, indent=2),
        )
    except Exception:
        # Fall back: 普通 write_text (race-prone 但至少不 crash)
        try:
            _ALERT_STATE_FILE.write_text(
                _json.dumps(state, ensure_ascii=False), encoding="utf-8",
            )
        except Exception:
            pass


def _should_send_once(key: str) -> bool:
    """同一個 key 一天最多送一次。回傳 True = 可以送、False = 跳過。
    狀態同時存記憶體 (cache_resource) 與檔案，跨容器重啟也會去重。
    跨日會自動清空 (透過 _date 標記)。
    """
    state = _alert_dedup_state()
    today = dt.date.today().isoformat()

    # 跨日自動清空
    if state.get("_date") != today:
        state.clear()
        state["_date"] = today
        _save_dedup_state(state)

    if state.get(key):
        return False
    state[key] = True
    _save_dedup_state(state)
    return True


def _release_send_once(key: str) -> None:
    """回滾 _should_send_once 紀錄. 推送失敗時用 — 讓下次 rerun 還能重試."""
    try:
        state = _alert_dedup_state()
        if key in state:
            del state[key]
            _save_dedup_state(state)
    except Exception:
        pass


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
# 外層 10 個 tab (個股+入場合一個 outer, 回測+追蹤合一個 outer)
(tab_wl, tab_actionable, tab_hold, tab_tw, tab_pulse, tab_growth,
 _tab_stock_outer, tab_us, tab_mood, _tab_bt_outer) = st.tabs(
    ["📋 自選股", "🎯 今日可行動", "💼 持倉分析", "🇹🇼 台股篩選", "🚀 強勢族群",
     "🌱 成長動能", "🔍 個股 (分析+入場)", "🇺🇸 美股 Top 10", "🧭 市場情緒",
     "📊 策略驗證 (回測+追蹤)"]
)

# Sub-tabs: 個股 outer 內含 2 個 (入場評估 / 深度分析)
with _tab_stock_outer:
    _inner_s = st.tabs(["⚡ 入場評估 (快, 30s)", "📊 深度分析 (Gemini 完整, 10-20s)"])
    tab_entry = _inner_s[0]
    tab_stock = _inner_s[1]

# Sub-tabs: 策略驗證 outer 內含 2 個 (回測 / 追蹤)
with _tab_bt_outer:
    _inner_b = st.tabs(["📈 回測勝率", "📋 推薦追蹤"])
    tab_bt = _inner_b[0]
    tab_track = _inner_b[1]


# =============================================================================
# Tab — 今日可行動 (整合各訊號的 Top 10 卡片)
# =============================================================================
with tab_actionable:
    st.subheader("🎯 今日 Top 10 可行動")
    st.caption(
        "整合「強勢族群龍頭 / 催化劑 / 隔日突破 / 潛力股」等所有訊號, "
        "用 R:R + 訊號交叉驗證 + 部位規模建議排出可下單清單. "
        "點下方按鈕重抓 (耗時 30-60 秒, Gemini quota 多燒一次)."
    )
    cA1, cA2, cA3 = st.columns([1, 1, 2])
    with cA1:
        load_actionable = st.button("🔄 重抓 Top 10", use_container_width=True,
                                      key="actionable_load", type="primary")
    with cA2:
        send_actionable_tg = st.button("✈️ 推 Top 10 到 TG", use_container_width=True,
                                         key="actionable_tg")
    with cA3:
        # 顯示上次抓的時間 (從 cache timestamp)
        last_ts = st.session_state.get("actionable_picks_ts")
        if last_ts:
            st.caption(f"上次更新: {last_ts}")

    # 只有按按鈕才 trigger — 不再「第一次進 tab 就自動跑」浪費 Gemini quota
    if load_actionable:
        with st.spinner("整合所有訊號中… (這需要呼叫 Gemini + yfinance, 30-60 秒)"):
            try:
                import actionable_picks as _ap
                st.session_state["actionable_picks_cache"] = _ap.compute_actionable_picks(top_n=10)
                st.session_state["actionable_picks_ts"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            except Exception as e:
                st.error(f"整合失敗: {type(e).__name__}: {e}")
                st.session_state["actionable_picks_cache"] = []
                st.session_state["actionable_picks_ts"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M") + " (失敗)"

    # raw_picks 是「沒被空頭邏輯改過的原始結果」, 用於 TG 推播
    raw_picks = st.session_state.get("actionable_picks_cache", None)

    # 還沒按過按鈕 — 直接顯示提示, 不要自動跑
    if raw_picks is None:
        st.info(
            "👆 按「重抓 Top 10」開始整合訊號. \n\n"
            "這個分析會跑:\n"
            "- compute_hot_themes (族群熱度 + 催化劑 Gemini)\n"
            "- find_emerging_themes (萌芽族群 + 法人卡位)\n"
            "- pick_next_day_breakout (隔日突破篩選)\n"
            "- regime_detector (大盤狀態判定)\n"
            "首次跑大概 30-60 秒, 之後會 cache 直到你重新按."
        )
    else:
        picks = list(raw_picks) or []  # copy 一份用於 dashboard render
        first = picks[0] if picks else {}
        regime = first.get("_regime") if first else None

        # Regime banner — 取第 1 筆 attach 的
        if regime:
            rg_label = regime.get("regime_label", "")
            rg_score = regime.get("score", 0)
            rg_guidance = regime.get("guidance", "")
            rg = regime.get("regime", "")
            rg_icon = {"bull":"🟢","bull_weak":"🟡","range":"⚪","bear_weak":"🟠","bear":"🔴"}.get(rg, "")
            if rg in ("bull", "bull_weak"):
                st.success(f"{rg_icon} **大盤狀態: {rg_label}** (score {rg_score}) · {rg_guidance}")
            elif rg == "range":
                st.warning(f"{rg_icon} **大盤狀態: {rg_label}** (score {rg_score}) · {rg_guidance}")
            else:
                st.error(f"{rg_icon} **大盤狀態: {rg_label}** (score {rg_score}) · {rg_guidance}")

        # 空頭 regime: picks 第 1 筆是 dummy (沒 stock_id)
        is_bear_dummy = first.get("_no_picks_reason") and not first.get("stock_id")
        if is_bear_dummy:
            st.error(f"⚠ {first['_no_picks_reason']}")
            st.info("保護資金優先, 觀察空頭結束後再進場.")
            picks = []  # 強制不 render 任何 card (但 raw_picks 仍保留給 TG 用)

        if not picks:
            if not regime and not is_bear_dummy:
                # 真的沒結果 — 可能是 yfinance 全失敗 / Gemini quota 滿
                st.warning(
                    "⚠ 沒抓到可行動清單. 可能原因:\n"
                    "- 盤前 / 休市時資料還沒更新\n"
                    "- Gemini quota 已滿 (檢查 Google Cloud console)\n"
                    "- yfinance / FinMind 暫時失效\n\n"
                    "再按一次「重抓 Top 10」試試, 或檢查 sidebar 的設定狀態."
                )
        else:
            for i, p in enumerate(picks, 1):
                rr = p.get("rr") or 0
                rr_emoji = "⭐⭐" if rr >= 3 else ("⭐" if rr >= 2 else "")
                score = p.get("score", 0)
                score_color = "🟢" if score >= 6 else ("🟡" if score >= 4 else "🔴")
                with st.container(border=True):
                    cH1, cH2 = st.columns([3, 1])
                    with cH1:
                        # 加入場標籤 emoji 一起顯示
                        entry_emoji = p.get("entry_emoji", "")
                        entry_label = p.get("entry_label", "")
                        label_str = f" · {entry_emoji} {entry_label}" if entry_label and entry_label != "—" else ""
                        st.markdown(
                            f"### {i}. `{p.get('stock_id')}` {p.get('name', '')} "
                            f"{score_color}{label_str}"
                        )
                        if p.get("theme"):
                            st.caption(f"族群: {p.get('theme')} · R:R {rr} {rr_emoji} · 綜合分數 {score}")
                        # E: 標註屬於哪個強勢族群
                        if p.get("sector_label"):
                            sap = p.get("sector_avg_pct", 0)
                            st.caption(f"📊 屬於強勢族群「{p['sector_label']}」(均漲 +{sap:.2f}%)")
                        if p.get("entry_score") is not None:
                            st.caption(f"入場評分 {p['entry_score']}/100 → {p.get('entry_action', '—')}")
                        # A: 3 層目標價
                        t_short = p.get("target_short") or p.get("target")
                        t_mid = p.get("target_mid")
                        t_long = p.get("target_long")
                        if t_mid or t_long:
                            cur_v = p.get("current") or 0
                            def _gain(t):
                                if t and cur_v:
                                    return f"+{(t/cur_v-1)*100:.1f}%"
                                return ""
                            tlines = []
                            if t_short:
                                tlines.append(f"短線 {t_short} ({_gain(t_short)})")
                            if t_mid:
                                tlines.append(f"中線 {t_mid} ({_gain(t_mid)})")
                            if t_long:
                                tlines.append(f"長線 {t_long} ({_gain(t_long)})")
                            st.caption("🎯 " + " / ".join(tlines))
                    with cH2:
                        if p.get("current") is not None:
                            st.metric("現價", f"{p['current']}")
                    cBody1, cBody2 = st.columns(2)
                    with cBody1:
                        if p.get("entry_low") and p.get("entry_high"):
                            st.write(f"**進場區間**: {p['entry_low']} ~ {p['entry_high']}")
                        if p.get("target") and p.get("stop"):
                            st.write(f"**目標**: {p['target']} · **停損**: {p['stop']}")
                        if p.get("win_prob"):
                            st.write(f"**上漲機率**: {p['win_prob']} · 持有 {p.get('hold_period', '—')}")
                        # 部位建議: 顯示所有 sizing (包含 shares=0 的 note 提示)
                        pos = p.get("position") or {}
                        if pos:
                            try:
                                import position_sizer
                                advice = position_sizer.fmt_position_advice(pos, market="TW")
                                if advice:
                                    if pos.get("shares", 0) > 0:
                                        regime_note = " 🛡 已依 regime 降部位" if pos.get("regime_adjusted") else ""
                                        st.success(f"💰 {advice}{regime_note}")
                                    else:
                                        # shares=0 (停損距離過大) — 用 warning 顯示原因
                                        st.warning(f"⚠ {advice}")
                            except Exception:
                                pass
                    with cBody2:
                        if p.get("reasons"):
                            st.write("**訊號交叉驗證**:")
                            for r in p["reasons"]:
                                st.write(f"  ✓ {r}")
                        if p.get("warnings"):
                            st.write("**警示**:")
                            for w in p["warnings"]:
                                st.write(f"  ⚠ {w}")
                        if p.get("catalyst"):
                            st.info(f"🔥 催化劑: {p['catalyst']}")

    # TG 推送 — 用 raw_picks (含空頭 dummy), 確保空頭 banner 也能推到 TG
    if send_actionable_tg:
        if raw_picks is None:
            st.warning("還沒抓資料, 請先按「重抓 Top 10」")
        elif not raw_picks:
            st.warning("沒可推送內容 (compute 回空)")
        else:
            try:
                import actionable_picks as _ap
                tg_msg = _ap.fmt_actionable_picks_tg(raw_picks)
                if tg_msg:
                    ok, info = notifier.send_message(tg_msg)
                    if ok:
                        st.toast("已推送 Top 10 到 Telegram", icon="✅")
                    else:
                        st.error(f"推送失敗: {info}")
                else:
                    st.warning("fmt 回空訊息, 不推送")
            except Exception as e:
                st.error(f"推送異常: {e}")


# =============================================================================
# Tab — 自選股管理 (含警報設定)
# =============================================================================
with tab_wl:
    st.subheader("自選股管理")
    st.caption(
        "新增最多 15 檔自選股 (台股+美股)。"
        "盤中監控每 30 分檢查，累計漲跌達到門檻就跳 TG 通知 — "
        "TW 每 ±2.5%、US 每 ±5%。基準價可選「自動抓當前股價」或「自訂入場價」。"
    )

    import watchlist_store
    import watchlist_alerts

    # 載入現有清單 + monitor state (用來顯示目前 base / 累計%)
    current_wl = watchlist_store.load_watchlist()
    _wl_state = watchlist_store.load_monitor_state().get("watchlist_alerts", {})

    cWL1, cWL2 = st.columns(2)

    with cWL1:
        st.markdown("**新增自選股**")
        new_sid = st.text_input("代號", placeholder="例 2330 / NVDA / 8082", key="wl_sid")
        new_name = st.text_input("名稱 (可選)", placeholder="例 台積電 / NVIDIA", key="wl_name")
        new_market = st.selectbox("市場", options=["TW", "US"], key="wl_market")
        base_mode = st.radio(
            "基準價來源",
            options=["自動抓當前股價", "自訂入場價"],
            horizontal=True,
            key="wl_base_mode",
            help="自動: 第一次監控時的市價當基準。自訂: 您實際買進的成本價。",
        )
        new_entry: float | None = None
        if base_mode == "自訂入場價":
            new_entry = st.number_input(
                "入場價",
                min_value=0.01,
                value=100.0,
                step=0.5,
                key="wl_entry",
                help="您實際買進的成本價 (例如 580.5)。漲跌 % 將以此價計算。",
            )
        if st.button("新增", use_container_width=True, type="primary",
                      key="wl_add", disabled=len(current_wl) >= 15):
            if new_sid.strip():
                ep = float(new_entry) if (new_entry and new_entry > 0) else None
                ok = watchlist_store.add_to_watchlist(
                    new_sid.strip().upper(), new_name.strip(), new_market, ep
                )
                if ok:
                    st.success(
                        f"已新增 {new_sid}" + (f" (入場 {ep})" if ep else " (用當前價當基準)")
                    )
                    st.rerun()
                else:
                    st.error("新增失敗 (可能已達 15 檔上限或代號為空)")

    with cWL2:
        st.markdown("**目前自選股清單 (上限 15 檔)**")
        st.markdown(f"已加入 **{len(current_wl)} / 15**")

        if not current_wl:
            st.info("還沒新增，請從左邊加入。")

    # ===== 今日表現摘要 + 個股卡片 (亞洲慣例: 紅漲 / 綠跌) =====
    if current_wl:
        @st.cache_data(ttl=300, show_spinner=False)
        def _wl_fetch_today_status(stock_id: str, market: str = "TW"):
            """抓自選股今日 (current + prev_close + today_pct)."""
            try:
                if market.upper() == "US":
                    df = ds.fetch_yf_history(stock_id, period="5d", interval="1d")
                else:
                    df = pd.DataFrame()
                    for suffix in [".TW", ".TWO"]:
                        df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="5d", interval="1d")
                        if not df.empty:
                            break
                if df is None or df.empty or len(df) < 2:
                    return None
                close = df["Close"].astype(float)
                current = float(close.iloc[-1])
                prev = float(close.iloc[-2])
                return {
                    "current": current,
                    "prev_close": prev,
                    "today_pct": (current / prev - 1) * 100 if prev > 0 else 0,
                }
            except Exception:
                return None

        st.divider()
        # 抓所有自選股即時資料
        with st.spinner("抓取即時股價..."):
            statuses = {}
            for item in current_wl:
                sid = item.get("stock_id", "")
                mk = item.get("market", "TW")
                s = _wl_fetch_today_status(sid, mk)
                if s:
                    statuses[sid] = s

        # 統計: 漲/跌/平/平均今日 % / 平均投報率
        def _pct_pos(p):
            return p > 0.005
        def _pct_neg(p):
            return p < -0.005

        up_count = sum(1 for s in statuses.values() if _pct_pos(s["today_pct"]))
        down_count = sum(1 for s in statuses.values() if _pct_neg(s["today_pct"]))
        flat_count = len(statuses) - up_count - down_count
        today_pcts = [s["today_pct"] for s in statuses.values()]
        avg_today = sum(today_pcts) / len(today_pcts) if today_pcts else 0

        # 投報率 vs base_price (優先 monitor state, 沒就用 entry_price)
        rois = []
        for item in current_wl:
            sid = item.get("stock_id", "")
            if sid not in statuses:
                continue
            sid_state = _wl_state.get(sid, {})
            base = sid_state.get("base_price")
            if base in (None, "", 0, 0.0):
                base = item.get("entry_price")
            try:
                base_f = float(base) if base not in (None, "", 0, 0.0) else None
            except Exception:
                base_f = None
            if base_f and base_f > 0:
                rois.append((statuses[sid]["current"] / base_f - 1) * 100)
        avg_roi = sum(rois) / len(rois) if rois else None

        # ----- 摘要面板 (4 顆 metric, 紅漲綠跌) -----
        st.markdown("**自選股今日表現**")
        cMs1, cMs2, cMs3, cMs4 = st.columns(4)
        # 上漲 (紅)
        cMs1.markdown(
            f"<div style='background:#ffebee; padding:12px; border-radius:8px; "
            f"text-align:center; border-top:3px solid #d32f2f;'>"
            f"<div style='color:#888; font-size:0.85em;'>上漲</div>"
            f"<div style='color:#c62828; font-size:1.8em; font-weight:bold; line-height:1.1;'>{up_count}</div>"
            f"<div style='color:#aaa; font-size:0.75em;'>檔</div></div>",
            unsafe_allow_html=True,
        )
        # 下跌 (綠)
        cMs2.markdown(
            f"<div style='background:#e8f5e9; padding:12px; border-radius:8px; "
            f"text-align:center; border-top:3px solid #388e3c;'>"
            f"<div style='color:#888; font-size:0.85em;'>下跌</div>"
            f"<div style='color:#2e7d32; font-size:1.8em; font-weight:bold; line-height:1.1;'>{down_count}</div>"
            f"<div style='color:#aaa; font-size:0.75em;'>檔 (持平 {flat_count})</div></div>",
            unsafe_allow_html=True,
        )
        # 平均今日 %
        avg_color = "#c62828" if avg_today > 0.005 else ("#2e7d32" if avg_today < -0.005 else "#616161")
        avg_bg = "#ffebee" if avg_today > 0.005 else ("#e8f5e9" if avg_today < -0.005 else "#f5f5f5")
        avg_border = "#d32f2f" if avg_today > 0.005 else ("#388e3c" if avg_today < -0.005 else "#9e9e9e")
        avg_sign = "+" if avg_today > 0 else ""
        cMs3.markdown(
            f"<div style='background:{avg_bg}; padding:12px; border-radius:8px; "
            f"text-align:center; border-top:3px solid {avg_border};'>"
            f"<div style='color:#888; font-size:0.85em;'>平均今日</div>"
            f"<div style='color:{avg_color}; font-size:1.6em; font-weight:bold; line-height:1.1;'>{avg_sign}{avg_today:.2f}%</div>"
            f"<div style='color:#aaa; font-size:0.75em;'>vs 昨收</div></div>",
            unsafe_allow_html=True,
        )
        # 平均投報率
        if avg_roi is not None:
            roi_color = "#c62828" if avg_roi > 0.005 else ("#2e7d32" if avg_roi < -0.005 else "#616161")
            roi_bg = "#ffebee" if avg_roi > 0.005 else ("#e8f5e9" if avg_roi < -0.005 else "#f5f5f5")
            roi_border = "#d32f2f" if avg_roi > 0.005 else ("#388e3c" if avg_roi < -0.005 else "#9e9e9e")
            roi_sign = "+" if avg_roi > 0 else ""
            cMs4.markdown(
                f"<div style='background:{roi_bg}; padding:12px; border-radius:8px; "
                f"text-align:center; border-top:3px solid {roi_border};'>"
                f"<div style='color:#888; font-size:0.85em;'>平均投報率</div>"
                f"<div style='color:{roi_color}; font-size:1.6em; font-weight:bold; line-height:1.1;'>{roi_sign}{avg_roi:.2f}%</div>"
                f"<div style='color:#aaa; font-size:0.75em;'>vs 基準價</div></div>",
                unsafe_allow_html=True,
            )
        else:
            cMs4.markdown(
                f"<div style='background:#f5f5f5; padding:12px; border-radius:8px; "
                f"text-align:center; border-top:3px solid #9e9e9e;'>"
                f"<div style='color:#888; font-size:0.85em;'>平均投報率</div>"
                f"<div style='color:#888; font-size:1.0em; padding-top:6px;'>—</div>"
                f"<div style='color:#aaa; font-size:0.75em;'>(尚無基準)</div></div>",
                unsafe_allow_html=True,
            )

        st.markdown("")  # 空行

        # ----- 個股卡片 (紅漲綠跌) -----
        st.markdown("**個股表現**")
        for item in current_wl:
            sid = item.get("stock_id", "")
            nm = item.get("name", "")
            mk = item.get("market", "TW")
            ep = item.get("entry_price")
            added = item.get("added_date", "")

            sid_state = _wl_state.get(sid, {})
            base_p = sid_state.get("base_price")
            base_src = sid_state.get("base_source", "—")
            last_b = sid_state.get("last_pct", 0)

            # 取今日資料
            stat = statuses.get(sid)
            if stat:
                today_pct = stat["today_pct"]
                current_p = stat["current"]
                prev_close = stat["prev_close"]
            else:
                today_pct = 0
                current_p = None
                prev_close = None

            # 顏色 (亞洲: 紅漲綠跌)
            if today_pct > 0.005:
                bg = "#ffebee"; border = "#d32f2f"; tc = "#c62828"; sign = "+"
            elif today_pct < -0.005:
                bg = "#e8f5e9"; border = "#388e3c"; tc = "#2e7d32"; sign = ""
            else:
                bg = "#f5f5f5"; border = "#9e9e9e"; tc = "#616161"; sign = ""

            # 投報率 (vs base_price)
            roi_str = ""
            base_for_roi = base_p if base_p not in (None, "", 0, 0.0) else ep
            try:
                base_for_roi = float(base_for_roi) if base_for_roi not in (None, "", 0, 0.0) else None
            except Exception:
                base_for_roi = None
            if current_p is not None and base_for_roi and base_for_roi > 0:
                roi_pct = (current_p / base_for_roi - 1) * 100
                roi_sign = "+" if roi_pct > 0 else ""
                roi_color = "#c62828" if roi_pct > 0.005 else ("#2e7d32" if roi_pct < -0.005 else "#616161")
                roi_str = (
                    f" · 投報率 <b style='color:{roi_color};'>{roi_sign}{roi_pct:.2f}%</b>"
                    f" <span style='color:#888;font-size:0.85em;'>(基準 {round(base_for_roi,2)})</span>"
                )

            # sub line (基準/觸發/加入日)
            sub_parts = []
            if ep:
                sub_parts.append(f"入場 {ep}")
            if base_p:
                src_zh = "入場" if base_src == "entry" else ("自動" if base_src == "auto" else "—")
                sub_parts.append(f"基準 {round(float(base_p),2)} ({src_zh})")
            if last_b:
                sub_parts.append(f"上次觸發 {last_b:+.1f}%")
            if not base_p:
                sub_parts.append("尚未開始監控")
            if added:
                sub_parts.append(f"加入 {added}")
            sub_str = " · ".join(sub_parts) if sub_parts else ""

            # 卡片內容
            if current_p is not None:
                head_line = (
                    f"<b style='font-size:1.05em;'>{sid}</b> {nm} "
                    f"<span style='color:#888;font-size:0.85em;'>({mk})</span>"
                )
                price_line = (
                    f"現價 <b>{current_p:.2f}</b>"
                    f" <span style='color:{tc};font-weight:bold;'>{sign}{today_pct:.2f}%</span>"
                    f" <span style='color:#888;font-size:0.85em;'>(昨收 {prev_close:.2f})</span>"
                    f"{roi_str}"
                )
            else:
                head_line = (
                    f"<b style='font-size:1.05em;'>{sid}</b> {nm} "
                    f"<span style='color:#888;font-size:0.85em;'>({mk})</span>"
                )
                price_line = "<span style='color:#888;'>抓不到即時股價</span>"

            cardA, cardB = st.columns([7, 3])
            with cardA:
                st.markdown(
                    f"<div style='background:{bg}; padding:10px 14px; border-radius:6px; "
                    f"border-left:5px solid {border}; margin-bottom:8px;'>"
                    f"<div style='color:#222;'>{head_line}</div>"
                    f"<div style='color:#333; margin-top:3px;'>{price_line}</div>"
                    f"<div style='color:#888; font-size:0.82em; margin-top:3px;'>{sub_str}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            with cardB:
                bcol1, bcol2 = st.columns(2)
                with bcol1:
                    if st.button("重設基準", key=f"reset_{sid}",
                                 help="清掉目前 base，下次監控用當前價或入場價重設"):
                        watchlist_alerts.reset_watchlist_baseline(sid)
                        st.toast(f"已重設 {sid} 基準價", icon="✅")
                        st.rerun()
                with bcol2:
                    if st.button("刪除", key=f"del_{sid}"):
                        watchlist_store.remove_from_watchlist(sid)
                        st.rerun()

    st.divider()

    # ===== 自訂警報門檻 =====
    with st.expander("⚙️ 警報門檻設定 (自選股)", expanded=False):
        st.caption("設定自選股盤中觸發門檻 (跟今日開盤或昨收較極端的那個)。 一個方向 × 門檻 一天最多觸發 1 次。")
        cur_tw = watchlist_alerts.get_thresholds_for("TW")
        cur_us = watchlist_alerts.get_thresholds_for("US")
        tcfg1, tcfg2 = st.columns(2)
        with tcfg1:
            st.markdown("**台股 (TW)**")
            tw1 = st.number_input("第一門檻 %", min_value=0.5, max_value=30.0,
                                   value=float(cur_tw[0] if len(cur_tw) > 0 else 5.0),
                                   step=0.5, key="thr_tw_1")
            tw2 = st.number_input("第二門檻 %", min_value=0.5, max_value=30.0,
                                   value=float(cur_tw[1] if len(cur_tw) > 1 else 10.0),
                                   step=0.5, key="thr_tw_2")
        with tcfg2:
            st.markdown("**美股 (US)**")
            us1 = st.number_input("第一門檻 %", min_value=0.5, max_value=30.0,
                                   value=float(cur_us[0] if len(cur_us) > 0 else 5.0),
                                   step=0.5, key="thr_us_1")
            us2 = st.number_input("第二門檻 %", min_value=0.5, max_value=30.0,
                                   value=float(cur_us[1] if len(cur_us) > 1 else 10.0),
                                   step=0.5, key="thr_us_2")
        if st.button("儲存門檻", use_container_width=True, key="save_thresholds"):
            ok1 = watchlist_alerts.save_thresholds_for("TW", [tw1, tw2])
            ok2 = watchlist_alerts.save_thresholds_for("US", [us1, us2])
            if ok1 and ok2:
                st.success(f"已儲存。TW: ±{tw1}% / ±{tw2}% · US: ±{us1}% / ±{us2}%")
            else:
                st.error("儲存失敗 (檢查 Google Sheets 設定)")

    st.markdown("**警報設定**")
    st.markdown("""
- 自選股 (vs 今日開盤 或 vs 昨收, 取較極端的當主):
  - 預設兩個門檻: ±5%、±10% (可在上方自訂)
  - 一天 4 個 bucket (5%↑、10%↑、5%↓、10%↓), 每個最多觸發 1 次
  - 訊息含主錨點 + 對照另一個錨點
- 大盤監控 (該市場休市/閉市時段自動跳過):
  - 日經 225 每 ±150 點
  - 韓國 KOSPI 每 ±50 點
  - 台灣加權 每 ±100 點
  - 費城半導體 SOX 每 ±100 點 (~1.7%, 台股 leading)
  - 那斯達克 IXIC 每 ±200 點 (~1%)
- 加密貨幣 (BTC / ETH):
  - 固定推播: 台北 12:00 / 23:00 各一次, 比對「跟上一個 slot 推播相比」
  - 盤中加碼: 同 slot 內 (約 30 分後) 若漲跌再 ±2.5%+ 才會發第二次, 標題會標「盤中變動警報」
- 連續同方向觸發 → 加上 [連續警示]，提醒台股可能要減碼

cron 每 30 分執行一次 (24x7)。
""")

    if st.button("🔍 立即手動檢查警報", use_container_width=True, key="manual_check_alerts"):
        with st.spinner("檢查中..."):
            try:
                import index_alerts
                wl_alerts = watchlist_alerts.check_watchlist_alerts()
                idx_alerts = index_alerts.check_index_alerts()
                cry_alerts = index_alerts.check_crypto_alerts()
                st.session_state["last_manual_alerts"] = {
                    "wl": wl_alerts, "idx": idx_alerts, "cry": cry_alerts,
                }
            except Exception as e:
                st.error(f"檢查失敗: {e}")

    last_alerts = st.session_state.get("last_manual_alerts")
    if last_alerts:
        wl = last_alerts.get("wl", [])
        idx = last_alerts.get("idx", [])
        cry = last_alerts.get("cry", [])
        if wl or idx or cry:
            st.markdown("**手動檢查結果**")
            if wl:
                st.markdown("自選股觸發:")
                for a in wl:
                    st.text(f"  {a['stock_id']} {a['name']}: {a['current']} ({a['primary_pct']:+.2f}%) 觸發 {int(a['threshold'])}%")
            if idx:
                st.markdown("大盤觸發:")
                for a in idx:
                    st.text(f"  {a['name']}: {a['current']} ({a['diff']:+.0f} 點) 觸發 {int(a['threshold_bucket'])}")
            if cry:
                st.markdown("加密貨幣觸發:")
                for a in cry:
                    st.text(f"  {a['name']}: ${a['current']} ({a['change_pct']:+.2f}%) 觸發 {a.get('threshold_pct', 2.5)}%")
            if st.button("Send 警報 to TG", use_container_width=True, key="send_alerts_tg",
                          disabled=not notifier.is_configured()):
                msg = notifier.fmt_monitor_alerts(wl, idx, cry)
                _send_tg(msg, "盤中警報")
        else:
            st.info("目前無新觸發警報")


# =============================================================================
# Tab — 持倉分析 (15 檔 TW, 每天 15:00 完整 Gemini 分析 + 推播)
# =============================================================================
with tab_hold:
    st.subheader("持倉分析 (台股, 上限 15 檔)")
    st.caption(
        "管理已持有的台股. 盤後 15:00 cron 會對每檔做完整分析: "
        "技術 + 籌碼 + 新聞 + Gemini 給「持有/加碼/減碼/出清」建議, "
        "含短中期目標價 + 停損 + 隔日漲機率, 並 TG 推播."
    )

    import holdings_store
    import holdings_analyzer
    import holdings_tracker

    current_h = holdings_store.load_holdings()

    # 準確率摘要 (歷史) — 在頂端顯示
    try:
        acc = holdings_tracker.accuracy_summary(lookback_days=30)
        if acc and acc.get("total"):
            cAa, cAb, cAc = st.columns(3)
            cAa.metric("過去 30 天預測", f"{acc['accuracy_pct']}%",
                       delta=f"{acc['correct']}/{acc['total']} 次", delta_color="off")
            high_prob = acc.get("by_prob_range", {}).get(">=70%", {})
            if high_prob.get("total"):
                hp_pct = round(high_prob["correct"] / high_prob["total"] * 100, 1)
                cAb.metric("高信心預測 (≥70%)", f"{hp_pct}%",
                           delta=f"{high_prob['correct']}/{high_prob['total']}",
                           delta_color="off")
    except Exception:
        pass

    cH1, cH2 = st.columns(2)
    with cH1:
        st.markdown("**新增持倉**")
        h_sid = st.text_input("代號", placeholder="例 2330", key="h_sid")
        h_name = st.text_input("名稱 (可選)", placeholder="例 台積電", key="h_name")
        h_entry = st.number_input("進場價 (可選)", min_value=0.0, value=0.0, step=0.5, key="h_entry")
        h_shares = st.number_input("持有張數 (可選)", min_value=0, value=0, step=1, key="h_shares")
        h_note = st.text_input("備註 (可選)", placeholder="例 AI 主軸長線持有", key="h_note")
        if st.button("加入持倉", use_container_width=True, type="primary",
                      key="h_add", disabled=len(current_h) >= 15):
            if h_sid.strip():
                ep = float(h_entry) if h_entry > 0 else None
                sh = int(h_shares) if h_shares > 0 else None
                ok = holdings_store.add_holding(
                    h_sid.strip().upper(), h_name.strip(), ep, sh, h_note.strip()
                )
                if ok:
                    st.success(f"已加入 {h_sid}")
                    st.rerun()
                else:
                    st.error("加入失敗 (可能已達 15 檔上限)")

    with cH2:
        st.markdown(f"**目前持倉 ({len(current_h)} / 15)**")
        if not current_h:
            st.info("還沒新增, 從左邊加入.")
        else:
            for it in current_h:
                sid = it.get("stock_id", "")
                nm = it.get("name", "")
                ep = it.get("entry_price")
                sh = it.get("shares")
                note = it.get("note", "")
                added = it.get("added_date", "")
                cR1, cR2 = st.columns([7, 2])
                with cR1:
                    info_str = f"**{sid}** {nm}"
                    sub_parts = []
                    if ep: sub_parts.append(f"進場 {ep}")
                    if sh: sub_parts.append(f"{sh} 張")
                    if added: sub_parts.append(f"加入 {added}")
                    if note: sub_parts.append(note)
                    if sub_parts:
                        info_str += "  \n<span style='color:#888;font-size:0.85em'>" + " · ".join(sub_parts) + "</span>"
                    st.markdown(info_str, unsafe_allow_html=True)
                with cR2:
                    if st.button("移除", key=f"hdel_{sid}"):
                        holdings_store.remove_holding(sid)
                        st.rerun()

    st.divider()
    st.markdown("**立即執行分析**")
    cBT1, cBT2 = st.columns([1, 1])
    with cBT1:
        run_analysis = st.button("跑一次 Gemini 分析", use_container_width=True, type="primary",
                                  key="h_run_analysis", disabled=not current_h)
    with cBT2:
        send_h_tg = st.button("推到 TG", use_container_width=True, key="h_send_tg",
                               disabled=not (current_h and notifier.is_configured()))

    if run_analysis:
        with st.spinner("分析中…(每檔約 5-10 秒)"):
            try:
                results = holdings_analyzer.analyze_all_holdings()
                st.session_state["h_results"] = results
            except Exception as e:
                st.error(f"分析失敗: {e}")

    h_results = st.session_state.get("h_results", [])
    if h_results:
        # 摘要表
        rows = []
        for h in h_results:
            tech = h.get("tech", {}) or {}
            adv = h.get("advice", {}) or {}
            ep = h.get("entry_price")
            cur = tech.get("current", 0)
            roi = ((cur / ep - 1) * 100) if (ep and ep > 0) else None
            rows.append({
                "代號": h["stock_id"],
                "名稱": h["name"],
                "現價": tech.get("current"),
                "今日%": tech.get("today_pct"),
                "ROI%": round(roi, 2) if roi is not None else None,
                "建議": adv.get("action"),
                "信心%": adv.get("confidence"),
                "隔日漲%": adv.get("next_day_up_prob"),
                "短期目標": adv.get("target_short"),
                "停損": adv.get("stop_loss"),
            })
        _show_table(pd.DataFrame(rows), market="TW")

        with st.expander("詳細分析", expanded=False):
            for h in h_results:
                tech = h.get("tech", {}) or {}
                chip = h.get("chip", {}) or {}
                adv = h.get("advice", {}) or {}
                news = h.get("news", []) or []
                cur = tech.get("current", 0)
                today = tech.get("today_pct", 0)
                action = adv.get("action", "持有")
                action_color = {
                    "持有": "#888",
                    "加碼": "#c62828",
                    "減碼": "#f57c00",
                    "出清": "#2e7d32",
                }.get(action, "#888")
                st.markdown(
                    f"<div style='padding:10px; border-left:4px solid {action_color}; "
                    f"margin-bottom:6px; background:#fafafa;'>"
                    f"<b>{h['stock_id']} {h['name']}</b> {cur} ({today:+.2f}%) · "
                    f"<b style='color:{action_color}'>{action}</b> "
                    f"信心 {adv.get('confidence',0)}% · 隔日漲 {adv.get('next_day_up_prob',50)}%<br>"
                    f"<span style='font-size:0.9em'>"
                    f"目標: 短 {adv.get('target_short','—')} / 中 {adv.get('target_mid','—')} · "
                    f"停損 {adv.get('stop_loss','—')}<br>"
                    f"理由: {adv.get('reason','—')}<br>"
                    f"風險: {adv.get('risks','—')}"
                    f"</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if news:
                    for n in news[:3]:
                        st.markdown(f"- [{n.get('title','')}]({n.get('link','')})")

    if send_h_tg:
        # Reentrancy guard: 避免使用者在 spinner 期間連點兩次, 同分析跑兩遍 + 推兩封
        if st.session_state.get("h_sending"):
            st.info("推播作業進行中, 請稍候…")
        else:
            st.session_state["h_sending"] = True
            try:
                if not h_results:
                    with st.spinner("先跑一次分析..."):
                        try:
                            h_results = holdings_analyzer.analyze_all_holdings()
                            st.session_state["h_results"] = h_results
                        except Exception as e:
                            st.error(f"分析失敗: {e}")
                if h_results:
                    _send_tg(notifier.fmt_holdings_daily(h_results), "持倉日報")
            finally:
                st.session_state["h_sending"] = False


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
        # 異常推播 (每天最多一次，跨 session 共享; 假日不推, 推送失敗回滾去重)
        if auto_send_on_alert and notifier.is_configured():
            _is_tw_closed = False
            try:
                import holiday_check
                _is_tw_closed = holiday_check.is_market_closed_today("TW")
            except Exception:
                pass
            if dt.date.today().weekday() >= 5:
                _is_tw_closed = True
            # 用戶選關: F&G + 台股情緒極值警報 TG 推播 (dashboard 仍會顯示)
            if False and not _is_tw_closed:
                alert = notifier.fmt_tw_pulse_alert(tw_pulse)
                if alert:
                    today_key = dt.date.today().isoformat()
                    direction = "low" if s <= 25 else "high"
                    dedup_key = f"tw_pulse_{today_key}_{direction}"
                    if _should_send_once(dedup_key):
                        ok, info = notifier.send_message(alert)
                        if not ok:
                            _release_send_once(dedup_key)

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
            progress_bar = st.progress(0, text="準備掃描…")
            def _update_progress(stage: str, pct: int):
                try:
                    progress_bar.progress(min(100, max(0, int(pct))), text=stage)
                except Exception:
                    pass

            res = tw_screener.run_all_screens(
                market=market_choice, params=tw_params,
                enabled=enabled_conditions, progress_cb=_update_progress,
            )
            progress_bar.empty()
            st.session_state["tw_result"] = res
            # 自動存 snapshot
            try:
                save_res = tracker.save_snapshot(res.get("combined"))
                if save_res.get("ok"):
                    st.toast(f"📈 已存追蹤 ({save_res['rows']} 檔, {save_res['backend']})", icon="💾")
            except Exception:
                pass
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

            # On-demand 催化劑按鈕（命中股可能很多，避免每次都打 Gemini）
            cCat1, cCat2 = st.columns([1, 3])
            with cCat1:
                load_catalyst_btn = st.button(
                    "💡 補上催化劑 (AI 推測上漲原因)",
                    key="tw_catalyst_btn",
                    use_container_width=True,
                )

            if load_catalyst_btn:
                top_n = min(20, len(view))
                with st.spinner(f"AI 分析前 {top_n} 檔上漲原因…"):
                    try:
                        records = view.head(top_n).to_dict("records")
                        cat_map = stock_catalyst.annotate_picks_with_catalysts(records, market="TW")
                        st.session_state["tw_catalyst_map"] = cat_map
                    except Exception as e:
                        st.error(f"催化劑分析失敗：{e}")

            cat_map_tw = st.session_state.get("tw_catalyst_map", {}) or {}
            view_with_cat = view.copy()
            if cat_map_tw:
                view_with_cat["催化劑"] = view_with_cat["stock_id"].astype(str).map(cat_map_tw).fillna("")

            base_cols = ["stock_id", "stock_name", "market", "hit_count", "hits_label"]
            extra_cols = ["現價", "今日%", "今日量", "量比", "投信今日(張)", "投信5日(張)", "投本比%"]
            if cat_map_tw:
                extra_cols.append("催化劑")
            show_cols = base_cols + [c for c in extra_cols if c in view_with_cat.columns]
            display = view_with_cat[show_cols].rename(columns={
                "stock_id": "代號", "stock_name": "名稱", "market": "市場",
                "hit_count": "命中數", "hits_label": "命中條件",
            })
            _show_table(display, market="TW")

            # ===== Watchlist 命中即推送 =====
            if watchlist and auto_alert_watchlist and notifier.is_configured():
                hit_in_wl = view[view["stock_id"].isin(watchlist)]
                if not hit_in_wl.empty:
                    # 以日期+命中清單組指紋，跨 session 去重
                    wl_fp = "wl_" + latest_str + "_" + ",".join(sorted(hit_in_wl["stock_id"].astype(str).tolist()))
                    if _should_send_once(wl_fp):
                        msgs = []
                        for _, row in hit_in_wl.iterrows():
                            msgs.append(notifier.fmt_watchlist_alert(
                                row["stock_id"], row.get("stock_name", ""),
                                row.get("hit", []), latest_str,
                                row=row.to_dict(),
                            ))
                        ok, info = notifier.send_message("\n\n".join(msgs))
                        if ok:
                            st.toast(f"✈️ Watchlist 命中 {len(hit_in_wl)} 檔已推送", icon="🔔")
                        else:
                            # 推送失敗 → 回滾去重 key, 下次 rerun 還可重試
                            _release_send_once(wl_fp)
                            st.warning(f"Watchlist 推送失敗 (已回滾去重, 下次可重試): {info}")

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
                _send_tg(msg, "台股篩選")


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

    # === 開盤後 30 分鐘分析 (台股 + 美股) ===
    st.markdown("---")
    st.markdown("### ⏰ 開盤後 30 分鐘 · 資金流向分析")
    st.caption(
        "台股每天 09:30、美股每天 22:00（台灣時間）由 GitHub Actions 自動跑，"
        "推送到 Telegram。也可以手動立刻跑：")
    cO1, cO2 = st.columns(2)
    with cO1:
        tw_open_btn = st.button("🇹🇼 立即跑台股開盤分析", use_container_width=True,
                                 key="tw_open_btn", type="primary")
    with cO2:
        us_open_btn = st.button("🇺🇸 立即跑美股開盤分析", use_container_width=True,
                                 key="us_open_btn", type="primary")

    if tw_open_btn:
        try:
            with st.spinner("計算台股開盤資金流向…"):
                data = market_open_picks.get_tw_open_picks()
            st.session_state["tw_open_data"] = data
        except Exception as e:
            st.error(f"台股分析失敗：{e}")

    if us_open_btn:
        try:
            with st.spinner("計算美股開盤資金流向…"):
                data = market_open_picks.get_us_open_picks()
            st.session_state["us_open_data"] = data
        except Exception as e:
            st.error(f"美股分析失敗：{e}")

    # 顯示台股開盤結果
    tw_open = st.session_state.get("tw_open_data")
    if tw_open and not tw_open.get("error"):
        # 大盤預測區塊
        pred = tw_open.get("prediction") or {}
        acc = tw_open.get("accuracy") or {}
        if pred and not pred.get("error"):
            cP1, cP2, cP3 = st.columns([2, 1, 1])
            cP1.metric("🎯 大盤預測", pred.get("pattern", "—"),
                        delta=pred.get("bias", ""))
            cP2.metric("信心度", pred.get("confidence", "—"))
            if acc and acc.get("n"):
                cP3.metric("過去 30d 準確率", f"{acc['accuracy_pct']}%",
                            delta=f"{acc['correct']}/{acc['n']} 次", delta_color="off")
            st.caption(f"{pred.get('explanation','')}  ｜ 開盤跳空 {pred.get('gap_pct',0):+.2f}%、30 分走勢 {pred.get('drift_pct',0):+.2f}%、量比 {pred.get('vol_ratio',1):.1f}x")

        st.markdown("#### 🇹🇼 台股 — 資金主流前 3 族群")
        themes_df = tw_open.get("themes")
        if themes_df is not None and not themes_df.empty:
            _show_table(themes_df, market="TW")
        catalysts_tw = tw_open.get("catalysts", {})
        for p in tw_open.get("picks", []):
            theme = p["theme"]
            stocks = p["stocks"]
            if stocks is None or stocks.empty:
                continue
            with st.expander(f"[{theme}] 動能潛在股 (3 檔)", expanded=True):
                show_cols = [c for c in ["stock_id", "stock_name", "現價", "今日%", "5日%", "量比", "score"]
                              if c in stocks.columns]
                _show_table(stocks[show_cols], market="TW")
                # 催化劑
                if catalysts_tw:
                    for _, row in stocks.iterrows():
                        sid = str(row.get("stock_id", ""))
                        cat = catalysts_tw.get(sid)
                        if cat:
                            st.markdown(f"💡 **{sid} {row.get('stock_name','')}** — {cat}")
        # AI 觀點 + 推送
        if ai_analyzer.gemini_available():
            if st.button("🤖 加 Gemini 觀點", key="tw_open_ai", use_container_width=True):
                with st.spinner("Gemini 分析中…"):
                    from scripts.market_open_alert import _summarize_tw_for_ai
                    ok, ai_text = ai_analyzer.analyze_open_picks("TW", _summarize_tw_for_ai(tw_open))
                if ok:
                    st.markdown("##### 🤖 Gemini 觀點")
                    st.markdown(ai_text)
                    st.session_state["tw_open_ai"] = ai_text
                else:
                    st.error(ai_text)
        if st.button("✈️ Send 台股開盤分析 to TG", key="tw_open_send",
                      disabled=not notifier.is_configured(), use_container_width=True):
            ai_text = st.session_state.get("tw_open_ai", "")
            _send_tg(notifier.fmt_tw_open_picks(tw_open, ai_text=ai_text), "台股開盤分析")

    # 顯示美股開盤結果
    us_open = st.session_state.get("us_open_data")
    if us_open and not us_open.get("error"):
        pred = us_open.get("prediction") or {}
        acc = us_open.get("accuracy") or {}
        if pred and not pred.get("error"):
            cQ1, cQ2, cQ3 = st.columns([2, 1, 1])
            cQ1.metric("🎯 大盤預測", pred.get("pattern", "—"),
                        delta=pred.get("bias", ""))
            cQ2.metric("信心度", pred.get("confidence", "—"))
            if acc and acc.get("n"):
                cQ3.metric("過去 30d 準確率", f"{acc['accuracy_pct']}%",
                            delta=f"{acc['correct']}/{acc['n']} 次", delta_color="off")
            st.caption(f"{pred.get('explanation','')}  ｜ 開盤跳空 {pred.get('gap_pct',0):+.2f}%、30 分走勢 {pred.get('drift_pct',0):+.2f}%、量比 {pred.get('vol_ratio',1):.1f}x")

        st.markdown("#### 美股 — 板塊輪動前 3")
        sectors_df = us_open.get("sectors")
        if sectors_df is not None and not sectors_df.empty:
            _show_table(sectors_df, market="US")
        catalysts_us = us_open.get("catalysts", {})
        for sp in us_open.get("sector_picks", []):
            sec = sp["sector"]
            stocks = sp["stocks"]
            if stocks is None or stocks.empty:
                continue
            with st.expander(f"[{sec}] 動能潛在股 (3 檔)", expanded=False):
                _show_table(stocks, market="US")
                if catalysts_us:
                    for _, row in stocks.iterrows():
                        sym = str(row.get("symbol", ""))
                        cat = catalysts_us.get(sym)
                        if cat:
                            st.markdown(f"**{sym}** — {cat}")
        growth = us_open.get("growth")
        if growth is not None and not growth.empty:
            st.markdown("##### 成長動能極強 / 近期 IPO Top 5")
            _show_table(growth, market="US")
            if catalysts_us:
                for _, row in growth.iterrows():
                    sym = str(row.get("symbol", ""))
                    cat = catalysts_us.get(sym)
                    if cat:
                        st.markdown(f"💡 **{sym}** — {cat}")

        if ai_analyzer.gemini_available():
            if st.button("🤖 加 Gemini 觀點", key="us_open_ai", use_container_width=True):
                with st.spinner("Gemini 分析中…"):
                    from scripts.market_open_alert import _summarize_us_for_ai
                    ok, ai_text = ai_analyzer.analyze_open_picks("US", _summarize_us_for_ai(us_open))
                if ok:
                    st.markdown("##### 🤖 Gemini 觀點")
                    st.markdown(ai_text)
                    st.session_state["us_open_ai"] = ai_text
                else:
                    st.error(ai_text)
        if st.button("✈️ Send 美股開盤分析 to TG", key="us_open_send",
                      disabled=not notifier.is_configured(), use_container_width=True):
            ai_text = st.session_state.get("us_open_ai", "")
            _send_tg(notifier.fmt_us_open_picks(us_open, ai_text=ai_text), "美股開盤分析")

    # === 潛伏題材股 ===
    stealth_data = st.session_state.get("stealth", {})
    stealth_df = stealth_data.get("stealth")
    if stealth_df is not None and not stealth_df.empty:
        st.markdown("### 🌱 潛伏題材股 (族群熱、本身還沒大漲、有量能跡象)")
        st.dataframe(stealth_df, use_container_width=True, hide_index=True)
        # 催化劑明細
        if "催化劑" in stealth_df.columns and stealth_df["催化劑"].astype(str).str.len().sum() > 0:
            with st.expander("💡 各檔上漲原因 / 催化劑", expanded=False):
                for _, row in stealth_df.iterrows():
                    sid = row.get("stock_id", "")
                    nm = row.get("stock_name", "")
                    cat = row.get("催化劑", "")
                    if cat:
                        st.markdown(f"- **{sid} {nm}** — {cat}")
        send_stealth_tg = st.button(
            "✈️ Send to TG", use_container_width=True, key="send_stealth_tg",
            disabled=not notifier.is_configured()
        )
        if send_stealth_tg and notifier.is_configured():
            _send_tg(
                notifier.fmt_stealth_picks(stealth_df, stealth_data.get("hot_themes")),
                "潛伏題材股",
            )

    # === 熱門題材區塊 ===
    themes_data = st.session_state.get("themes", {})
    themes_df = themes_data.get("themes")
    leaders_map = themes_data.get("leaders") or {}
    if themes_df is not None and not themes_df.empty:
        st.markdown("### 🔥 熱門題材熱度排行")
        st.dataframe(themes_df, use_container_width=True, hide_index=True)

        st.markdown("### 🎯 題材龍頭強勢股 (跨題材去重, 一檔只顯示一次)")
        seen_sids: set = set()
        for theme in themes_df["題材"].head(8):
            df = leaders_map.get(theme)
            if df is None or df.empty:
                continue
            # 把已在其他題材出現的去掉
            df_dedup = df[~df["stock_id"].astype(str).isin(seen_sids)].copy()
            if df_dedup.empty:
                continue  # 此題材的股票全都已在其他題材顯示過
            seen_sids.update(df_dedup["stock_id"].astype(str).tolist())
            with st.expander(f"{theme} ({len(df_dedup)} 檔)",
                              expanded=(themes_df.iloc[0]["題材"] == theme)):
                cols_want = ["stock_id", "stock_name", "現價", "今日%", "振幅%", "量比", "5日%"]
                cols_have = [c for c in cols_want if c in df_dedup.columns]
                show = df_dedup[cols_have].copy()
                show = show.rename(columns={"stock_id": "代號", "stock_name": "名稱"})
                # 數值欄位四捨五入到 2 位
                for num_col in ["現價", "今日%", "振幅%", "量比", "5日%"]:
                    if num_col in show.columns:
                        show[num_col] = pd.to_numeric(show[num_col], errors="coerce").round(2)
                _show_table(show, market="TW")

    # === 證交所產業分類區塊 ===
    pulse = st.session_state.get("pulse", {})
    sectors = pulse.get("sectors")
    leaders = pulse.get("leaders")
    if sectors is not None and not sectors.empty:
        st.markdown("### 證交所產業分類 Top 5")
        first_col = sectors.columns[0]
        top5 = sectors.head(5).copy()
        _show_table(
            top5.rename(columns={first_col: "產業", "avg_change": "平均%", "median_change": "中位%",
                                 "up_count": "上漲家數", "n": "樣本數", "up_ratio": "上漲比率"}),
            market="TW",
        )
        if leaders is not None and not leaders.empty:
            with st.expander("各產業龍頭 (前 5 名 + 盤中資訊)"):
                # E: 標註該 leader 是否已在「今日可行動」 Top 內
                _actionable = st.session_state.get("actionable_picks") or []
                _actionable_sids = {str(p.get("stock_id", "")) for p in _actionable if p.get("stock_id")}
                show_df = leaders.copy()
                if _actionable_sids:
                    show_df["在今日可行動"] = show_df["stock_id"].astype(str).map(
                        lambda s: "✨" if s in _actionable_sids else ""
                    )
                show_cols = [c for c in ["industry_category", "stock_id", "stock_name",
                                          "現價", "今日%", "振幅%", "量比", "5日%",
                                          "入場標籤", "在今日可行動"]
                             if c in show_df.columns]
                _show_table(show_df[show_cols], market="TW")

        # 異常觸發推播 (任何格式化錯誤都不能炸掉整個 app)
        # 加假日/週末 guard: 避免在台股休市時推前一交易日的舊資料
        if auto_send_on_alert and notifier.is_configured():
            try:
                # 假日 / 週末 → skip 自動推播 (但保留 dashboard 顯示)
                _is_tw_closed = False
                try:
                    import holiday_check
                    _is_tw_closed = holiday_check.is_market_closed_today("TW")
                except Exception:
                    pass
                if dt.date.today().weekday() >= 5:  # 週末
                    _is_tw_closed = True

                top1 = sectors.iloc[0]
                avg = float(top1.get("avg_change", 0) or 0)
                if pd.isna(avg):
                    avg = 0.0
                if not _is_tw_closed and avg >= 1.5:
                    today_key = dt.date.today().isoformat()
                    pulse_fp = f"strong_sector_{today_key}_{top1[first_col]}"
                    if _should_send_once(pulse_fp):
                        msg = notifier.fmt_strong_sectors(
                            sectors, leaders_map=leaders,
                            themes_df=themes_df, theme_leaders=leaders_map,
                        )
                        if msg:
                            ok, info = notifier.send_message(msg)
                            if ok:
                                st.toast("已推送強勢族群通知", icon="🚀")
                            else:
                                # 失敗 → 回滾 send_once 紀錄, 下次 rerun 還可以重試
                                _release_send_once(pulse_fp)
                                st.warning(f"強勢族群推播失敗 (已回滾去重): {info}")
            except Exception as _e:
                st.warning(f"強勢族群自動推播失敗 (略過): {type(_e).__name__}: {_e}")

    if (sectors is None or sectors.empty) and (themes_df is None or themes_df.empty):
        st.info("按上方按鈕開始分析 (盤前/休市時 yfinance 資料可能尚未更新)。")

    if send_pulse_tg and (sectors is not None and not sectors.empty):
        try:
            msg = notifier.fmt_strong_sectors(
                sectors, leaders_map=leaders, themes_df=themes_df, theme_leaders=leaders_map,
            )
            _send_tg(msg, "強勢族群")
        except Exception as _e:
            st.error(f"強勢族群推播失敗: {type(_e).__name__}: {_e}")

    # ========== 🔎 隱藏概念股探勘 (從新聞反向挖) ==========
    st.divider()
    st.markdown("### 🔎 隱藏概念股探勘")
    st.caption(
        "輸入題材關鍵字 (如 '矽光子' / 'AI 伺服器' / '低軌衛星'), "
        "掃近 60 天台股新聞, 找「實質有做但還沒上熱門名單」的隱藏受惠股. "
        "限制: 必須有新聞曝光才挖得到, 純內部研發無曝光的找不到."
    )
    cCF1, cCF2, cCF3 = st.columns([3, 1, 1])
    with cCF1:
        try:
            import concept_finder
            preset = concept_finder.PRESET_THEMES
        except Exception:
            preset = ["矽光子", "AI 伺服器", "低軌衛星"]
        cf_keyword = st.selectbox(
            "選預設題材, 或下方手動輸入",
            options=[""] + preset, key="cf_preset",
        )
        cf_custom = st.text_input(
            "或手動輸入 (覆蓋上方 select)",
            value="", key="cf_custom",
            placeholder="例: 玻璃基板, 矽智財, 衛星通訊...",
        )
    with cCF2:
        cf_days = st.number_input("掃幾天新聞", 14, 90, 60, step=7, key="cf_days")
    with cCF3:
        cf_btn = st.button("🔎 開始探勘", use_container_width=True,
                            type="primary", key="cf_btn",
                            help="掃 200 檔 × N 天新聞, 約 1-2 分鐘")

    if cf_btn:
        kw = (cf_custom.strip() or cf_keyword).strip()
        if not kw:
            st.warning("請選擇或輸入一個 keyword")
        else:
            with st.spinner(f"探勘「{kw}」概念股中... (掃 200 檔 × {cf_days} 天新聞)"):
                try:
                    import concept_finder
                    results = concept_finder.find_hidden_concept_stocks(
                        kw, days=int(cf_days), max_scan=200,
                    )
                    st.session_state["concept_results"] = results
                    st.session_state["concept_keyword"] = kw
                except Exception as e:
                    st.error(f"探勘失敗: {type(e).__name__}: {e}")

    cf_results = st.session_state.get("concept_results")
    if cf_results is not None:
        cf_kw = st.session_state.get("concept_keyword", "")
        hidden = [r for r in cf_results if r["hidden"]]
        known = [r for r in cf_results if not r["hidden"]]
        st.markdown(f"#### 「{cf_kw}」 探勘結果")
        st.caption(
            f"找到 {len(cf_results)} 檔有相關新聞 — "
            f"**🔎 {len(hidden)} 檔隱藏 / 📊 {len(known)} 檔已知**"
        )
        if hidden:
            st.markdown("##### 🔎 隱藏概念股 (未上 TW_THEMES 名單, 但有新聞曝光)")
            for r in hidden[:15]:
                with st.container(border=True):
                    cR1, cR2 = st.columns([3, 1])
                    with cR1:
                        st.markdown(
                            f"**`{r['stock_id']}` {r['name']}** "
                            f"[{r.get('industry','—')}]"
                        )
                        for t in r.get("recent_news_titles", [])[:3]:
                            st.text(f"  • {t}")
                    with cR2:
                        st.metric("提及次數", r["mentions"])
        if known:
            with st.expander(f"📊 已知概念股 ({len(known)} 檔, 已在 TW_THEMES 名單)"):
                df_known = pd.DataFrame([
                    {"代號": r["stock_id"], "名稱": r["name"],
                     "產業": r.get("industry", ""), "提及次數": r["mentions"]}
                    for r in known
                ])
                st.dataframe(df_known, use_container_width=True, hide_index=True)

        if not hidden and not known:
            st.info(f"沒找到「{cf_kw}」相關的股票新聞, 試試其他 keyword 或拉長 days.")


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
    cG1, cG2, cG3 = st.columns([1, 1, 1])
    with cG1:
        growth_btn = st.button("🔄 更新成長動能榜", use_container_width=True, type="primary")
    with cG2:
        send_growth_tg = st.button("✈️ Send to TG", use_container_width=True, key="send_growth",
                                    disabled=not notifier.is_configured())
    with cG3:
        # 強制清 cache — 若 FinMind 上次 cache 到空結果, 強制重抓
        if st.button("🗑️ 強制清 cache", use_container_width=True, key="growth_clear_cache",
                      help="若「沒反應」連續好幾次, 點這個強制清 Streamlit cache + yfinance cache"):
            try:
                st.cache_data.clear()
                import data_sources as _ds
                if hasattr(_ds, "_yf_cache_clear"):
                    _ds._yf_cache_clear()
                # 也清 session_state 的 growth 結果
                st.session_state.pop("growth", None)
                st.success("✅ Cache 已清空, 請再點「更新成長動能榜」")
            except Exception as _ce:
                st.error(f"清 cache 失敗: {_ce}")

    if growth_btn:
        try:
            with st.spinner("評估題材股 K 線健康度中…約 30 秒"):
                st.session_state["growth"] = news_picks.run_news_growth_picks(top_n=10)
        except Exception as e:
            import traceback
            st.error(f"成長動能分析失敗：{e}")
            st.code(traceback.format_exc(), language="python")

    growth = st.session_state.get("growth", {})
    picks = growth.get("picks")
    diagnostic = growth.get("diagnostic", "")
    stats = growth.get("stats", {})

    # 顯示 diagnostic — picks 空時自動展開, 否則折疊
    is_empty = picks is None or picks.empty
    if diagnostic and (growth_btn or is_empty):
        with st.expander("🔍 執行診斷 (找出為什麼沒結果)", expanded=is_empty):
            st.code(diagnostic, language=None)
            if stats:
                st.markdown(
                    f"**統計**: 候選池 {stats.get('n_universe', 0)} 檔 / "
                    f"抓到日線 {stats.get('n_daily_fetched', 0)} 檔 / "
                    f"K 線正分 {stats.get('n_with_score', 0)} 檔 / "
                    f"最終 picks {stats.get('n_picks', 0)} 檔"
                )

    if is_empty:
        if not growth_btn:
            st.info("按上方按鈕開始分析。")
        else:
            st.warning(
                "⚠️ 本次分析沒抓到任何 picks. 看上方「執行診斷」找原因. "
                "若是 FinMind / cache 問題, 點「強制清 cache」後重試."
            )
    else:
        st.dataframe(picks, use_container_width=True, hide_index=True)
        if "催化劑" in picks.columns and picks["催化劑"].astype(str).str.len().sum() > 0:
            with st.expander("💡 各檔上漲原因 / 催化劑", expanded=True):
                for _, row in picks.iterrows():
                    code = row.get("代號", "")
                    nm = row.get("名稱", "")
                    cat = row.get("催化劑", "")
                    if cat:
                        st.markdown(f"- **{code} {nm}** — {cat}")
        if send_growth_tg:
            _send_tg(notifier.fmt_growth_picks(picks), "成長動能 Top 10")


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

        # ========== 🔬 深度分析 (法說 / PE / 籌碼 / K 形態) ==========
        st.markdown("---")
        st.markdown("### 🔬 深度分析")
        if st.button("🔄 跑深度分析 (約 10-20 秒)", key=f"deep_btn_{sid}",
                      help="抓重大訊息+Gemini 摘要 / PE vs 同業 / 外資持股變化 / K 線形態",
                      use_container_width=False):
            with st.spinner("深度分析中..."):
                try:
                    import stock_deep_analyzer
                    deep = stock_deep_analyzer.get_deep_analysis(
                        sid, market="US" if full.get("is_us") else "TW",
                    )
                    st.session_state[f"deep_{sid}"] = deep
                except Exception as e:
                    st.error(f"深度分析失敗: {type(e).__name__}: {e}")
                    st.session_state[f"deep_{sid}"] = None

        deep = st.session_state.get(f"deep_{sid}")
        if deep:
            # 1. PE vs 同業 (台股才有)
            pe = deep.get("pe_peers") or {}
            if pe.get("stock_pe") is not None:
                cPE1, cPE2, cPE3, cPE4 = st.columns(4)
                cPE1.metric("本股 PE", f"{pe['stock_pe']}")
                cPE2.metric("同業中位數", f"{pe.get('peer_median_pe','—')}")
                cPE3.metric("本股 percentile", f"{pe.get('stock_percentile','—')}%")
                val = pe.get("valuation", "—")
                color = {"低估":"🟢","合理":"🟡","偏高":"🟠","極高":"🔴"}.get(val, "")
                cPE4.metric("估值", f"{color} {val}")
                if pe.get("context"):
                    st.caption(pe["context"])

            # 2. 外資持股比例變化
            h = deep.get("holdings") or {}
            if h and h.get("trend"):
                st.markdown("**🏛 籌碼變化**")
                if h.get("foreign_pct_now") is not None:
                    cH1, cH2, cH3 = st.columns(3)
                    cH1.metric("外資持股 (今)", f"{h['foreign_pct_now']:.2f}%")
                    cH2.metric("30 日變化",
                                f"{h.get('foreign_change_30d',0):+.2f}pp",
                                delta_color="normal")
                    cH3.metric("趨勢", h["trend"])
                    # 走勢 chart
                    if isinstance(h.get("history"), pd.DataFrame) and not h["history"].empty:
                        try:
                            chart_df = h["history"].set_index("date")[["foreign_pct"]]
                            st.line_chart(chart_df, height=200)
                        except Exception:
                            pass
                elif h.get("fi_30d_lots"):
                    cF1, cF2, cF3 = st.columns(3)
                    cF1.metric("外資 30d 累積",
                                f"{int(h['fi_30d_lots']):+,} 張")
                    cF2.metric("5d 累積", f"{int(h.get('fi_5d_lots',0)):+,} 張")
                    cF3.metric("趨勢", h["trend"])
                    if h.get("note"):
                        st.caption(f"⚠ {h['note']}")

            # 3. K 線形態
            cp = deep.get("candle_patterns") or {}
            patterns = cp.get("patterns") or []
            if cp.get("summary"):
                st.markdown("**🕯 K 線形態 (近 5 日)**")
                pCol1, pCol2 = st.columns([2, 1])
                with pCol1:
                    st.write(f"📊 {cp['summary']}")
                with pCol2:
                    st.caption(f"短期趨勢: **{cp.get('trend_context','—')}**")
                # badge 顯示每個 pattern
                if patterns:
                    badge_lines = []
                    for p in patterns:
                        di = p.get("day_index", 0)
                        day_label = "今天" if di == 0 else f"{di} 日前"
                        signal_color = "🟢" if "跌勢反轉" in p.get("signal", "") or "強勢" in p.get("signal", "") else \
                                        ("🔴" if "漲勢反轉" in p.get("signal", "") or "弱勢" in p.get("signal", "") else "🟡")
                        badge_lines.append(
                            f"- {signal_color} **{p['label']}** ({day_label}): {p.get('signal', '')}"
                        )
                    st.markdown("\n".join(badge_lines))

            # 4. 財報數據 (月營收 YoY + 季 EPS YoY)
            fund = deep.get("fundamentals") or {}
            if fund and (fund.get("monthly_revenue") or fund.get("latest_eps") is not None):
                st.markdown("**📈 財報數據**")
                cFund1, cFund2, cFund3, cFund4 = st.columns(4)
                # 最新月營收 YoY (擋 None + NaN)
                yoy_val = fund.get("latest_revenue_yoy")
                if yoy_val is not None and not (isinstance(yoy_val, float) and pd.isna(yoy_val)):
                    cFund1.metric("最新月營收 YoY", f"{yoy_val:+.2f}%",
                                    delta=fund.get("revenue_trend", "—"),
                                    delta_color="normal")
                # 最新 EPS + YoY
                eps_val = fund.get("latest_eps")
                if eps_val is not None and not (isinstance(eps_val, float) and pd.isna(eps_val)):
                    q = fund.get("latest_eps_quarter", "")
                    cFund2.metric(f"EPS ({q})", f"{eps_val}")
                    eps_yoy = fund.get("eps_yoy_pct")
                    if eps_yoy is not None and not (isinstance(eps_yoy, float) and pd.isna(eps_yoy)):
                        cFund3.metric("EPS YoY", f"{eps_yoy:+.2f}%",
                                        delta=fund.get("eps_trend", "—"))
                    elif fund.get("eps_trend"):
                        cFund3.caption(fund["eps_trend"])
                # 趨勢標籤
                if fund.get("revenue_trend"):
                    cFund4.caption(f"營收: {fund['revenue_trend'][:25]}")

                # 6 個月營收 chart
                if fund.get("monthly_revenue"):
                    try:
                        mr_df = pd.DataFrame(fund["monthly_revenue"])
                        if "yoy_pct" in mr_df.columns and mr_df["yoy_pct"].notna().any():
                            chart_df = mr_df.set_index("date")[["yoy_pct"]]
                            st.caption("近 6 月營收 YoY (%)")
                            st.bar_chart(chart_df, height=180)
                    except Exception:
                        pass

            # 5. 重大訊息 / 法說摘要 (含利多/利空 sentiment)
            ann = deep.get("announcements") or {}
            if ann.get("summary") or ann.get("count"):
                st.markdown("**📢 近期重大訊息 / 法說**")
                # 利多 / 利空 統計
                sb = ann.get("sentiment_breakdown") or {}
                if sb:
                    sb_cols = st.columns(3)
                    sb_cols[0].metric("🟢 利多訊息", sb.get("bullish", 0))
                    sb_cols[1].metric("🔴 利空訊息", sb.get("bearish", 0))
                    sb_cols[2].metric("⚪ 中性訊息", sb.get("neutral", 0))
                if ann.get("key_events"):
                    st.write(" · ".join(f"`{e}`" for e in ann["key_events"]))
                if ann.get("summary"):
                    st.info(f"🤖 Gemini 摘要: {ann['summary']}")
                if ann.get("raw_items"):
                    with st.expander(f"📋 原始訊息列表 ({ann.get('count',0)} 條)"):
                        for it in ann["raw_items"][:15]:
                            cat_emoji = "🏛" if it.get("category") == "重大訊息" else "📰"
                            sent = it.get("sentiment_label", "")
                            sent_emoji = {"利多":"🟢","利空":"🔴","中性":"⚪"}.get(sent, "")
                            st.text(f"{cat_emoji} {sent_emoji} {it.get('date','')} {it.get('title','')}")

        # ========== 💡 上漲催化劑 + 財報事件 ==========
        st.markdown("---")
        st.markdown("### 💡 為什麼可能會漲？")

        is_us_stock = full.get("is_us", False)
        market_label = "US" if is_us_stock else "TW"

        # 取近 30 天新聞 (TW 自動 fallback yfinance)
        try:
            news_list = stock_catalyst.fetch_news_for_stock(
                sid, market=market_label, max_items=10, days=30
            )
        except Exception as _e:
            news_list = []
            st.caption(f"⚠️ 新聞抓取錯誤: {_e}")

        # 取催化劑 (Gemini 批次 / 新聞 fallback)
        try:
            today_pct = float(full["daily"]["close"].iloc[-1] /
                              full["daily"]["close"].iloc[-2] - 1) * 100 if len(full["daily"]) >= 2 else 0.0
        except Exception:
            today_pct = 0.0

        try:
            catalyst_map = stock_catalyst.annotate_picks_with_catalysts(
                [{"stock_id": sid, "stock_name": full.get("name", ""), "今日%": round(today_pct, 2)}],
                market=market_label,
            )
            catalyst_text = catalyst_map.get(sid, "") if catalyst_map else ""
        except Exception:
            catalyst_text = ""

        if catalyst_text:
            if ai_analyzer.gemini_available():
                st.info(f"🤖 **AI 推測上漲原因**\n\n{catalyst_text}")
            else:
                st.info(f"📰 **近期重點消息**\n\n{catalyst_text}")
        else:
            st.warning("近期沒有抓到明顯的催化劑訊息。可以按下方「🤖 AI 深度」做更完整的分析。")

        # 財報行事曆
        try:
            ev = earnings_calendar.get_stock_events(sid, market=market_label)
        except Exception:
            ev = {}

        if ev and ev.get("summary") and ev["summary"] != "—":
            sentiment = ev.get("sentiment", "neutral")
            color_map = {"warn": "🔴", "caution": "🟡", "watch": "🟠", "neutral": "🟢"}
            icon = color_map.get(sentiment, "🟢")
            st.markdown(f"**📅 財報行事曆** {icon}")
            st.caption(ev["summary"])
            if ev.get("brief"):
                if sentiment in ("warn", "caution"):
                    st.warning(f"⚠️ {ev['brief']}")
                elif sentiment == "watch":
                    st.info(f"👀 {ev['brief']}")

        # 近期新聞 (含 sentiment 標籤)
        if news_list:
            # 補上 sentiment
            try:
                import news_sources
                news_list = news_sources.enrich_news_with_sentiment(
                    news_list, lang_default="zh" if market_label == "TW" else "en",
                )
                # Gemini 可用時翻譯成中文
                if ai_analyzer.gemini_available():
                    news_list = news_sources.translate_news_titles(news_list)
            except Exception:
                pass

            with st.expander(f"📰 近 30 天新聞 ({len(news_list)} 則)", expanded=True):
                for n in news_list[:10]:
                    title_orig = n.get("title", "")
                    title_zh = n.get("title_zh", title_orig)
                    has_translation = title_zh and title_zh != title_orig
                    link = n.get("link", "")
                    date = n.get("date", "")
                    src = n.get("source", "")
                    sent = n.get("sentiment", 0)
                    if sent > 0:
                        tag = "📈"
                    elif sent < 0:
                        tag = "📉"
                    else:
                        tag = "▪"
                    head = f"`[{date}]`" if date else ""
                    display = title_zh if has_translation else title_orig
                    if link:
                        line = f"- {tag} {head} [{display}]({link})"
                    else:
                        line = f"- {tag} {head} {display}"
                    line += f" <span style='color:#888;font-size:11px'>· {src}</span>"
                    if has_translation:
                        line += f"<br/><span style='color:#aaa;font-size:11px;font-style:italic;margin-left:24px'>{title_orig}</span>"
                    st.markdown(line, unsafe_allow_html=True)
        else:
            with st.expander("📰 近 30 天新聞 (0 則)", expanded=False):
                st.warning(
                    "📭 此股票近 30 天**無新聞紀錄**。可能原因：\n"
                    "- 中小型股 FinMind 新聞覆蓋率較低\n"
                    "- 該股近期確實沒有重大消息\n"
                    "- 您可以到 Goodinfo / 鉅亨網等網站手動查詢"
                )

        st.markdown("---")

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
                    # 把 tab_stock 下方深度分析結果一起餵進 AI prompt
                    # (如果使用者已按過「跑深度分析」按鈕)
                    deep_data = st.session_state.get(f"deep_{full['stock_id']}")
                    ok, ai_text = ai_analyzer.analyze(
                        stock_meta={
                            "stock_id": full["stock_id"], "name": full["name"],
                            "industry": full["industry"], "market": full["market"],
                        },
                        daily=full["daily"], ind=ind,
                        inst=full["inst"], margin=full["margin"],
                        hits=hits, score=score,
                        deep_analysis=deep_data,
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
            if st.button("Send AI 分析 to TG", key="send_ai_tg", use_container_width=True):
                _send_tg(
                    notifier.fmt_ai_analysis(sid, full["name"] or "", last_ai["text"]),
                    f"AI 分析 {sid}",
                    stock_id=sid,  # 附 inline 按鈕方便快速操作
                    market="TW",
                )

    # ================== 🖼️ 上傳 K 線圖讓 Gemini 分析 ==================
    st.divider()
    st.markdown("### 🖼️ 上傳 K 線圖 → AI 視覺分析")
    st.caption("拍/截圖任意股票走勢圖丟上來，Gemini 會結合恐慌指數和市場新聞做綜合判讀。")

    uploaded = st.file_uploader(
        "選擇圖片 (PNG / JPG, ≤ 5 MB)", type=["png", "jpg", "jpeg"],
        key="chart_upload",
    )
    extra_note = st.text_input(
        "備註 (可選 — 例如：這是 NVDA 日 K,您想知道現在能不能進場)",
        value="", key="chart_note",
    )

    # 檔案大小檢查 — 太大會撞 Gemini payload 上限或 OOM
    MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
    image_too_big = False
    if uploaded is not None:
        size = len(uploaded.getvalue())
        if size > MAX_IMAGE_SIZE_BYTES:
            image_too_big = True
            st.error(
                f"⚠ 圖片過大 ({size / 1024 / 1024:.1f} MB > 5 MB), Gemini 可能 OOM. "
                "請縮圖再上傳 (建議寬度 ≤ 1200px, 用 jpg 壓 80% 品質)."
            )

    cI1, cI2 = st.columns([1, 1])
    with cI1:
        analyze_image_btn = st.button(
            "🤖 用 Gemini 分析這張圖", use_container_width=True, type="primary",
            disabled=(not ai_analyzer.gemini_available()) or uploaded is None or image_too_big,
        )
    with cI2:
        send_image_tg = st.button(
            "✈️ Send 圖片分析 to TG", use_container_width=True,
            disabled=not notifier.is_configured(), key="send_image_tg",
        )

    if uploaded is not None and not image_too_big:
        st.image(uploaded, caption="上傳的圖片", use_column_width=True)

    if analyze_image_btn and uploaded is not None and not image_too_big:
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

    if send_image_tg and last_img:
        _send_tg(
            notifier.fmt_ai_analysis("圖片分析", "上傳圖片", last_img),
            "圖片 AI 分析",
        )


with tab_us:
    st.subheader("🇺🇸 美股 Top 10 可進場 (像台股今日可行動)")
    st.caption(
        "從 us_screener 候選池過篩, 篩 entry_score ≥ 55 的可進場個股, "
        "卡片含 3 層目標 / 進場區間 / 停損 / 財報日警示 / 同類股 ETF 強度. "
        "候選池可在 Streamlit secrets 加入 `US_WATCHLIST=AAPL,MSFT,...` 自訂。"
    )

    cA, cB = st.columns([1, 1])
    with cA:
        us_btn = st.button("🔄 更新美股 Top 10", use_container_width=True, type="primary")
    with cB:
        send_us_tg = st.button("✈️ Send to TG", use_container_width=True,
                               disabled=not notifier.is_configured(),
                               key="send_us_tg")

    if us_btn:
        try:
            with st.spinner("掃描美股可進場精選中…(約 60-90 秒)"):
                # F fix: us_pool 抓一次, 傳給 us_actionable 避免重複抓 (省 60-90s)
                _us_pool = us_screener.run_us_recommendation(top_n=20)
                st.session_state["us_result"] = _us_pool
                try:
                    import us_actionable as _ua
                    st.session_state["us_actionable"] = _ua.compute_us_actionable_picks(
                        top_n=10, min_score=65, us_pool=_us_pool,
                    )
                except Exception as _ae:
                    st.warning(f"actionable 精選失敗 (fallback table): {_ae}")
                    st.session_state["us_actionable"] = []
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

    # === 新: 卡片區 (entry_score >= 55 可進場精選) ===
    us_actionable_list = st.session_state.get("us_actionable") or []
    if us_actionable_list:
        st.markdown(f"### 🎯 可進場精選 ({len(us_actionable_list)} 檔)")
        for i, p in enumerate(us_actionable_list, 1):
            score = p.get("score", 0)
            score_color = "🟢" if score >= 6 else ("🟡" if score >= 4 else "🔴")
            entry_emoji = p.get("entry_emoji", "")
            entry_label = p.get("entry_label", "")
            label_str = f" · {entry_emoji} {entry_label}" if entry_label else ""
            with st.container(border=True):
                cH1, cH2 = st.columns([3, 1])
                with cH1:
                    st.markdown(
                        f"### {i}. `{p.get('symbol')}` {p.get('name', '')} "
                        f"{score_color}{label_str}"
                    )
                    if p.get("theme"):
                        st.caption(f"產業: {p.get('theme')} · 綜合分數 {score:.1f}")
                    if p.get("entry_score") is not None:
                        st.caption(
                            f"入場評分 {p['entry_score']}/100 → "
                            f"{p.get('entry_action', '—')}"
                        )
                with cH2:
                    if p.get("current") is not None:
                        today_pct = p.get("today_pct", 0) or 0
                        try:
                            today_pct = float(today_pct)
                        except (TypeError, ValueError):
                            today_pct = 0
                        st.metric("現價",
                                   f"${p['current']}",
                                   f"{today_pct:+.2f}%")
                cB1, cB2 = st.columns(2)
                with cB1:
                    if p.get("entry_low") and p.get("entry_high"):
                        st.write(f"**進場區間**: ${p['entry_low']} ~ ${p['entry_high']}")
                    if p.get("stop"):
                        rr = p.get("rr", "—")
                        st.write(f"**停損**: ${p['stop']} · **R:R**: {rr}")
                    # 3 層目標
                    cur_v = p.get("current") or 0
                    t_s = p.get("target_short")
                    t_m = p.get("target_mid")
                    t_l = p.get("target_long")
                    if t_s or t_m or t_l:
                        def _gain(t):
                            if t and cur_v:
                                return f"+{(t/cur_v-1)*100:.1f}%"
                            return ""
                        tlines = []
                        if t_s: tlines.append(f"短 ${t_s} ({_gain(t_s)})")
                        if t_m: tlines.append(f"中 ${t_m} ({_gain(t_m)})")
                        if t_l: tlines.append(f"長 ${t_l} ({_gain(t_l)})")
                        st.write("🎯 " + " / ".join(tlines))
                    if p.get("win_prob"):
                        st.write(f"**上漲機率**: {p['win_prob']} · 持有 {p.get('hold_period','—')}")
                with cB2:
                    if p.get("pe_label"):
                        st.write(f"**PE**: {p['pe_label']}")
                    if p.get("forward_pe"):
                        st.write(f"**Fwd PE**: {p['forward_pe']:.1f}")
                    if p.get("eps") is not None:
                        st.write(f"**EPS**: {p['eps']}")
                    if p.get("marketcap_str") and p["marketcap_str"] != "—":
                        st.write(f"**市值**: {p['marketcap_str']}")
                    if p.get("earnings_date"):
                        st.write(f"📅 **下次財報**: {p['earnings_date']}")
                    if p.get("sector_etf"):
                        # D fix: 防 None value 不被 default 救
                        _etf_pct = p.get("sector_etf_5d_pct")
                        _etf_pct_str = f"{_etf_pct:+.2f}%" if _etf_pct is not None else "—"
                        st.write(
                            f"🏷 同類股 ETF **{p['sector_etf']}** "
                            f"5d {_etf_pct_str}"
                        )
                # Reasons
                for r in (p.get("reasons") or [])[:4]:
                    st.markdown(f"- ✅ {r}")
                # Warnings
                for w in (p.get("warnings") or [])[:3]:
                    st.markdown(f"- ⚠️ {w}")
        st.caption("⚠️ 僅供參考, 不構成投資建議. 請自行做研究與風控.")

    # === 完整候選池 (含 entry_score < 55 的票) — 收摺疊 ===
    if top_picks is not None and not top_picks.empty:
        with st.expander(f"📊 完整候選池 ({len(top_picks)} 檔, 含 WAIT/AVOID)", expanded=False):
            show_df = top_picks.drop(columns=["近期新聞"], errors="ignore")
            _show_table(show_df, market="US")
    elif not us_actionable_list:
        st.info("資料抓取中或無命中標的，請稍後再試。")
    # G fix: 移除 if False 死碼, 改成正常 if top_picks 條件
    if top_picks is not None and not top_picks.empty:
        # 催化劑顯示
        if "催化劑" in top_picks.columns and top_picks["催化劑"].astype(str).str.len().sum() > 0:
            with st.expander("💡 各檔上漲原因 / 催化劑", expanded=False):
                for _, row in top_picks.iterrows():
                    sym = row.get("symbol", "")
                    cat = row.get("催化劑", "")
                    if cat:
                        st.markdown(f"- **{sym}** — {cat}")
        # 強制清 cache 按鈕
        if st.button("🗑️ 清新聞 cache", key="us_news_clear",
                      help="若新聞欄位顯示空白, 點此清掉 streamlit cache 強制重抓"):
            st.cache_data.clear()
            st.session_state.pop("us_result", None)
            st.session_state.pop("us_actionable", None)
            st.success("✅ Cache 已清, 請重新按「更新美股 Top 10」")

        with st.expander("📰 候選個股近期新聞 / 題材", expanded=False):
            for _, row in top_picks.iterrows():
                sym = row.get("symbol", "")
                theme_v = row.get("題材") or "—"
                news_list = row.get("近期新聞") or []
                st.markdown(f"**{sym}** — 題材: {theme_v}  (新聞數: {len(news_list)})")
                shown = 0
                for n in news_list:
                    if not isinstance(n, dict):
                        continue
                    title = n.get("title")
                    if not title:
                        continue
                    link = n.get("link")
                    publisher = n.get("publisher", "")
                    if link:
                        st.markdown(f"- [{title}]({link}) · _{publisher}_")
                    else:
                        st.markdown(f"- {title} · _{publisher}_  _(無連結)_")
                    shown += 1
                if shown == 0:
                    st.caption("  _(該股無近期新聞)_")
                st.markdown("---")

        if send_us_tg:
            _send_tg(notifier.fmt_us_top_picks(top_picks, fg), "美股 Top 10")


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
                # 用獨立 key, 不覆蓋 tab_us 的 us_result
                st.session_state["us_result_mood"] = us_screener.run_us_recommendation(top_n=5)
        except Exception as e:
            st.error(f"抓取失敗：{e}")
    # 優先用 mood 自己的, 沒按過時 fallback 拿 tab_us 的 (避免重抓燒 quota)
    us = st.session_state.get("us_result_mood") or st.session_state.get("us_result", {}) or {}
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

    # 美股 F&G 異常觸發 — 用戶選關 TG 推播 (dashboard 仍會顯示 st.warning)
    if fg and fg.get("score") is not None:
        alert_msg = notifier.fmt_fear_greed_alert(fg)
        if alert_msg:
            st.warning(alert_msg, icon="⚠️")
            # TG 推播已關閉 (用戶選: 心理指標, 組訊息量低)

    # 台股 F&G 異常 — 用戶選關 TG 推播 (dashboard 仍會顯示 st.warning)
    if tw_pulse and tw_pulse.get("score") is not None:
        tw_alert = notifier.fmt_tw_pulse_alert(tw_pulse)
        if tw_alert:
            st.warning(tw_alert, icon="⚠️")
            # TG 推播已關閉 (用戶選: 心理指標, 組訊息量低)

    if sectors_us is not None and not sectors_us.empty:
        st.markdown("#### S&P SPDR 板塊輪動 (5 日 %)")
        st.dataframe(sectors_us, use_container_width=True, hide_index=True)

    # ============== 📰 IBKR 風格新聞摘要 ==============
    st.markdown("---")
    st.markdown("### 📰 全球市場新聞摘要")
    st.caption("整合 CNN / Fox / BBC / NYT / Reuters / FinMind 台股新聞 + 油價訊號 + Trump 言論。每則自動標利多/利空。")

    cN1, cN2, cN3 = st.columns([1, 1, 1])
    with cN1:
        refresh_news_btn = st.button("🔄 更新所有新聞", use_container_width=True, type="primary",
                                       key="refresh_world_news")
    with cN2:
        news_filter = st.selectbox(
            "分類過濾",
            options=["全部", "📈 利多", "📉 利空", "➖ 中性",
                     "CNN", "Fox", "BBC", "NYT", "Reuters", "Trump"],
            index=0, key="news_filter",
        )
    with cN3:
        news_show_count = st.number_input("顯示筆數", 5, 50, 20, step=5, key="news_show_n")

    if refresh_news_btn:
        try:
            import news_sources
            with st.spinner("抓取全球新聞..."):
                world_news = news_sources.fetch_world_news()
                trump_posts = news_sources.fetch_trump_truth_social(max_items=5)
                for tp in trump_posts:
                    world_news.append({
                        "source": "Trump",
                        "title": tp.get("text", "")[:180],
                        "link": tp.get("link", ""),
                        "summary": tp.get("text", "")[:300],
                        "time": tp.get("time", ""),
                    })
                # 補上 sentiment 跟相對時間
                world_news = news_sources.enrich_news_with_sentiment(world_news)
                # 翻譯成繁中 (Gemini 可用時才翻)
                if ai_analyzer.gemini_available():
                    with st.spinner("翻譯新聞為繁中..."):
                        world_news = news_sources.translate_news_titles(world_news)
            st.session_state["world_news"] = world_news
        except Exception as e:
            st.error(f"新聞抓取失敗：{e}")

    world_news = st.session_state.get("world_news", [])
    if world_news:
        # 過濾
        filtered = world_news
        if news_filter == "📈 利多":
            filtered = [n for n in filtered if n.get("sentiment", 0) > 0]
        elif news_filter == "📉 利空":
            filtered = [n for n in filtered if n.get("sentiment", 0) < 0]
        elif news_filter == "➖ 中性":
            filtered = [n for n in filtered if n.get("sentiment", 0) == 0]
        elif news_filter not in ("全部",):
            filtered = [n for n in filtered if news_filter.lower() in (n.get("source", "") or "").lower()]

        # 統計
        n_bull = sum(1 for n in world_news if n.get("sentiment", 0) > 0)
        n_bear = sum(1 for n in world_news if n.get("sentiment", 0) < 0)
        n_neut = sum(1 for n in world_news if n.get("sentiment", 0) == 0)
        cS1, cS2, cS3, cS4 = st.columns(4)
        cS1.metric("總計", len(world_news))
        cS2.metric("📈 利多", n_bull)
        cS3.metric("📉 利空", n_bear)
        cS4.metric("➖ 中性", n_neut)

        # IBKR 風格 card 顯示
        st.caption(f"顯示 {min(int(news_show_count), len(filtered))} / {len(filtered)} 則")
        for n in filtered[: int(news_show_count)]:
            sent = n.get("sentiment", 0)
            if sent > 0:
                tag = "📈"
                bar_color = "#3B6D11"
                bg = "rgba(99, 153, 34, 0.08)"
                kw_label = "、".join(n.get("bullish_kw", [])[:3])
            elif sent < 0:
                tag = "📉"
                bar_color = "#A32D2D"
                bg = "rgba(163, 45, 45, 0.08)"
                kw_label = "、".join(n.get("bearish_kw", [])[:3])
            else:
                tag = "➖"
                bar_color = "#888780"
                bg = "rgba(127, 127, 127, 0.04)"
                kw_label = ""

            src = n.get("source", "—")
            ta = n.get("time_ago", "")
            title_orig = n.get("title", "")
            title_zh = n.get("title_zh", title_orig)
            # 標題顯示策略: 中文 (主) + 英文 (副), 或單獨原文
            has_translation = title_zh and title_zh != title_orig
            link = n.get("link", "")
            summary = (n.get("summary", "") or "").strip()
            if summary and len(summary) > 200:
                summary = summary[:200] + "…"

            kw_html = f"<span style='color:{bar_color};font-size:11px;margin-left:8px'>〔{kw_label}〕</span>" if kw_label else ""

            card_html = (
                f"<div style='background:{bg};border-left:3px solid {bar_color};"
                f"border-radius:6px;padding:10px 14px;margin:6px 0;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:baseline;'>"
                f"<span style='background:rgba(127,127,127,0.15);padding:2px 8px;border-radius:4px;"
                f"font-size:11px;font-weight:500'>{src}</span>"
                f"<span style='color:#888;font-size:11px'>{ta}</span>"
                f"</div>"
                f"<div style='margin-top:6px;font-size:14px;line-height:1.5'>"
                f"<span style='font-size:14px'>{tag}</span> "
            )
            display_title = title_zh if has_translation else title_orig
            if link:
                card_html += f"<a href='{link}' target='_blank' style='color:inherit;text-decoration:none'>{display_title}</a>"
            else:
                card_html += display_title
            if kw_html:
                card_html += kw_html
            card_html += "</div></div>"
            st.markdown(card_html, unsafe_allow_html=True)


# =============================================================================
# Tab — 回測勝率
# =============================================================================
with tab_bt:
    st.subheader("📊 回測勝率")
    st.caption("基於歷史資料模擬篩選器表現,評估訊號的歷史命中率與報酬分佈.")
    cBT1, cBT2 = st.columns([1, 1])
    with cBT1:
        bt_market = st.selectbox("市場", options=["TW", "US"], key="bt_market")
    with cBT2:
        bt_lookback = st.slider("回測天數", 30, 365, 90, step=30, key="bt_lookback")
    bt_btn = st.button("🚀 跑回測", use_container_width=True, type="primary", key="bt_run")
    if bt_btn:
        try:
            with st.spinner("回測中..."):
                import backtest
                bt_res = backtest.run_backtest(market=bt_market, days=bt_lookback)
            if bt_res.get("error"):
                st.error(f"回測失敗: {bt_res['error']}")
            else:
                df_bt = bt_res.get("trades")
                if df_bt is not None and not df_bt.empty:
                    cMA, cMB, cMC = st.columns(3)
                    with cMA:
                        st.metric("總交易數", len(df_bt))
                    with cMB:
                        if "return%" in df_bt.columns:
                            wr = (df_bt["return%"] > 0).mean() * 100
                            st.metric("勝率", f"{wr:.1f}%")
                    with cMC:
                        if "return%" in df_bt.columns:
                            avg = df_bt["return%"].mean()
                            st.metric("平均報酬", f"{avg:+.2f}%")
                    st.dataframe(df_bt, use_container_width=True, hide_index=True)
                else:
                    st.info("沒有回測結果")
        except ImportError:
            st.warning("尚未安裝 backtest 模組")
        except Exception as e:
            st.error(f"回測異常: {type(e).__name__}: {e}")


# =============================================================================
# Tab — 推薦追蹤
# =============================================================================
with tab_track:
    st.subheader("📈 推薦追蹤")
    st.caption("追蹤過往推送過的標的後續表現. 自動跑每筆「推送日 vs 現價」的報酬計算.")
    import tracker
    cT1, cT2, cT3 = st.columns([1, 1, 1])
    with cT1:
        track_window = st.number_input("追蹤天數", 7, 90, 30, step=7, key="track_window")
    with cT2:
        track_btn = st.button("🔄 計算追蹤表現", use_container_width=True, type="primary",
                               key="track_run_btn")
    with cT3:
        st.download_button(
            "💾 下載歷史 CSV", data=tracker.csv_for_download(),
            file_name=f"tracking_history_{dt.date.today().strftime('%Y%m%d')}.csv",
            use_container_width=True, key="track_download",
        )

    upload = st.file_uploader("📤 還原之前下載的歷史 CSV (可選)", type=["csv"], key="track_upload")
    if upload is not None:
        res = tracker.import_history_from_csv(upload.getvalue())
        if res.get("ok"):
            st.success(f"已匯入 {res['rows']} 筆 (encoding: {res.get('encoding_detected', '?')})")
        else:
            st.error(f"匯入失敗：{res.get('msg')}")

    if track_btn:
        try:
            with st.spinner("計算每檔現價並比對 base_price…"):
                history = tracker.load_history()
                perf = tracker.evaluate_history_performance(history, days_window=track_window)
            if perf is None or perf.empty:
                st.info("尚無追蹤紀錄. 進入「強勢族群」/「美股 Top 10」tab 推送過後, 會自動加進追蹤. 或從本 tab 上方匯入歷史 CSV.")
            else:
                st.dataframe(perf, use_container_width=True, hide_index=True)
                # tracker.evaluate_history_performance 寫入欄位 "return%" (英文),
                # 同時相容舊資料 (可能是 "報酬%" 中文)
                ret_col = None
                for c in ("return%", "報酬%"):
                    if c in perf.columns:
                        ret_col = c
                        break
                if ret_col:
                    valid = perf[perf[ret_col].notna()]
                    if len(valid) > 0:
                        avg = float(valid[ret_col].mean())
                        win_rate = float((valid[ret_col] > 0).mean() * 100)
                        cM1, cM2, cM3 = st.columns(3)
                        with cM1:
                            st.metric("平均報酬", f"{avg:+.2f}%")
                        with cM2:
                            st.metric("勝率", f"{win_rate:.1f}%")
                        with cM3:
                            st.metric("追蹤檔數", f"{len(valid)}")
        except Exception as e:
            st.error(f"追蹤計算失敗: {type(e).__name__}: {e}")


# =============================================================================
# Tab — 入場評估 (C: 新)
# =============================================================================
with tab_entry:
    st.subheader("🎯 個股入場評估")
    st.caption(
        "輸入股票代號 (台股 4 碼 / 美股 ticker), 系統評估當下個股強度、同族群表現、"
        "相對大盤、PE/EPS, 給出 BUY/WAIT/AVOID 結論. 適合判斷「現在能不能進場」."
    )

    cE1, cE2, cE3 = st.columns([2, 1, 1])
    with cE1:
        entry_input = st.text_input(
            "股票代號 (例: 2330 / NVDA / RKLB)",
            value="",
            key="entry_input",
            placeholder="輸入後按下方分析按鈕",
        )
    with cE2:
        market_choice = st.selectbox(
            "市場", ["auto", "TW", "US"], index=0, key="entry_market",
            help="auto 會自動判斷 (4 碼數字=TW, 字母=US)",
        )
    with cE3:
        entry_run = st.button("🔍 分析", use_container_width=True,
                                key="entry_run", type="primary")

    if entry_run and entry_input.strip():
        try:
            import entry_evaluator as ee
            with st.spinner(f"分析 {entry_input.strip()} 中…約 10-20 秒"):
                result = ee.evaluate_entry(entry_input.strip(), market=market_choice)
            st.session_state["entry_result"] = result
        except Exception as e:
            st.error(f"分析失敗: {type(e).__name__}: {e}")

    result = st.session_state.get("entry_result")
    if result:
        if result.get("error"):
            st.error(result["error"])
        else:
            snap = result.get("snap", {})
            peers = result.get("peers", {})
            rs = result.get("rs_vs_market")
            fund = result.get("fundamentals", {})
            verdict = result.get("verdict", {})

            # === 結論卡片 ===
            v_emoji = verdict.get("verdict_emoji", "")
            v_text = verdict.get("verdict", "?")
            v_score = verdict.get("score", 0)
            st.markdown(f"### {v_emoji} 結論: **{v_text}** (評分 {v_score}/100)")
            pa = verdict.get("position_action")
            if pa:
                pa_e = verdict.get("position_emoji", "")
                pa_d = verdict.get("position_detail", "")
                st.markdown(f"### {pa_e} 持倉建議: **{pa}**")
                st.caption(pa_d)
            for r in verdict.get("reasons", []):
                st.markdown(f"- {r}")

            st.divider()

            # === 個股現況 (safe-format) ===
            def _sf(v, fmt="+.2f", suffix="%"):
                if v is None:
                    return "—"
                try:
                    return format(float(v), fmt) + suffix
                except (TypeError, ValueError):
                    return "—"

            st.markdown("#### 📊 個股現況")
            cS1, cS2, cS3, cS4 = st.columns(4)
            cS1.metric("現價", snap.get("current") if snap.get("current") is not None else "—",
                        _sf(snap.get("today_pct")))
            cS2.metric("量比", snap.get("vol_ratio") if snap.get("vol_ratio") is not None else "—")
            cS3.metric("RSI(14)", snap.get("rsi14") if snap.get("rsi14") is not None else "—")
            cS4.metric("趨勢", snap.get("trend") or "—")
            cT1, cT2, cT3 = st.columns(3)
            cT1.metric("距 MA20", _sf(snap.get("ma20_dist_pct")))
            cT2.metric("距 52w 高", _sf(snap.get("from_52w_high_pct")))
            cT3.metric("距 52w 低", _sf(snap.get("from_52w_low_pct")))

            # === 同族群 ===
            st.markdown(f"#### 🚀 同族群表現 ({peers.get('sector') or '—'})")
            if peers.get("sector_avg_pct") is not None:
                cP1, cP2 = st.columns(2)
                cP1.metric("族群均漲", f"{peers['sector_avg_pct']:+.2f}%")
                if peers.get("up_ratio") is not None:
                    cP2.metric("上漲家數比", f"{peers['up_ratio'] * 100:.0f}%")
            peer_list = peers.get("peers") or []
            if peer_list:
                import pandas as pd
                peer_df = pd.DataFrame(peer_list)
                st.dataframe(peer_df, use_container_width=True, hide_index=True)
            else:
                st.caption("(沒有抓到同族群股, 可能族群偏小)")

            # === 相對大盤 RS ===
            if rs is not None:
                if rs >= 1.5:
                    rs_emoji = "✅ 強跑贏"
                elif rs >= 0.3:
                    rs_emoji = "➕ 跑贏"
                elif rs >= -0.3:
                    rs_emoji = "➡️ 同步"
                elif rs >= -1.5:
                    rs_emoji = "➖ 跑輸"
                else:
                    rs_emoji = "❌ 大幅跑輸"
                st.markdown(f"#### 🌐 相對大盤 RS = **{rs:+.2f}pp**  {rs_emoji}")

            # === 基本面 ===
            st.markdown("#### 💰 基本面")
            cF1, cF2, cF3 = st.columns(3)
            cF1.metric("PE", fund.get("pe_label") or "—")
            if fund.get("forward_pe"):
                cF2.metric("Forward PE", f"{fund['forward_pe']:.1f}")
            if fund.get("peg"):
                cF3.metric("PEG", f"{fund['peg']:.2f}")
            cG1, cG2 = st.columns(2)
            if fund.get("eps") is not None:
                cG1.metric("EPS", fund["eps"])
            if fund.get("eps_yoy_pct") is not None:
                cG2.metric("EPS YoY", f"{fund['eps_yoy_pct']:+.1f}%")
            if fund.get("revenue_yoy_pct") is not None:
                st.caption(f"營收 YoY: {fund['revenue_yoy_pct']:+.1f}%")
            if fund.get("earnings_date"):
                st.caption(f"📅 下次財報: **{fund['earnings_date']}**")
            if fund.get("marketcap") and fund["marketcap"] != "—":
                st.caption(f"市值: {fund['marketcap']}")
            if fund.get("industry"):
                st.caption(f"產業: {fund['industry']}")

            ai_summary = result.get("ai_summary")
            if ai_summary:
                st.divider()
                st.markdown("#### 🤖 Gemini 結論")
                st.info(ai_summary)

    st.caption("⚠️ 評估僅供參考, 不構成投資建議. 請自行做研究與風控.")
