# MockingBot

MockingBot is a Hyperliquid copy-trading bot with isolated paper/live state,
wallet scoring, and a local monitoring dashboard.

## Main Bot

```powershell
python .\MockingBot.py
```

Default state lives in `MockingBot_Data/`.

## Dashboard

```powershell
python .\MockingBot_Dashboard.py
```

Open `http://127.0.0.1:8765`.

## Elite Comparison Bot

```powershell
python .\MockingBot_Elite.py
```

Elite comparison state lives in `MockingBot_Elite_Data/` and uses separate
credentials from the main bot.

## Paper / Live Parity

Live-test execution state is isolated in `MockingBot_Main_Live_Test_Data/`.
On first initialization, wallet-scoring evidence is bootstrapped from the paper
database without copying positions, account state, or trading PnL.

Compare the two main instances with:

```powershell
python .\MockingBot_Compare.py
```

Append-only parity reports are written to
`MockingBot_Comparison_Data/parity.jsonl`.

## Live Test Checklist

- Use credentials for the intended small live-test account only.
- Confirm no duplicate main bot process is running.
- Confirm `HL_LIVE=true` only when ready to place live orders.
- Use conservative sizing for the first live test.
- Keep the Elite comparison bot credentials and data separate.
