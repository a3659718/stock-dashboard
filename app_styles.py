"""
app_styles.py — 從 app.py 拆出來的 CSS / styling helper.

只負責 inject CSS, 不涉及 logic.

Usage:
  import app_styles
  app_styles.inject_global_css()
"""
from __future__ import annotations

import streamlit as st


_CSS_GLOBAL = """
    <style>
      .block-container { padding-top: 1rem; padding-bottom: 4rem; }
      /* === #2 統一卡片 / 指標視覺 (淡框卡片, 明暗主題皆適用) === */
      .stMetric { background: rgba(127,127,127,0.06); padding: 10px 14px;
                  border-radius: 10px; border: 1px solid rgba(127,127,127,0.18); }
      .stMetric div[data-testid="stMetricValue"] { font-weight: 600; }
      .stMetric label { opacity: 0.72; }
      hr { opacity: 0.5; margin: 0.6rem 0; }
      [data-testid="stCaptionContainer"] { opacity: 0.82; }
      .pill { display:inline-block; padding:2px 8px; border-radius:999px;
              background:#1f6feb22; color:#1f6feb; font-size:12px; margin:2px; }
      .pill.warn { background:#d2940022; color:#d29400; }
      .pill.bad  { background:#d3000022; color:#d30000; }

      /* 分頁列 — 變得更醒目, 過窄時會自動換行 */
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

      /* === Mobile UI 優化 (max-width: 640px) === */
      @media (max-width: 640px) {
        .block-container {
            padding-left: 0.5rem; padding-right: 0.5rem;
            padding-top: 0.5rem;
        }
        .stTabs [data-baseweb="tab"] {
            padding: 6px 8px;
            font-size: 11px;
            margin-bottom: 3px;
        }
        .stMetric { padding: 4px 6px; }
        .stMetric label { font-size: 11px !important; }
        .stMetric div[data-testid="stMetricValue"] {
            font-size: 18px !important;
        }
        .stButton button {
            min-height: 44px;
            font-size: 14px !important;
            padding: 0.5rem 0.8rem !important;
        }
        h1 { font-size: 1.4rem !important; }
        h2, .stSubheader { font-size: 1.15rem !important; }
        h3 { font-size: 1.0rem !important; }
        .stDataFrame {
            overflow-x: auto;
            font-size: 12px;
        }
        .stMarkdown, .stCaption { font-size: 13px; }
        .stColumn { padding: 0 4px !important; }
        .streamlit-expanderHeader { padding: 8px 12px !important; font-size: 14px; }
        .stTextInput input, .stSelectbox div[data-baseweb="select"] {
            min-height: 40px;
            font-size: 14px;
        }
        .stCheckbox label { font-size: 12px !important; }
        .date-banner { font-size: 12px; padding: 6px 10px; }
        .date-banner b { font-size: 13px; }
      }
      @media (max-width: 480px) {
        .stTabs [data-baseweb="tab-list"] {
            flex-wrap: nowrap !important;
            overflow-x: auto !important;
        }
        .stTabs [data-baseweb="tab"] {
            font-size: 10px;
            padding: 5px 6px;
        }
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
"""


def inject_global_css() -> None:
    """注入全站 CSS — 只在 app 啟動時呼叫一次."""
    st.markdown(_CSS_GLOBAL, unsafe_allow_html=True)
