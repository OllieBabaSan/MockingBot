from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import MockingBot as core


TEST_USER = "0x" + "2" * 40
TEST_AGENT = "0x" + "b" * 40
TEST_KEY = "0x" + "1" * 64


def settings(data_dir: Path, live: bool = False, **changes: Any) -> core.Settings:
    base = replace(
        core.Settings(),
        data_dir=data_dir,
        live=live,
        hl_wallet_address=TEST_USER,
        hl_api_wallet_address=TEST_AGENT,
        hl_api_key=TEST_KEY,
        notify_webhook_url="",
    )
    return replace(base, **changes)


def fill_response(size: str = "0.12", price: str = "100", oid: int = 7) -> dict[str, Any]:
    return {
        "status": "ok",
        "response": {
            "data": {
                "statuses": [
                    {"filled": {"totalSz": size, "avgPx": price, "oid": oid}}
                ]
            }
        },
    }


class FakeExchange:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.opens = 0
        self.closes = 0

    def update_leverage(self, *_args: Any, **_kwargs: Any) -> None:
        return

    def market_open(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.opens += 1
        return self.responses.pop(0)

    def market_close(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.closes += 1
        return self.responses.pop(0)
