"""Session-state routing for the dashboard's shared Telegram button."""

PULSE_SEND_LABELS = {
    "pulse": "強勢族群/熱門題材",
    "themes": "強勢族群/熱門題材",
    "stealth": "潛伏題材股",
    "smart_stealth": "大戶偷進場",
    "limit_up_precursor": "漲停前兆",
}


def pulse_send_target(state):
    selected = state.get("_pulse_send_target")
    if selected in PULSE_SEND_LABELS:
        return selected
    # 相容更新前已存在的 session，優先保留原本產業/題材合併推送。
    for key in PULSE_SEND_LABELS:
        value = state.get(key)
        if key in ("pulse", "themes", "stealth"):
            frame = (value or {}).get("sectors" if key == "pulse" else key)
            if frame is not None and not frame.empty:
                return key
        elif value:
            return key
    return "pulse"


def format_pulse_send(state):
    """Return (HTML, label) for the most recently requested analysis.

    Empty/failed latest results must never fall back to an older analysis.
    Industry and theme scans retain their original combined-message behavior.
    """
    import notifier

    target = pulse_send_target(state)
    label = PULSE_SEND_LABELS[target]
    data = state.get(target)
    if target == "limit_up_precursor":
        from limit_up_precursor import fmt_limit_up_precursor_msg
        return fmt_limit_up_precursor_msg(data or []), label
    if target == "smart_stealth":
        from smart_money_stealth import fmt_smart_stealth_msg
        return fmt_smart_stealth_msg(data or []), label
    if target == "stealth":
        data = data or {}
        df = data.get("stealth")
        if df is None or df.empty:
            return "", label
        return notifier.fmt_stealth_picks(df, data.get("hot_themes")), label

    # 若這次明確選擇的掃描為空，不把另一種舊分析當作本次結果。
    current = (data or {}).get("sectors" if target == "pulse" else "themes")
    if current is None or current.empty:
        return "", label
    pulse = state.get("pulse") or {}
    themes = state.get("themes") or {}
    return notifier.fmt_strong_sectors(
        pulse.get("sectors"), leaders_map=pulse.get("leaders"),
        themes_df=themes.get("themes"), theme_leaders=themes.get("leaders") or {},
    ), label
