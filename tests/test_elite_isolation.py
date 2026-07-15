from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import MockingBot_Elite as elite


class EliteIsolationTests(unittest.TestCase):
    def test_elite_is_forced_to_paper_mode_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.txt"
            with patch.object(elite, "ELITE_CREDENTIALS_PATH", missing):
                credentials = elite.load_elite_credentials()
                configured = elite.elite_settings()
        self.assertFalse(configured.live)
        self.assertEqual(credentials["api_key"], "")
        self.assertIn("MockingBot_Elite_Data", str(configured.data_dir))

    def test_new_labeled_format_is_supported(self) -> None:
        wallet = "0x" + "2" * 40
        agent = "0x" + "b" * 40
        key = "0x" + "1" * 64
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "elite.txt"
            path.write_text(
                f"Wallet: {wallet}\nAPI wallet: {agent}\nAPI key: {key}\n",
                encoding="utf-8",
            )
            with patch.object(elite, "ELITE_CREDENTIALS_PATH", path):
                parsed = elite.load_elite_credentials()
        self.assertEqual(parsed["wallet"], wallet)
        self.assertEqual(parsed["api_wallet"], agent)
        self.assertEqual(parsed["api_key"], key)


if __name__ == "__main__":
    unittest.main()
