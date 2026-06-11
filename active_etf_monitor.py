"""
active_etf_monitor.py
台股主動式 ETF 持股每日變動偵測.

目前只監控 00981A (主動統一台股增長) — 規模最大且 MoneyDJ 確認 T+2 daily 揭露.
其他主動式 ETF 在 MoneyDJ 多為週/月更新, 不適合做「每日新增/移除」偵測.
未來若想擴充, 改抓各投信官網才有真正每日資料.

資料源: MoneyDJ basic0007 (持股狀況) 頁
   URL: https://www.moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid={CODE}.tw

State: monitor_state["active_etf_holdings"][etf_code] = {
    "last_data_date": "2026/05/12",       # 上次抓到的「資料日期」
    "stocks": {                            # 上次的 top-10 持股
        "2330": {"name": "台積電", "pct": 9.63, "shares": 11657000.0},
        ...
    },
    "checked_at": iso datetime,
}

Workflow:
  1. fetch_etf_holdings(code) → 抓 + parse 持股
  2. detect_changes(code, current) → 比對 state 找 added / removed / pct 變動
  3. save_holdings(code, current) → 更新 state
  4. notifier.fmt_active_etf_change(...) → 推 TG
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Tuple

import requests

import watchlist_store


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
ETF_CONFIG = {
    "00981A": {
        "name": "主動統一台股增長",
        "issuer": "統一投信",
        "url_tmpl": "https://www.moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid={}.tw",
    },
}

# Hidden Bug #3 fix: 偽裝成普通 Chrome 避免 MoneyDJ 擋 bot UA
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 20

# 持股比例變動門檻 (低於這個就不算「變動」, 避免過敏感)
PCT_CHANGE_THRESHOLD = 0.5   # 投資比例變動 ≥0.5pp 才算

# Hidden Bug #7 fix: parser 抓到的 row 數低於這個就視為 parse 失敗 (避免存空 baseline)
MIN_EXPECTED_ROWS = 5


# ---------------------------------------------------------------------------
# Fetch + Parse
# ---------------------------------------------------------------------------
def fetch_etf_holdings(etf_code: str) -> Optional[Dict]:
    """抓並解析 MoneyDJ 的 ETF 持股頁.

    Returns:
        {"data_date": "2026/05/12", "stocks": {sid: {name, pct, shares}, ...}}
        失敗時 return None.
    """
    cfg = ETF_CONFIG.get(etf_code)
    if not cfg:
        print(f"[active_etf] 未知 ETF 代碼: {etf_code}", flush=True)
        return None
    url = cfg["url_tmpl"].format(etf_code.lower())
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print(f"[active_etf] HTTP {r.status_code} for {etf_code}", flush=True)
            return None
        html = r.text
    except Exception as e:
        print(f"[active_etf] fetch failed for {etf_code}: {e}", flush=True)
        return None

    return parse_holdings_html(html)


def parse_holdings_html(html: str) -> Optional[Dict]:
    """從 MoneyDJ HTML 解析「持股明細」section 的資料日期 + top-10 持股.

    Returns dict 同 fetch_etf_holdings, 或 None.
    """
    if not html:
        return None

    # === 1) 找「持股明細」section 起點 ===
    idx = html.find("持股明細")
    if idx < 0:
        return None
    # 抓「持股明細」標題往後 5000 字 — 足夠涵蓋表格
    section = html[idx:idx + 5000]

    # === 2) 解析資料日期 ===
    m_date = re.search(r"資料日期：(\d{4}/\d{1,2}/\d{1,2})", section)
    data_date = m_date.group(1) if m_date else None
    if not data_date:
        return None

    # === 3) 解析每一行持股 ===
    # MoneyDJ 表格 row 範例 (markdown-ish from web_fetch):
    #   | [台積電(2330.TW)](.../etfid=2330.TW&back=00981A.TW) | 9.63 | 11,657,000.00 |
    # 用 regex 抓 (個股名稱 + 代碼 + 比例 + 股數)
    row_pattern = re.compile(
        r"\[?([^\[\]\(\)|]+?)\((\d{4,5})\.TW\)\]?[^|]*\|\s*(\d+\.\d+)\s*\|\s*([\d,]+\.\d{2})",
        re.MULTILINE,
    )
    # 簡化版 fallback: 沒有 markdown link 結構時 (純 HTML), 用更寬鬆
    alt_pattern = re.compile(
        r"etfid=(\d{4,5})\.TW[^>]*>([^<]+)</a>[^|]*\|\s*(\d+\.\d+)\s*\|\s*([\d,]+\.\d{2})",
        re.MULTILINE,
    )

    stocks: Dict[str, Dict] = {}

    for m in row_pattern.finditer(section):
        try:
            name = m.group(1).strip()
            sid = m.group(2)
            pct = float(m.group(3))
            shares_str = m.group(4).replace(",", "")
            shares = float(shares_str)
            if sid not in stocks:  # 第一個出現的優先 (top-N 排序)
                stocks[sid] = {"name": name, "pct": pct, "shares": shares}
        except (ValueError, IndexError):
            continue

    # alt fallback if primary 抓不到
    if not stocks:
        for m in alt_pattern.finditer(section):
            try:
                sid = m.group(1)
                name = m.group(2).strip()
                pct = float(m.group(3))
                shares = float(m.group(4).replace(",", ""))
                if sid not in stocks:
                    stocks[sid] = {"name": name, "pct": pct, "shares": shares}
            except (ValueError, IndexError):
                continue

    # Hidden Bug #2 偵測: 計算 row 總數 (含「無 link 純文字」row)
    # MoneyDJ 偶爾出現 `| 穩懋 | 3.20 | 1,716,000.00 |` 沒 (XXXX.TW) 連結 — 無法解析,
    # 但我們至少要知道它存在, 提醒 user.
    plain_row_pattern = re.compile(
        r"\|\s*[^\|\[\(]{2,10}\s*\|\s*\d+\.\d+\s*\|\s*[\d,]+\.\d{2}\s*\|"
    )
    plain_rows = len(plain_row_pattern.findall(section))
    unmapped = max(0, plain_rows - len(stocks))

    # Hidden Bug #7: parser 抓到太少 row → 視為解析失敗, 避免存空 baseline
    if len(stocks) < MIN_EXPECTED_ROWS:
        print(
            f"[active_etf] parser anomaly: only {len(stocks)} stocks parsed "
            f"(min expected {MIN_EXPECTED_ROWS}). Page format may have changed. "
            f"Returning None to avoid bad baseline.",
            flush=True,
        )
        return None

    if unmapped:
        print(
            f"[active_etf] note: {unmapped} 個 row 沒 (XXXX.TW) link, 已跳過 "
            f"(top-{len(stocks) + unmapped} 中只解析 {len(stocks)} 個). "
            f"diff 可能有 false positive, 請知悉.",
            flush=True,
        )

    return {
        "data_date": data_date,
        "stocks": stocks,
        "unmapped_count": unmapped,  # 給 formatter / debug 用
    }


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def _load_etf_state() -> Dict:
    state = watchlist_store.load_monitor_state()
    return state.setdefault("active_etf_holdings", {})


def _save_etf_state(etf_state: Dict) -> None:
    state = watchlist_store.load_monitor_state()
    state["active_etf_holdings"] = etf_state
    watchlist_store.save_monitor_state(state)


def get_stored_holdings(etf_code: str) -> Optional[Dict]:
    """讀上次存的 ETF 持股 state. None 表示沒紀錄過."""
    s = _load_etf_state()
    return s.get(etf_code)


def save_holdings(etf_code: str, parsed: Dict) -> None:
    """覆寫 ETF 的 state."""
    s = _load_etf_state()
    s[etf_code] = {
        "last_data_date": parsed["data_date"],
        "stocks": parsed["stocks"],
        "checked_at": dt.datetime.now(timezone.utc).isoformat(),
    }
    _save_etf_state(s)


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------
def detect_changes(etf_code: str, current: Dict) -> Optional[Dict]:
    """比對當前持股跟 state 內的持股, 找出 added / removed / pct 大變動.

    Returns:
        None — 沒新資料 (data_date 沒變) 或 沒紀錄前次 state
        Dict — 有變動, 含:
          {"etf_code", "etf_name", "etf_issuer",
           "prev_data_date", "new_data_date",
           "added":   [{sid, name, pct, shares}, ...],
           "removed": [{sid, name, pct, shares}, ...],
           "changed": [{sid, name, old_pct, new_pct, delta_pp}, ...],
           "is_baseline": False,
           "unmapped_count": int}
        或者第一次跑時回傳 {"is_baseline": True, ...} 讓 caller 推「監控已啟動」通知.
    """
    prev = get_stored_holdings(etf_code)
    if not prev:
        # Hidden Bug #5 fix: 第一次 — 回 baseline 訊號讓 caller 推「已啟動」通知
        cfg = ETF_CONFIG.get(etf_code, {})
        return {
            "etf_code": etf_code,
            "etf_name": cfg.get("name", etf_code),
            "etf_issuer": cfg.get("issuer", ""),
            "is_baseline": True,
            "new_data_date": current.get("data_date", ""),
            "stocks_count": len(current.get("stocks", {})),
            "unmapped_count": current.get("unmapped_count", 0),
        }
    prev_date = prev.get("last_data_date")
    new_date = current.get("data_date")
    if not new_date or new_date == prev_date:
        # 資料日期沒變, 表示 MoneyDJ 還沒更新, 不算「新一筆」
        return None

    prev_stocks: Dict[str, Dict] = prev.get("stocks", {})
    curr_stocks: Dict[str, Dict] = current.get("stocks", {})

    prev_ids = set(prev_stocks.keys())
    curr_ids = set(curr_stocks.keys())

    added_ids = curr_ids - prev_ids
    removed_ids = prev_ids - curr_ids
    common = prev_ids & curr_ids

    added = [{"sid": sid, **curr_stocks[sid]} for sid in added_ids]
    removed = [{"sid": sid, **prev_stocks[sid]} for sid in removed_ids]

    changed = []
    for sid in common:
        old_pct = float(prev_stocks[sid].get("pct", 0))
        new_pct = float(curr_stocks[sid].get("pct", 0))
        delta = new_pct - old_pct
        if abs(delta) >= PCT_CHANGE_THRESHOLD:
            changed.append({
                "sid": sid,
                "name": curr_stocks[sid].get("name", ""),
                "old_pct": old_pct,
                "new_pct": new_pct,
                "delta_pp": round(delta, 2),
            })

    if not (added or removed or changed):
        return None

    cfg = ETF_CONFIG.get(etf_code, {})
    return {
        "etf_code": etf_code,
        "etf_name": cfg.get("name", etf_code),
        "etf_issuer": cfg.get("issuer", ""),
        "prev_data_date": prev_date,
        "new_data_date": new_date,
        "added": sorted(added, key=lambda x: -x.get("pct", 0)),
        "removed": sorted(removed, key=lambda x: -x.get("pct", 0)),
        "changed": sorted(changed, key=lambda x: -abs(x.get("delta_pp", 0))),
        "is_baseline": False,
        "unmapped_count": current.get("unmapped_count", 0),
    }


def check_all_active_etfs() -> List[Dict]:
    """檢查所有設定的 active ETF, 回傳有變動的 list."""
    changes: List[Dict] = []
    for code in ETF_CONFIG:
        current = fetch_etf_holdings(code)
        if not current:
            continue
        diff = detect_changes(code, current)
        # 不管有沒有 diff, 都更新 state (即使 data_date 同也覆寫無傷)
        save_holdings(code, current)
        if diff:
            changes.append(diff)
    return changes
