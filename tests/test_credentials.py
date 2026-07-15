from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import MockingBot as core

from tests.helpers import TEST_AGENT, TEST_KEY, TEST_USER, settings


class CredentialTests(unittest.TestCase):
    def test_main_credential_file_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "credentials.Hyper.txt"
            path.write_text(
                "\n".join(
                    [
                        f"HL_WALLET_ADDRESS={TEST_USER}",
                        f"HL_API_WALLET_ADDRESS={TEST_AGENT}",
                        f"HL_API_KEY={TEST_KEY}",
                    ]
                ),
                encoding="utf-8",
            )
            parsed = core.load_main_credentials(path)
        self.assertEqual(parsed["wallet"], TEST_USER)
        self.assertEqual(parsed["api_wallet"], TEST_AGENT)
        self.assertEqual(parsed["api_key"], TEST_KEY)

    def test_live_validation_masks_key_and_checks_link(self) -> None:
        class FakeAccount:
            @staticmethod
            def from_key(key: str) -> types.SimpleNamespace:
                self.assertEqual(key, TEST_KEY)
                return types.SimpleNamespace(address=TEST_AGENT)

        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td), live=True)
            store = core.Store(configured.db_path)
            try:
                adapter = core.HyperliquidAdapter(configured, store)
                adapter._post_info = lambda *_args, **_kwargs: {
                    "role": "agent",
                    "data": {"user": TEST_USER},
                }
                output = io.StringIO()
                with patch.dict(sys.modules, {"eth_account": types.SimpleNamespace(Account=FakeAccount)}):
                    with redirect_stdout(output):
                        adapter.validate_live_credentials()
                rendered = output.getvalue()
                self.assertNotIn(TEST_KEY, rendered)
                self.assertIn(TEST_USER[:8], rendered)
                self.assertIn(TEST_AGENT[:8], rendered)
            finally:
                store.conn.close()

    def test_wrong_agent_link_is_rejected(self) -> None:
        fake_account = types.SimpleNamespace(
            from_key=lambda _key: types.SimpleNamespace(address=TEST_AGENT)
        )
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td), live=True)
            store = core.Store(configured.db_path)
            try:
                adapter = core.HyperliquidAdapter(configured, store)
                adapter._post_info = lambda *_args, **_kwargs: {
                    "role": "agent",
                    "data": {"user": "0x" + "0" * 40},
                }
                with patch.dict(sys.modules, {"eth_account": types.SimpleNamespace(Account=fake_account)}):
                    with self.assertRaisesRegex(RuntimeError, "not linked"):
                        adapter.validate_live_credentials()
            finally:
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
