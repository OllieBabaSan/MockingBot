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

## Live Test Checklist

- Use credentials for the intended small live-test account only.
- Confirm no duplicate main bot process is running.
- Confirm `HL_LIVE=true` only when ready to place live orders.
- Use conservative sizing for the first live test.
- Keep the Elite comparison bot credentials and data separate.

