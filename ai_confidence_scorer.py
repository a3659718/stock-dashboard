"""
ai_confidence_scorer.py
對 actionable picks 用 Gemini 批次評估「現在進場信心 0-1」, 加權重排.

設計考量:
  - 批次 (1 次 API call 評估 N 檔), 不要 N 個 stocks 跑 N 次
  - Gemini 回 JSON dict: {symbol: {confidence: 0-1, reason: str}}
  - 用 ensemble: final_score = original_score × (0.6 + 0.4 × ai_confidence)
    (AI 不主導, 只調權重 ±20% 以內)
  - 失敗 → 不動原始排序 (graceful degradation)

API:
  rescore_with_ai(picks: List[Dict], market: str = "TW") -> List[Dict]
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

import ai_analyzer


# AI 權重: ensemble 公式 final = orig × (1 - W + W × ai_conf)
# W=0.4 表示 AI 最多 +40% 加分 / -40% 扣分
AI_WEIGHT = 0.4


def _build_batch_prompt(picks: List[Dict], market: str) -> str:
    """生成 batch prompt — 給 Gemini 一次評估全部."""
    market_label = "台股" if market == "TW" else "美股"
    lines = [
        f"你是專業 {market_label}短線分析師. 我要對以下 {len(picks)} 檔股票評估「現在進場的信心 0.0-1.0」.",
        "",
        "判斷依據 (排序):",
        "  1. 技術面評分 + 命中條件數",
        "  2. 籌碼面 (法人/主力動向)",
        "  3. 板塊強度與催化劑",
        "  4. R:R 比、現價距停損/目標距離",
        "  5. 警告訊號 (財報前/估值偏高/籌碼背離)",
        "",
        "回**純 JSON 格式** (沒有 markdown ```, 沒前後說明), schema:",
        '{ "scores": [ {"symbol": "代號", "confidence": 0.0-1.0, "reason": "20 字內" }, ... ] }',
        "",
        "信心分布建議: 多數 0.4-0.7, 真正好的 >0.75, 真正爛的 <0.3.",
        "",
        "股票清單:",
    ]
    for i, p in enumerate(picks, 1):
        sym = p.get("symbol") or p.get("stock_id", "")
        name = p.get("name") or p.get("stock_name", "")
        score = p.get("score") or p.get("entry_score", "—")
        reasons = (p.get("reasons") or [])[:3]
        warnings = (p.get("warnings") or [])[:2]
        rr = p.get("rr")
        cur = p.get("current") or p.get("price")
        info_parts = [f"score={score}"]
        if rr:
            info_parts.append(f"R:R={rr}")
        if cur:
            info_parts.append(f"現價={cur}")
        if reasons:
            info_parts.append(f"理由={'; '.join(reasons)[:80]}")
        if warnings:
            info_parts.append(f"警告={'; '.join(warnings)[:60]}")
        lines.append(f"{i}. {sym} {name}: {' | '.join(info_parts)}")

    return "\n".join(lines)


def _parse_ai_response(text: str) -> Dict[str, Dict]:
    """從 Gemini 回應抽 JSON dict.
    回 {sym: {"confidence": float, "reason": str}}
    """
    if not text:
        return {}
    # 去掉 markdown code fence
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        j = json.loads(text)
    except Exception:
        # fallback 找第一個 { … } JSON 區塊
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            j = json.loads(m.group())
        except Exception:
            return {}

    out = {}
    scores = j.get("scores") if isinstance(j, dict) else None
    if not isinstance(scores, list):
        return {}
    for s in scores:
        if not isinstance(s, dict):
            continue
        sym = str(s.get("symbol", "")).strip().upper()
        conf = s.get("confidence")
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        # 防範: clamp [0, 1]
        conf = max(0.0, min(1.0, conf))
        out[sym] = {
            "confidence": conf,
            "reason": str(s.get("reason", ""))[:100],
        }
    return out


def rescore_with_ai(picks: List[Dict], market: str = "TW",
                      max_picks_to_score: int = 20) -> List[Dict]:
    """用 Gemini 對 picks 評信心, 加權重排.

    流程:
      1. 沒 picks 或 Gemini 不可用 → 原樣回
      2. 限制最多 max_picks_to_score 檔給 AI 評 (省 quota + 避 response 截斷)
      3. 批次給 Gemini 評 confidence
      4. 對每檔 final_score = orig_score × (1 - W + W × ai_conf)
      5. 按 final_score 降冪重排
      6. 加 ai_confidence / ai_reason / ai_adjusted_score 欄位

    HIGH 設計: 任何 AI 失敗都 graceful — 不改原始排序, 只記 warning.
    """
    if not picks:
        return picks
    if not ai_analyzer.gemini_available():
        return picks

    # 限上限 (超過 20 檔, 只對 top 20 跑 AI; 剩下保留原排序在後面)
    scored_picks = picks[:max_picks_to_score]
    rest_picks = picks[max_picks_to_score:]

    try:
        prompt = _build_batch_prompt(scored_picks, market)
        # 用 ai_analyzer 內部的 model (省 init 重複)
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return picks
        resp = model.generate_content(prompt)
        ai_text = (resp.text or "") if resp else ""
        if not ai_text:
            return picks
        ai_scores = _parse_ai_response(ai_text)
        if not ai_scores:
            return picks
    except Exception as e:
        print(f"[ai_confidence] AI 失敗 graceful skip: {type(e).__name__}: {e}", flush=True)
        return picks

    # ensemble (只對 scored_picks 跑)
    for p in scored_picks:
        sym = str(p.get("symbol") or p.get("stock_id", "")).strip().upper()
        ai = ai_scores.get(sym)
        if not ai:
            p["ai_confidence"] = None
            continue
        p["ai_confidence"] = round(ai["confidence"], 2)
        p["ai_reason"] = ai["reason"]
        # 加權: orig × (1 - W + W × ai_conf), 範圍 [orig × 0.6, orig × 1.0]
        # entry_score 範圍 0-100, score 範圍 0-10. 明確路由避免 corner case.
        if p.get("entry_score") is not None:
            try:
                orig_f = max(0.0, min(1.0, float(p["entry_score"]) / 100.0))
            except (TypeError, ValueError):
                orig_f = 0.5
        elif p.get("score") is not None:
            try:
                orig_f = max(0.0, min(1.0, float(p["score"]) / 10.0))
            except (TypeError, ValueError):
                orig_f = 0.5
        else:
            orig_f = 0.5
        adjusted = orig_f * (1.0 - AI_WEIGHT + AI_WEIGHT * ai["confidence"])
        p["ai_adjusted_score"] = round(adjusted * 10, 2)  # 顯示用 0-10

    # 重排 (只重排 scored_picks; rest 接在後面保原序)
    def _key(p):
        return p.get("ai_adjusted_score") or (
            (p.get("score") or p.get("entry_score") or 5) * (10 if p.get("entry_score") else 1) / 10
        )
    scored_picks.sort(key=_key, reverse=True)
    return scored_picks + rest_picks
