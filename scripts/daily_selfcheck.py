"""
daily_selfcheck.py — 每日健康自檢 + 主動告警 (讓「安靜的退化」變大聲).

這個專案最大的風險不是會崩, 而是「安靜地壞掉」(到處 try/except + fallback 把錯誤吞掉,
例如 _get_model 缺失讓 8 個 Gemini 功能默默失效很久沒人發現)。本腳本每天跑一次,
把以下異常主動推一封 TG 給你:

  1. 程式完整性  — 跑 smoke_test_full 的靜態檢查 (import 符號失效 / 排程無 handler)
                   → 這會自動抓到未來再出現的「_get_model 類」回歸
  2. Gemini      — key 在不在 + _get_model() 是否真的建得起來 (不是只看 key)
  3. FinMind     — API 額度 / token
  4. 推播紀錄    — 過去 24h 有沒有失敗; 交易日卻一封都沒推 (可疑)
  5. 排程/cron   — 最近一次 cron 是否過期

沒異常時推一封安靜的 (不響鈴) 摘要; 有異常時響鈴。

用法 (建議排程每天一次, 例如台北 08:10):
    python scripts/daily_selfcheck.py
"""
from __future__ import annotations

import os
import sys
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _tpe_now() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def run_checks() -> dict:
    """回 {anomalies: [str], info: [str]} — 每個探針自我吞例外, 絕不讓自檢本身崩."""
    anomalies: list = []
    info: list = []

    # 1. 程式完整性 (靜態, 零相依)
    try:
        import smoke_test_full as smk
        imp = smk.check_local_imports()
        sch = smk.check_schedule_coverage()
        if imp:
            anomalies.append(f"🧩 程式完整性: {len(imp)} 個 import 符號失效 — 例: {imp[0][2][:70]}")
        if sch:
            anomalies.append(f"🗓 排程: {len(sch)} 個 market 沒有 handler — 例: {sch[0][2][:70]}")
        if not imp and not sch:
            info.append("🧩 程式完整性 OK")
    except Exception as e:
        anomalies.append(f"🧩 完整性檢查本身失敗: {type(e).__name__}: {e}")

    # 2. Gemini 深探 (不只看 key, 還實際建 model)
    try:
        import ai_analyzer
        has_key = bool(ai_analyzer.get_gemini_key())
        if not has_key:
            info.append("🤖 Gemini: 未設 key (功能停用, 非錯誤)")
        else:
            model = ai_analyzer._get_model()
            if model is None:
                anomalies.append("🤖 Gemini: 有 key 但 _get_model() 回 None — AI 功能可能整片失效")
            else:
                info.append("🤖 Gemini OK")
    except Exception as e:
        anomalies.append(f"🤖 Gemini 探測失敗: {type(e).__name__}: {e}")

    # 3~5. 用 system_health.collect_health()
    try:
        import system_health
        h = system_health.collect_health()

        fm = h.get("finmind") or {}
        if fm.get("err"):
            anomalies.append(f"📦 FinMind: {fm['err']}")
        else:
            info.append("📦 FinMind OK")

        p24 = h.get("push_24h") or {}
        fails = int(p24.get("fail", 0) or 0)
        total = int(p24.get("total", 0) or 0)
        if fails > 0:
            bt = p24.get("by_type", {}) or {}
            worst = ", ".join(f"{k}×{v.get('fail',0)}" for k, v in bt.items() if v.get("fail"))
            anomalies.append(f"📨 推播: 過去 24h 有 {fails} 次失敗 ({worst or '?'})")
        # 交易日卻一封都沒推 → 可疑 (週一~週五 TPE)
        if total == 0 and _tpe_now().weekday() < 5:
            anomalies.append("📨 推播: 交易日過去 24h 一封都沒推 (cron 可能掛了?)")
        info.append(f"📨 24h 推播 {p24.get('ok',0)} 成功 / {fails} 失敗")

        cron = h.get("cron_health") or {}
        cstat = str(cron.get("status", ""))
        if "🔴" in cstat or "stale" in cstat.lower() or "過期" in cstat:
            anomalies.append(f"⏰ cron: {cstat}")
    except Exception as e:
        anomalies.append(f"🩺 system_health 失敗: {type(e).__name__}: {e}")

    return {"anomalies": anomalies, "info": info}


def build_message(result: dict) -> str:
    anomalies = result["anomalies"]
    info = result["info"]
    ts = _tpe_now().strftime("%Y-%m-%d %H:%M")
    if anomalies:
        lines = [f"🚨 <b>每日自檢 — 發現 {len(anomalies)} 項異常</b> ({ts} TPE)", ""]
        lines += [f"• {a}" for a in anomalies]
        if info:
            lines += ["", "<i>其餘正常:</i>"]
            lines += [f"  · {i}" for i in info]
    else:
        lines = [f"✅ <b>每日自檢 — 全部正常</b> ({ts} TPE)", ""]
        lines += [f"• {i}" for i in info]
    return "\n".join(lines)


def main() -> int:
    result = run_checks()
    msg = build_message(result)
    has_anomaly = bool(result["anomalies"])
    print(msg)
    # 使用者反饋: 這支腳本每次執行都會真的推一封 TG (健康時靜音、異常時響鈴),
    # 等於每天多一封「一切正常」的背景推播 — 沒有資訊價值, 只有異常時才需要
    # 主動打擾使用者。改成: 沒異常只印 console log (排程執行紀錄仍可查), 有
    # 異常才真的推播。
    if has_anomaly:
        try:
            import notifier
            ok, infostr = notifier.send_message(msg)
            print(f"[daily_selfcheck] TG sent: ok={ok} {infostr}", flush=True)
        except Exception as e:
            print(f"[daily_selfcheck] TG 推送失敗: {e}", flush=True)
    else:
        print("[daily_selfcheck] 全部正常, 略過推播 (只留 console log)", flush=True)
    # 有異常回非 0 (方便 CI / 排程辨識)
    return 1 if has_anomaly else 0


if __name__ == "__main__":
    raise SystemExit(main())
