from __future__ import annotations

import unittest

import analyze_stop_loss as analysis


class StopLossAnalysisTests(unittest.TestCase):
    def trade(self, side: str = "LONG") -> dict[str, object]:
        return {
            "opened_at": "2026-01-01 00:01:00",
            "closed_at": "2026-01-01 01:00:00",
            "entry_price": 100.0,
            "side": side,
            "pnl_pct": 5.0,
        }

    def test_partial_entry_candle_is_excluded(self) -> None:
        entry_candle = {
            "t": analysis.parse_utc("2026-01-01 00:00:00"),
            "o": "100", "h": "101", "l": "80", "c": "90",
        }
        result = analysis.stop_result(self.trade(), [entry_candle], 12.0, 0.0)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["return_pct"], 5.0)

    def test_long_stop_uses_gap_open_and_slippage(self) -> None:
        candle = {
            "t": analysis.parse_utc("2026-01-01 00:15:00"),
            "o": "85", "h": "90", "l": "80", "c": "87",
        }
        result = analysis.stop_result(self.trade(), [candle], 12.0, 10.0)
        self.assertTrue(result["stopped"])
        self.assertAlmostEqual(result["return_pct"], -15.085)

    def test_short_stop_uses_candle_high(self) -> None:
        candle = {
            "t": analysis.parse_utc("2026-01-01 00:15:00"),
            "o": "100", "h": "116", "l": "99", "c": "114",
        }
        result = analysis.stop_result(self.trade("SHORT"), [candle], 15.0, 0.0)
        self.assertTrue(result["stopped"])
        self.assertAlmostEqual(result["return_pct"], -15.0)


if __name__ == "__main__":
    unittest.main()
