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
            max_coin_margin_pct=1.0,
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


class TokenExposureTests(unittest.TestCase):
    def _portfolio(self, max_positions: int) -> tuple[tempfile.TemporaryDirectory, core.Store, core.PaperPortfolio]:
        temp = tempfile.TemporaryDirectory()
        config = settings(
            Path(temp.name),
            live=True,
            paper_starting_cash=1000.0,
            max_positions=max_positions,
            max_slices_per_coin=4,
            max_coin_margin_pct=0.20,
            max_wallet_margin_pct=1.0,
        )
        store = core.Store(config.db_path)
        return temp, store, core.PaperPortfolio(config, store)

    def test_token_cap_is_twenty_percent_for_six_and_ten_slot_books(self) -> None:
        for max_positions in (6, 10):
            with self.subTest(max_positions=max_positions):
                temp, store, paper = self._portfolio(max_positions)
                try:
                    self.assertEqual(
                        paper.available_slot(
                            "PUMP",
                            1.0,
                            2.0,
                            "SHORT",
                            wallet="wallet-a",
                            wallet_equity=1000.0,
                        ),
                        200.0,
                    )
                finally:
                    store.conn.close()
                    temp.cleanup()

    def test_existing_over_cap_position_is_grandfathered_but_cannot_grow(self) -> None:
        temp, store, paper = self._portfolio(6)
        try:
            self.assertEqual(
                paper.open("wallet-a", "PUMP", "SHORT", 1.0, 250.0, leverage=3),
                250.0,
            )
            self.assertIsNotNone(paper.positions().get("PUMP"))
            self.assertIsNone(
                paper.available_slot(
                    "PUMP",
                    1.0,
                    1.0,
                    "SHORT",
                    wallet="wallet-b",
                    wallet_equity=1000.0,
                )
            )
        finally:
            store.conn.close()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
