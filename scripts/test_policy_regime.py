"""Offline tests for radar/analysis/policy_regime (no network)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.analysis import policy_regime as pr  # noqa: E402


def _fake_history(symbol, period="3mo", interval="1d", **_):
    # SPY / 0050 平盤; 綠能 +10%; 國防 -10%; 其他 +1%; DEAD 無資料
    if symbol == "DEAD":
        return pd.DataFrame()
    end = {"SPY": 100, "0050.TW": 100, "FSLR": 110, "LMT": 90}.get(symbol, 101)
    closes = [100.0] * 40 + [float(end)]
    return pd.DataFrame({"Date": range(len(closes)), "Close": closes})


class PolicyRegimeTests(unittest.TestCase):
    def test_every_theme_is_well_formed(self):
        for t in pr.POLICY_THEMES:
            self.assertIn(t["direction"], pr.DIRECTION_LABEL)
            self.assertTrue(t["us"] or t["tw"], t["key"])
            self.assertTrue(t["thesis"] and t["catalysts"])
        self.assertEqual(len({t["key"] for t in pr.POLICY_THEMES}), len(pr.POLICY_THEMES))

    def test_scan_verdicts_and_message(self):
        with patch("radar.data.data_sources.fetch_yf_history", side_effect=_fake_history):
            rows = pr.scan_policy_themes()
        by = {r["key"]: r for r in rows}
        self.assertGreater(by["clean_energy_grid"]["us"]["rel20"], 0)
        self.assertEqual(pr._verdict("benefit", 5), "市場已反映")
        self.assertEqual(pr._verdict("pressure", 5), "與假設相反")
        self.assertEqual(pr._verdict("benefit", 0.5), "尚未反映")
        msg = pr.fmt_policy_regime_tg(rows)
        self.assertIn("政策情境族群", msg)
        self.assertIn("非投資建議", msg)

    def test_missing_data_does_not_crash(self):
        with patch("radar.data.data_sources.fetch_yf_history", return_value=pd.DataFrame()):
            rows = pr.scan_policy_themes()
        self.assertTrue(all(r["verdict"] == "無資料" for r in rows))
        pr.fmt_policy_regime_tg(rows)


if __name__ == "__main__":
    unittest.main()
