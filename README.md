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
Live entries also snapshot the coin position before submission. If an order
response is lost, a measured position increase is recovered as the fill; an
unchanged position is a clean failure, while unverifiable state quarantines only
that coin. Ambiguous entries are never automatically resubmitted.
Close responses receive the same state-based recovery. Verified flatness is
required before the local allocation is cleared; a measured partial close gets
one residual-close attempt, and unresolved residuals remain tracked and
quarantined.
When several wallets share a coin position, exits use Hyperliquid's reduce-only
`market_close` size parameter for only the exiting wallet's reconstructed fill
size. The measured reduction must match before that wallet's local slices are
removed; unrelated wallet allocations remain open.
Confirmed average close fills drive live-ledger realized PnL and copied-wallet
scoring. The execution audit retains the pre-order quote, adverse slippage in
basis points, and whether pricing came from an exchange fill or a midpoint
estimate after response recovery.
Before every live entry, MockingBot reads Hyperliquid account value,
`totalMarginUsed`, and `withdrawable`. Required allocation margin must fit within
the smaller available amount after a default 5% equity reserve
(`LIVE_MARGIN_RESERVE_PCT`). If the snapshot is unavailable, entries stop while
exits continue. The Live dashboard reports available/usable margin and the
variance between exchange equity and the local comparison ledger.
Leverage is also verified from live clearinghouse position state. New entries
require a successful leverage-update response and post-fill confirmation;
same-coin additions require the existing exchange leverage to match the local
coin leverage. Mismatches quarantine that coin and are visible as local versus
Hyperliquid leverage on the Live dashboard.
Live size reconciliation starts at a 1% relative tolerance
(`LIVE_SIZE_TOLERANCE_PCT=0.01`) with a floor of two exchange size increments.
The setting cannot exceed 1%. The Live dashboard shows measured size difference
and permitted tolerance; synchronized positions clear their quarantine
automatically.
An entry that produces a measured exchange fill but fails post-fill validation
gets one reduce-only rollback for exactly that fill size. Confirmed rollback
restores the pre-entry size without retrying the entry. Failed or unverifiable
rollback produces a prominent `ENTRY ROLLBACK FAILED` coin quarantine and
notification while the remaining book continues.
