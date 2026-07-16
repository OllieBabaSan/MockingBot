import sqlite3
import tempfile
import unittest
from pathlib import Path

from MockingBot import PaperPortfolio, Settings, Store


class AtomicPaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.settings = Settings(data_dir=Path(self.temp.name), paper_starting_cash=10_000.0)
        self.portfolio = PaperPortfolio(self.settings, self.store)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def test_failed_open_rolls_back_cash_slice_and_aggregate(self):
        self.store.conn.execute(
            """
            CREATE TRIGGER reject_btc_aggregate BEFORE INSERT ON paper_positions
            WHEN NEW.coin = 'BTC' BEGIN SELECT RAISE(ABORT, 'injected aggregate failure'); END
            """
        )
        self.store.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.portfolio.open("wallet", "BTC", "LONG", 100.0, cost_basis=100.0)

        self.assertEqual(self.portfolio.account()["cash"], 10_000.0)
        self.assertEqual(self.store.open_position_slices("BTC"), [])
        self.assertIsNone(self.portfolio.position("BTC"))

    def test_failed_close_rolls_back_cash_slice_and_aggregate(self):
        self.assertEqual(
            self.portfolio.open("wallet", "BTC", "LONG", 100.0, cost_basis=100.0),
            100.0,
        )
        before_cash = self.portfolio.account()["cash"]
        self.store.conn.execute(
            """
            CREATE TRIGGER reject_btc_delete BEFORE DELETE ON paper_positions
            WHEN OLD.coin = 'BTC' BEGIN SELECT RAISE(ABORT, 'injected aggregate failure'); END
            """
        )
        self.store.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.portfolio.close("wallet", "BTC", "LONG", 110.0)

        self.assertEqual(self.portfolio.account()["cash"], before_cash)
        slices = self.store.open_position_slices("BTC")
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0]["status"], "OPEN")
        self.assertIsNotNone(self.portfolio.position("BTC"))


if __name__ == "__main__":
    unittest.main()
