# MockingBot

MockingBot is a Hyperliquid copy-trading bot with isolated paper/live state,
wallet scoring, and a local monitoring dashboard.

Install the live-execution dependency once per Python environment:

```powershell
python -m pip install -r .\requirements.txt
```

## Main Bot

```powershell
python .\MockingBot.py
```

That command is the paper bot; its default state lives in `MockingBot_Data/`.

Start the live bot only through its preflight-gated launcher:

```powershell
.\Start-MockingBot_Live.ps1
# equivalent: python .\MockingBot.py start-live
```

`start-live` forces live mode, seven slots, the `live-main` instance identity,
and `MockingBot_Main_Live_Test_Data/` regardless of the calling shell's paper
defaults. It holds the duplicate-instance lock throughout preflight and startup,
and cannot continue to the trading loop unless every preflight gate passes.
Generic `HL_LIVE`, `MAX_POSITIONS`, and `MOCKINGBOT_DATA_DIR` environment values
cannot bypass or redirect this launcher. Future deliberate expansion can use
`MOCKINGBOT_LIVE_MAX_POSITIONS`; a non-default live state location requires the
dedicated `MOCKINGBOT_LIVE_DATA_DIR` setting.

## Dashboard

```powershell
.\Start-MockingBot_Paper_Dashboard.ps1
.\Start-MockingBot_Live_Dashboard.ps1
```

Open the paper dashboard at `http://127.0.0.1:8765` and the live dashboard at
`http://127.0.0.1:8766`. Each process is read-only and connects only to its
own instance database.

## Automatic Recovery

Register the hidden per-user supervisor once from Administrator PowerShell:

```powershell
.\Install-MockingBot_Resilience.ps1
```

The installer also sets Windows Update to notify before download/install and
disables scheduled automatic restart behavior. Updates must then be initiated
manually during a maintenance window.

The `MockingBot Supervisor` scheduled task starts at user logon and checks every
minute that Paper, Live, and both dashboards are present. Live always restarts
through its normal preflight-gated launcher. The supervisor uses instance-lock
PIDs for the engines and ports `8765`/`8766` for the dashboards, so it does not
duplicate healthy processes. Its event log is `MockingBot_Supervisor.log`.
Disable the scheduled task before intentionally stopping MockingBot for
maintenance.

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

Run the no-order preflight immediately before starting the live bot:

```powershell
python .\MockingBot.py preflight-live
```

It validates credentials and account linkage, capital, asset metadata, database
isolation, writable runtime paths, the duplicate-instance lock, circuit-breaker
state, and exact local/exchange position synchronization. The configured slot
count is a launch gate. Five- and six-slot sizing are also reported as
informational expansion checks and cannot block an otherwise safe four-slot
launch. The command makes information requests but never submits an order.

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
Live entries also snapshot the coin position before submission and persist a
deterministic Hyperliquid client order ID before placing the order. If an order
response is lost, a measured position increase is recovered as the fill; an
unchanged position is a clean failure, while unverifiable state quarantines only
that coin. A crash between journal write and submission is resubmitted only when
Hyperliquid explicitly reports that client order ID as unknown, and the same ID
is reused. Other ambiguous states are not resubmitted.
Close responses receive the same state-based recovery. Verified flatness is
required before the local allocation is cleared; a measured partial close gets
one residual-close attempt, and unresolved residuals remain tracked and
quarantined.
When several wallets share a coin position, exits use Hyperliquid's reduce-only
`market_close` size parameter for only the exiting wallet's reconstructed fill
size. The measured reduction must match before that wallet's local slices are
removed; unrelated wallet allocations remain open.
Same-side live additions are supported only when the local and Hyperliquid books
are synchronized. Adds inherit the coin's existing leverage, create a separate
wallet allocation slice, and remain subject to wallet, slice, coin-cost, and
buying-power limits.
Live `ENTRY` and `ADD` events expire after five minutes by default
(`LIVE_ENTRY_EVENT_MAX_AGE_SECS`). Durable exchange intents are recovered even
after that limit because an order may already have reached Hyperliquid, and
`EXIT` events never expire. An incoming opposite-side signal may replace an
existing position only when its wallet tier is strictly higher than every
incumbent wallet tier (Elite over Core, Core over Candidate). The incumbent
side must close successfully before the replacement order is considered;
numeric score differences within the same tier do not trigger reversals.
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
Hyperliquid account abstraction is detected automatically. Standard accounts
use their perpetuals margin summary; Unified and Portfolio Margin accounts use
USDC total and `tokenToAvailableAfterMaintenance` from the spot clearinghouse,
which Hyperliquid defines as the authoritative unified balance state. Missing or
internally inconsistent availability data blocks entries.
Live drawdown protection uses persisted rolling equity history. The 24-hour
window warns at 15% and blocks new entries for 24 hours at 25%; the seven-day
window warns at 35% and blocks new entries for seven days at 50%. Neither
breaker force-liquidates positions. The persistent all-time high-water remains
diagnostic, and intentional deposits or withdrawals should be recorded through
the supported capital-adjustment workflow rather than by resetting risk history.

Position-level intervention research runs in shadow mode by default. Every five
minutes (`POSITION_RISK_INTERVAL_SECS`), each open lifecycle records its signed
unlevered return, time below 5%/10%/15% loss, BTC/ETH/SOL-relative return,
funding, open interest, and volume in `position_risk_snapshots`. The states are
`HEALTHY`, `WATCH`, `ADD_FROZEN`, `THESIS_IMPAIRED`, and `EXIT_CANDIDATE`.
Shadow actions such as `WOULD_FREEZE_ADDS` and `WOULD_EXIT` are evidence only:
they do not suppress signals or submit orders. Snapshots remain available in
the database for offline analysis but are not displayed on the dashboards. Defaults
watch at 7% loss, flag an addition freeze at 10%, require at least four hours
below 10% plus a 15% loss for thesis impairment, and require six hours,
benchmark-relative weakness, and price/funding confirmation for an exit
candidate. Set `POSITION_RISK_SHADOW_ENABLED=0` to disable collection.

Wallet quality is scored from allocation-independent percentage returns. Each
copied exit is projected onto a fixed `$1,000` margin position at `3x` for the
profitability component (`SCORING_REFERENCE_MARGIN_USD` and
`SCORING_REFERENCE_LEVERAGE`). This preserves the original paper-test score
scale while preventing Paper's `$10,000` account or larger tier allocations from
mechanically overpowering outcomes from the `$500` Live account. Actual dollar
PnL remains in the trade ledger and score explanation for reporting only.
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
Each live allocation also records its authoritative exchange-filled quantity;
partial wallet exits close that exact quantity instead of reconstructing units
from cent-rounded margin. When the exiting wallet owns every remaining slice for
a coin, the bot requests the complete verified exchange position and requires a
flat terminal state. Any measured whole-unit dust is retried once before local
ownership is cleared.
Each live cycle validates the exchange book before source-wallet reconciliation.
Source-driven and signal-driven closes are skipped for quarantined coins until
side, leverage, and size synchronization clears the quarantine; unrelated coins
continue normally. If the live book is unavailable, entries and periodic source
reconciliation are blocked for that cycle, while an ordinary exit still performs
its own authoritative pre-close exchange-state check.
An entry that produces a measured exchange fill but fails post-fill validation
gets one reduce-only rollback for exactly that fill size. Confirmed rollback
restores the pre-entry size without retrying the entry. Failed or unverifiable
rollback produces a prominent `ENTRY ROLLBACK FAILED` coin quarantine and
notification while the remaining book continues.

Live state is backed up at startup and every six hours by default using SQLite's
online backup API (`BACKUP_INTERVAL_SECS`). A snapshot is published only after
`PRAGMA integrity_check` succeeds, and the newest 14 copies are retained by
default (`BACKUP_RETENTION_COUNT`, minimum 2). Backup files remain inside the
ignored live data directory. The Live dashboard reports backup age and failures.

The Live dashboard uses the exact equity observation used by the risk manager,
including Unified Account handling. After 90 seconds without a fresh bot equity
observation it displays live equity and drawdown as unavailable; the separately
labeled local ledger estimate is never substituted for exchange equity.
