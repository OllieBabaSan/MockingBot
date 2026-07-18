from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import settings


class WalletExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings(
            Path(self.temp.name),
            live=True,
            paper_starting_cash=500.0,
            max_positions=4,
            max_wallet_margin_pct=0.35,
        )
        self.store = core.Store(self.settings.db_path)
        self.paper = core.PaperPortfolio(self.settings, self.store)

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def test_second_allocation_is_trimmed_to_aggregate_wallet_cap(self) -> None:
        wallet = "wallet-a"
        first = self.paper.available_slot(
            "BTC", 100.0, 1.15, "LONG", wallet=wallet, wallet_equity=500.0
        )
        self.assertEqual(first, 143.75)
        self.assertEqual(
            self.paper.open(wallet, "BTC", "LONG", 100.0, first, leverage=3),
            first,
        )

        second = self.paper.available_slot(
            "ETH", 100.0, 1.15, "LONG", wallet=wallet, wallet_equity=500.0
        )
        self.assertEqual(second, 31.25)

    def test_wallet_over_cap_cannot_receive_another_allocation(self) -> None:
        wallet = "wallet-a"
        self.assertEqual(
            self.paper.open(wallet, "BTC", "LONG", 100.0, 200.0, leverage=3),
            200.0,
        )
        self.assertIsNone(
            self.paper.available_slot(
                "ETH", 100.0, 1.15, "LONG", wallet=wallet, wallet_equity=500.0
            )
        )
        self.assertEqual(
            self.paper.available_slot(
                "SOL", 100.0, 1.15, "LONG", wallet="wallet-b", wallet_equity=500.0
            ),
            143.75,
        )


if __name__ == "__main__":
    unittest.main()
