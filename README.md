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
- Startup enforces a per-data-directory instance lock; stale locks are recovered
  automatically after an interrupted process.
- Confirm `HL_LIVE=true` only when ready to place live orders.
- The live default is four concurrent coin positions (`MAX_POSITIONS=4`).
- Candidate, proven Candidate, Core, and Elite allocations all start at 3x.
- `MAX_LEVERAGE_CAP=5` is a hard bot cap; tier settings cannot exceed it.
- Slippage defaults to 1% (`SLIPPAGE=0.01`) and cannot exceed 2%.
- Tier allocation multipliers remain independent of leverage and retain their
  established settings.
- Keep the Elite comparison bot credentials and data separate.

Tier leverage can be adjusted later with
`SCORING_ENGINE_DEFAULT_CANDIDATE_LEVERAGE`,
`SCORING_ENGINE_CANDIDATE_LEVERAGE`,
`SCORING_ENGINE_PROVEN_CANDIDATE_LEVERAGE`,
`SCORING_ENGINE_CORE_LEVERAGE`, and `SCORING_ENGINE_ELITE_LEVERAGE`.
Invalid values fail startup before the bot connects or submits an order.
Hyperliquid applies leverage per coin position: additions to an already-open
coin inherit its effective leverage. The requested tier leverage and effective
position leverage are both retained in the execution audit.
Before submission, live orders are checked against Hyperliquid's per-asset
maximum leverage and size precision. Notional is checked again after size
rounding; predictable rejections are audited without quarantining the coin.
