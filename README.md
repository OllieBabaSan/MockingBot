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
.\Start-MockingBot_Paper_Dashboard.ps1
.\Start-MockingBot_Live_Dashboard.ps1
```

Open the paper dashboard at `http://127.0.0.1:8765` and the live dashboard at
`http://127.0.0.1:8766`. Each process is read-only and connects only to its
own instance database.

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

## Tests

Run the offline safety and regression suite before live restarts or releases:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary databases and fake exchange responses. They do not use
the network, submit orders, or print credential values.

## Live Test Checklist

- Use credentials for the intended small live-test account only.
- Confirm no duplicate main bot process is running.
- Confirm `HL_LIVE=true` only when ready to place live orders.
- Use conservative sizing for the first live test.
- Keep the Elite comparison bot credentials and data separate.
