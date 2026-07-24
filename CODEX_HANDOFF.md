# MockingBot Codex Handoff

Last updated: 2026-07-16

## 2026-07-16 Live-Readiness Audit Update

The earlier status below is retained as project history. The deep live audit has
since implemented and tested:

- 15% drawdown warning and persistent 25% high-water breaker.
- Isolated live startup and no-order preflight for the four-slot test account.
- Normalized Paper/Live wallet scoring with imported Paper score history.
- Durable copy-event inbox and deterministic Hyperliquid client order IDs.
- Crash recovery for entries, exits, rollbacks, and reconciliation closes.
- Atomic local cash/allocation ledger updates.
- Per-coin quarantine that leaves the remaining book operating.
- Synchronized same-side live adds and allocation-specific partial exits.
- Separate Paper and Live dashboards plus non-blocking parity comparison.
- Breaker-aligned live equity reporting with stale-data refusal.
- Non-blocking token-risk refresh.
- Verified SQLite online backups with retention and dashboard status.

The dormant wallet-pause subsystem was deliberately removed. Poor wallet
performance is controlled by score demotion and Candidate allocation limits.
Run the complete test suite and `python .\MockingBot.py preflight-live` immediately
before any live launch. Preflight performs reads only and submits no orders.

## Project Goal

Create and operate a profitable Hyperliquid copy-trading bot named `MockingBot`.

Core design philosophy:
- KISS: avoid overfit rules and fragile complexity.
- Profitability matters more than win rate.
- Scoring Engine should promote/demote wallets by copied-trade evidence over time.
- Strong wallets should be allowed to breathe; avoid panic exits.
- Candidate wallets are discovery only, not primary capital drivers.

## Current Source Layout

Workspace:
`C:\Users\user\Desktop\MockingBot_Revamp_CodeX`

Main files:
- `MockingBot.py` - main bot, formerly `MockingBot_CodeX`
- `MockingBot_Dashboard.py` - local dashboard
- `MockingBot_Elite.py` - separate baseline comparison bot
- `README.md`
- `deferred_adjustments.md`
- `CODEX_HANDOFF.md`

Runtime data:
- Main bot data/logs: `MockingBot_Data`
- Elite bot data: `MockingBot_Elite_Data`

Do not commit runtime data, logs, DB files, backups, credentials, or caches.

## Git

Repo:
`https://github.com/OllieBabaSan/MockingBot.git`

Branch:
`main`

Recent pushed commits:
- `7e87f2e Prepare MockingBot near-final live test build`
- `10795a2 Tighten Candidate allocation exposure`

## Current Running Processes

Last known after restart:
- Main bot: `MockingBot.py`
- Dashboard: `MockingBot_Dashboard.py`
- Elite comparison bot: `MockingBot_Elite.py`

Always re-check with:
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and ($_.CommandLine -like '*MockingBot*') } | Select-Object ProcessId,Name,CommandLine
```

## Current Bot State / Settings

Main bot is still paper mode:
- `HL_LIVE=false`
- Default paper start remains `$10,000`
- Dashboard URL: `http://127.0.0.1:8765`

Important settings currently in source:
- `MAX_POSITIONS=10`
- `ROSTER_SIZE=150`
- Staggered roster refresh enabled:
  - `ROSTER_REFRESH_BATCH_SIZE=25`
  - `ROSTER_REFRESH_BATCH_SECS=600`
- Scoring Engine active by default.
- Elite gate:
  - score `>=65`
  - copied-trade sample `>=8`
  - historical-only wallets are capped at Core and cannot become Elite.
- Candidate allocation was tightened:
  - Candidate multiplier: `0.50x`
  - Proven Candidate multiplier: `0.70x`
  - Candidate max allocations: `1`
  - Proven Candidate max allocations: `2`
- Core multiplier: `1.15x`
- Elite multiplier: `1.35x`

Dashboard valuation was fixed to apply `HL_LEVERAGE` to dollar PnL while leaving displayed percent as underlying trade move.

## Current Model Read

Confidence is relatively high for the first time in this build.

Observed structure:
- Core is the profit engine.
- Candidate is discovery and now sized appropriately.
- Bench is negative and mostly filtered.
- Elite is reachable but not cheap.
- Simple Elite-only comparison bot has not outperformed the main model so far.

Recent Core sample check:
- Current Core total: `15`
- Core sample `>8`: `10`, all historical-only
- Core sample `<8`: `5`, copied-evidence Core wallets
- Copied-evidence Core close to Elite:
  - `0x7c9309...c8fd`: score `75.1`, sample `7`, PnL `+$252.90`, win `71.4%`
  - `0xec4a6f...cf62`: score `66.0`, sample `7`, PnL `+$43.98`, win `100%`
  - `0xa445a0...329d`: score `65.5`, sample `6`, PnL `+$37.43`
  - `0x17c3c8...a868`: score `67.7`, sample `4`, PnL `+$299.21`

Interpretation:
- `c8fd` likely becomes Elite with one more non-damaging close.
- `cf62` is the useful borderline test: high win rate, low PnL, may enter Elite but should demote if it fails to keep producing.
- Do not over-weight win rate; profitability is the game.

## Recent Candidate Adjustment Rationale

Candidate exposure was reduced because:
- Core closed PnL was much stronger than Candidate.
- Candidate had become too capital-heavy relative to quality.
- `GRASS SHORT` from Candidate recovered, then failed to close and went deep red.
- Another Candidate `BLUR LONG` went against market momentum.

Important: existing Candidate positions were not force-closed. The adjustment only affects future entries/adds.

## Live Wiring Plan

User intends to wire main bot to a separate small Hyperliquid live-test account soon.

Risk context:
- Test account bank around `$500`.
- Elite bot credentials/account must remain untouched.
- User will provide separate main bot live-test credentials.
- Suggested credential file:
  `C:\Users\user\Documents\MockingBot_Main_Live_Test.Hyper.txt`

Before live:
1. Stop or confirm main bot state.
2. Verify only one main bot process.
3. Confirm credentials and wallet address are for the intended small test account.
4. Confirm Elite bot remains isolated.
5. Use conservative live sizing.
6. Ensure only main bot gets `HL_LIVE=true`.
7. Start and closely monitor first live signals.

Suggested live-test posture:
- Start boring.
- Consider `MAX_POSITIONS=3` or `4` for first live test.
- Keep Scoring Engine active.
- Keep Candidate restrictions active.
- Verify order minimums/leverage/sizing before allowing unattended operation.

## Deferred / Not Yet Implemented

Next coordinated paper/live maintenance window:
- Refine parity classification for successful live executions:
  - paper correctly records its midpoint estimate because it submits no exchange order
  - live correctly records the confirmed exchange fill
  - when both engines make the same decision, this price-source/reason difference must be
    classified as expected environment variance, not `LOGIC_DIVERGENCE`

High value but not installed:
- Position replacement v1:
  - let stronger Core/Elite signal free capacity from weaker active Candidate exposure
  - only when blocked by cap/cash
  - no opposite-side handling initially
  - high bug risk, so not near-term until after live readiness

Later:
- Dashboard class-performance panel.
- Possible Elite sample adjustment after 1-2 weeks if no Elite promotions:
  - keep score `65`
  - consider sample `8 -> 6`
- Token market-cap/maturity filter only if token-risk PnL proves bad. Current stance: track first, restrict later.

## Useful Commands

Check processes:
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and ($_.CommandLine -like '*MockingBot*') } | Select-Object ProcessId,Name,CommandLine
```

Start main bot:
```powershell
python .\MockingBot.py
```

Start dashboard:
```powershell
python .\MockingBot_Dashboard.py
```

Check recent log:
```powershell
Get-Content .\MockingBot_Data\mockingbot_live.log -Tail 80
```

Syntax check:
```powershell
python3.13 -m py_compile .\MockingBot.py .\MockingBot_Dashboard.py .\MockingBot_Elite.py
```

Git push after changes:
```powershell
git status --short
git add .
git commit -m "message"
git push
```
